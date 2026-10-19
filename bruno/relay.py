"""The relay on one chain: what it offers, its quotes, the submissions it takes, and each
block's bundles.

A submission is checked when it's taken (its token, payee, limits, a deadline at most
`max_deadline_blocks` ahead, its amount against the next block's price, and a simulation),
then queued: at most `max_pending`, a full queue taking a submission only by evicting its
least profitable. Queued submissions never hold an account.

Every `batch_seconds` a batch is made: the queue ranked by profit (the amount in wei less the
transaction's cost at the next block's fee), the top `accounts` that still simulate and
cover the price mapped onto concurrency accounts, each to the lowest-numbered free account
that already covers it, else the lowest-numbered free account, topped up from the float to
`funding_multiplier` times its transaction's cost. One funding transaction at a time, not
waited for: its accounts join the batch's blocks once it lands.

Each block, each of the batch's submissions on a ready account is simulated again, checked
against that block's price and its account's balance, signed at the account's nonce, and
sent to every builder in its mode. The next batch replaces the mapping; a submission that
hasn't landed is a queue entry again."""

import asyncio
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from math import ceil
from pathlib import Path

import httpx
from eth_abi.abi import encode
from eth_account.datastructures import SignedSetCodeAuthorization
from eth_account.signers.local import LocalAccount
from eth_keys.exceptions import BadSignature, ValidationError
from eth_typing import ChecksumAddress, HexStr
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from .accounts import derive, read_key
from .builders import Builder, send_bundle
from .bundle import TopUp, bundles, sign_funding
from .chain import Chain
from .common import BrunoError
from .config import ChainConfig, ConfigError
from .deployer import DEPLOYER, transaction_gas
from .evm import NATIVE_ASSET, EncodedCall
from .forks import GasSchedule
from .maintenance import monitor
from .net.jsonrpc import JsonRpcError
from .prices import Prices
from .runner import Erc20, Payment, Runner, RunnerError, in_call_runner
from .tokens import erc20
from .transactions import Fees, SignedTransaction, authorization_json, sign

logger = logging.getLogger(__name__)

# EIP-1559: the base fee rises by at most an eighth a block
MAX_BASE_FEE_RISE = Fraction(9, 8)
# how often the chain's head is read for a new block: under a slot
HEAD_POLL_SECONDS = 1.0
# the largest amount: a quote's runner pushes it, so its initcode is never longer than the
# one paid for
_MAX_AMOUNT = 2**256 - 1
_DELEGATION_PREFIX = "0xef0100"
_SUBMISSION_ID_TYPES = [
    "address",
    "bytes",
    "(uint256,address,uint256,uint8,uint256,uint256)[]",
    "uint256",
    "address",
    "address",
    "uint256",
    "uint256",
]


class RelayError(BrunoError):
    pass


class InvalidSubmissionError(RelayError):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field


class UnsupportedError(RelayError):
    pass


class UnderpaidError(RelayError):
    def __init__(self, token: ChecksumAddress, required: int) -> None:
        super().__init__(f"underpaid: {required} of {token} is required")
        self.token = token
        self.required = required


class SimulationFailedError(RelayError):
    def __init__(self, revert_data: bytes) -> None:
        super().__init__("simulation failed")
        self.revert_data = revert_data


class NoCapacityError(RelayError):
    """The queue is full of more profitable submissions: `required` of `token` would beat
    the least profitable of them."""

    def __init__(self, token: ChecksumAddress, required: int) -> None:
        super().__init__(f"no capacity: {required} of {token} would be taken")
        self.token = token
        self.required = required


class UnknownSubmissionError(RelayError):
    pass


@dataclass(frozen=True, slots=True)
class Submission:
    """A client's call, its authorizations, the gas the call is given, and its payment: at
    least `amount` of `token` to `payee`, by block `max_block_number`."""

    call: EncodedCall
    authorizations: tuple[SignedSetCodeAuthorization, ...]
    gas: int
    token: ChecksumAddress
    payee: ChecksumAddress
    amount: int
    max_block_number: int

    @property
    def id(self) -> bytes:
        """keccak256 of the submission ABI-encoded: the same submission, the same id."""
        return keccak(
            encode(
                _SUBMISSION_ID_TYPES,
                [
                    self.call.to,
                    bytes.fromhex(self.call.data.removeprefix("0x")),
                    [
                        (a.chain_id, to_checksum_address(a.address), a.nonce, a.y_parity, a.r, a.s)
                        for a in self.authorizations
                    ],
                    self.gas,
                    self.token,
                    self.payee,
                    self.amount,
                    self.max_block_number,
                ],
            )
        )


class Status(StrEnum):
    PENDING = "pending"
    INCLUDED = "included"
    EXPIRED = "expired"
    REJECTED = "rejected"


@dataclass(slots=True)
class Tracked:
    """A submission taken, and what's become of it. `gas_limit` is its transaction's, as
    last planned; `sent` the transaction sent for the latest block, until that block shows
    whether it landed."""

    submission: Submission
    gas_limit: int
    status: Status = Status.PENDING
    sent: bytes | None = None
    attempts: int = 0
    transaction_hash: bytes | None = None
    block_number: int | None = None
    reason: str | None = None
    revert_data: bytes | None = None


@dataclass(frozen=True, slots=True)
class Terms:
    """What a client needs before it signs: who to pay, in what, and the limits."""

    payee: ChecksumAddress
    tokens: tuple[ChecksumAddress, ...]
    max_gas: int
    max_call_bytes: int


@dataclass(frozen=True, slots=True)
class TokenQuote:
    token: ChecksumAddress
    amount: int


@dataclass(frozen=True, slots=True)
class Quotes:
    valid_until_block: int
    quotes: tuple[TokenQuote, ...]


@dataclass(frozen=True, slots=True)
class Accepted:
    """A submission's id, and whether it's new: one already taken is taken once."""

    id: bytes
    new: bool


@dataclass(frozen=True, slots=True)
class _Plan:
    """A submission's runner and its transaction's gas limit, under a block's schedule."""

    runner: Runner
    gas_limit: int


@dataclass(frozen=True, slots=True)
class _Slot:
    """A batch's submission, on concurrency account `account`."""

    tracked: Tracked
    account: int


@dataclass(frozen=True, slots=True)
class _Batch:
    """A batch's submissions on their accounts, and the accounts still awaiting the funding
    transaction."""

    slots: tuple[_Slot, ...]
    awaiting: frozenset[int]


@dataclass(frozen=True, slots=True)
class _Funding:
    """The float's funding transaction in flight, and the accounts it tops up."""

    tx: SignedTransaction
    accounts: frozenset[int]
    sent_at: float


class Relay:
    def __init__(
        self,
        chain: Chain,
        config: ChainConfig,
        float_account: LocalAccount,
        tokens: Mapping[ChecksumAddress, Erc20 | None],
        prices: Prices,
        builders: Sequence[Builder],
        client: httpx.AsyncClient,
    ) -> None:
        self.chain = chain
        self._config = config
        self._float = float_account
        self._tokens = tokens
        self._margins = {
            to_checksum_address(t.address): Fraction(config.margin_multiplier)
            * Fraction(t.margin_multiplier)
            for t in config.tokens
        }
        self._prices = prices
        self._builders = builders
        self._client = client
        self.payee = to_checksum_address(config.payee)
        self._funding_multiplier = Fraction(config.funding_multiplier)
        self._tracked: dict[bytes, Tracked] = {}
        self._batch = _Batch((), frozenset())
        self._funding: _Funding | None = None

    def terms(self) -> Terms:
        return Terms(
            payee=self.payee,
            tokens=tuple(self._tokens),
            max_gas=self._config.max_gas,
            max_call_bytes=self._config.max_call_bytes,
        )

    async def quote(
        self,
        gas: int,
        call_bytes: int,
        authority: ChecksumAddress | None,
        tokens: Sequence[ChecksumAddress],
    ) -> Quotes:
        """What a call given `gas`, of `call_bytes` bytes, with an authorization from
        `authority` if any, costs in each of `tokens`: enough for the next base fee risen as
        far as it can in `quote_blocks` blocks."""
        self._within_limits(gas, call_bytes)
        for token in tokens:
            self._supported(token)
        head = await self.chain.head()
        schedule = await self.chain.schedule()
        target = authority if authority is not None else self.payee
        call = EncodedCall(target, HexStr("0x" + "ff" * call_bytes))
        authorities = () if authority is None else (authority,)
        empty = await self._empty(authorities)
        max_fee = await self._max_fee(self._config.quote_blocks - 1)
        quotes: list[TokenQuote] = []
        for token in tokens:
            payment = Payment(self._tokens[token], self.payee, _MAX_AMOUNT)
            runner = in_call_runner(call, payment, schedule, delegated=set(authorities))
            limit = transaction_gas(
                runner.initcode, runner.frame_gas(gas), schedule, len(authorities), empty
            )
            quotes.append(TokenQuote(token, await self._price(token, limit * max_fee)))
        return Quotes(valid_until_block=head + self._config.quote_blocks, quotes=tuple(quotes))

    async def submit(self, submission: Submission) -> Accepted:
        """Take `submission`, checked; or raise why not."""
        id_ = submission.id
        if id_ in self._tracked:
            return Accepted(id_, new=False)
        data = bytes.fromhex(submission.call.data.removeprefix("0x"))
        self._within_limits(submission.gas, len(data))
        self._supported(submission.token)
        if submission.payee != self.payee:
            raise UnsupportedError(f"the payee is {self.payee}, not {submission.payee}")
        for auth in submission.authorizations:
            if auth.chain_id not in (0, self.chain.chain_id):
                raise InvalidSubmissionError("authorizationList", f"chain id {auth.chain_id}")
        head = await self.chain.head()
        if submission.max_block_number <= head:
            raise InvalidSubmissionError("maxBlockNumber", "passed")
        if submission.max_block_number > head + self._config.max_deadline_blocks:
            raise InvalidSubmissionError(
                "maxBlockNumber", f"over {self._config.max_deadline_blocks} blocks ahead"
            )
        schedule = await self.chain.schedule()
        plan = await self._plan(submission, schedule)
        fee = (await self._fees()).max_fee_per_gas
        required = await self._price(submission.token, plan.gas_limit * fee)
        if submission.amount < required:
            raise UnderpaidError(submission.token, required)
        await self._simulate(submission, plan, derive(self._float, 0).address)
        tracked = Tracked(submission, plan.gas_limit)
        queue = self._in(Status.PENDING)
        if len(queue) >= self._config.max_pending:
            profits = [(await self._profit(t, fee), t) for t in queue]
            worst_profit, worst = min(profits, key=lambda p: p[0])
            if await self._profit(tracked, fee) <= worst_profit:
                cost = plan.gas_limit * fee
                wei = await self._prices.wei(submission.token)
                raise NoCapacityError(submission.token, (worst_profit + cost) // wei + 1)
            worst.status = Status.REJECTED
            worst.reason = "outbid"
        self._tracked[id_] = tracked
        return Accepted(id_, new=True)

    def status(self, id_: bytes) -> Tracked:
        tracked = self._tracked.get(id_)
        if tracked is None:
            raise UnknownSubmissionError(f"no submission 0x{id_.hex()}")
        return tracked

    async def run(self) -> None:
        """Close a batch every `batch_seconds`, and bundle for each new block, forever."""
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self._batches())
            tasks.create_task(self._blocks())

    async def _batches(self) -> None:
        while True:
            await asyncio.sleep(self._config.batch_seconds)
            try:
                await self.close_batch()
            except Exception:
                logger.exception("chain %d: closing a batch", self.chain.chain_id)

    async def _blocks(self) -> None:
        last: int | None = None
        while True:
            try:
                head = await self.chain.head()
                if head != last:
                    await self.on_block(head)
                    last = head
            except Exception:
                logger.exception("chain %d: block %s", self.chain.chain_id, last)
            await asyncio.sleep(HEAD_POLL_SECONDS)

    async def close_batch(self) -> None:
        """Make the next batch: rank the queue, take the top `accounts` that still simulate
        and cover the next block's price, map them onto accounts, and send the float's
        funding for those that need it (unless a funding transaction is in flight: they wait
        for the next batch)."""
        schedule = await self.chain.schedule()
        fee = (await self._fees()).max_fee_per_gas
        ranked: list[tuple[Fraction, Tracked, _Plan]] = []
        for tracked in self._in(Status.PENDING):
            plan = await self._plan(tracked.submission, schedule)
            tracked.gas_limit = plan.gas_limit
            required = await self._price(tracked.submission.token, plan.gas_limit * fee)
            if tracked.submission.amount >= required:
                ranked.append((await self._profit(tracked, fee), tracked, plan))
        ranked.sort(key=lambda r: r[0], reverse=True)
        chosen: list[tuple[Tracked, _Plan]] = []
        for _, tracked, plan in ranked:
            if len(chosen) == self._config.accounts:
                break
            if await self._resimulated(tracked, plan, derive(self._float, 0).address):
                chosen.append((tracked, plan))
        states = await monitor(self.chain, self._float, range(self._config.accounts))
        free = list(states)
        ceiling = await self._max_fee(self._config.quote_blocks)
        slots: list[_Slot] = []
        top_ups: list[TopUp] = []
        awaiting: set[int] = set()
        for tracked, plan in chosen:
            cost = plan.gas_limit * ceiling
            covering = next((a for a in free if a.balance >= cost), None)
            if covering is None:
                if self._funding is not None:
                    continue  # one funding transaction at a time: the next batch
                covering = free[0]
                amount = ceil(Fraction(cost) * self._funding_multiplier) - covering.balance
                top_ups.append(TopUp(covering.address, amount, new_account=not covering.exists))
                awaiting.add(covering.index)
            free.remove(covering)
            slots.append(_Slot(tracked, covering.index))
        if top_ups:
            nonce = await self.chain.nonce(self._float.address)
            fees = await self.chain.fees(self._config.priority_fee)
            tx = sign_funding(self._float, self.chain.chain_id, nonce, fees, top_ups, schedule)
            await self.chain.rpc("eth_sendRawTransaction", ["0x" + tx.raw.hex()])
            self._funding = _Funding(tx, frozenset(awaiting), time.monotonic())
        self._batch = _Batch(tuple(slots), frozenset(awaiting))

    async def on_block(self, head: int) -> None:
        """Block `head` is the latest: settle what was sent for it and the funding in flight,
        then send the batch's bundles for the next block."""
        target = head + 1
        for tracked in self._in(Status.PENDING):
            await self._settle(tracked, target)
        await self._settle_funding()
        schedule = await self.chain.schedule()
        fees = await self._fees()
        txs: list[SignedTransaction] = []
        for slot in self._batch.slots:
            tracked = slot.tracked
            if tracked.status is not Status.PENDING or slot.account in self._batch.awaiting:
                continue
            submission = tracked.submission
            plan = await self._plan(submission, schedule)
            tracked.gas_limit = plan.gas_limit
            required = await self._price(submission.token, plan.gas_limit * fees.max_fee_per_gas)
            if submission.amount < required:
                continue
            account = derive(self._float, slot.account)
            if not await self._resimulated(tracked, plan, account.address):
                continue
            tx = sign(
                account,
                self.chain.chain_id,
                await self.chain.nonce(account.address),
                fees,
                plan.gas_limit,
                DEPLOYER,
                plan.runner.initcode,
                authorizations=submission.authorizations,
            )
            if await self.chain.balance(account.address) < tx.max_cost:
                continue  # fees rose past its funding: the next batch tops it up
            tracked.sent = tx.hash
            tracked.attempts += 1
            txs.append(tx)
        if not txs:
            return
        sends = [
            (builder, params)
            for builder in self._builders
            for params in bundles(builder.mode, target, None, txs)
        ]
        results = await asyncio.gather(
            *(send_bundle(self._client, b.url, params, b.identity) for b, params in sends),
            return_exceptions=True,
        )
        for (builder, _), result in zip(sends, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("block %d: %s refused a bundle: %s", target, builder.url, result)

    async def _settle_funding(self) -> None:
        """Whether the funding transaction in flight landed: its accounts are then ready. One
        that reverted, or wasn't seen in `funding_timeout_seconds`, is given up on."""
        funding = self._funding
        if funding is None:
            return
        receipt = await self.chain.receipt(funding.tx.hash)
        if receipt is not None:
            self._funding = None
            if receipt["status"] == "0x1":
                self._batch = _Batch(self._batch.slots, self._batch.awaiting - funding.accounts)
            else:
                logger.error("funding 0x%s reverted", funding.tx.hash.hex())
        elif time.monotonic() - funding.sent_at > self._config.funding_timeout_seconds:
            self._funding = None
            logger.error("funding 0x%s not seen in time", funding.tx.hash.hex())

    async def _resimulated(self, tracked: Tracked, plan: _Plan, sender: ChecksumAddress) -> bool:
        """Whether the submission still simulates; if not, it's rejected, with the revert
        data."""
        try:
            await self._simulate(tracked.submission, plan, sender)
        except SimulationFailedError as e:
            tracked.status = Status.REJECTED
            tracked.reason = "simulation failed"
            tracked.revert_data = e.revert_data
            return False
        return True

    async def _profit(self, tracked: Tracked, fee: int) -> Fraction:
        """The submission's amount in wei, less its transaction's cost at `fee` a gas."""
        wei = await self._prices.wei(tracked.submission.token)
        return tracked.submission.amount * wei - tracked.gas_limit * fee

    def _in(self, status: Status) -> list[Tracked]:
        return [t for t in self._tracked.values() if t.status is status]

    async def _settle(self, tracked: Tracked, target: int) -> None:
        """Whether what was sent for the latest block landed; expired if `target` is past its
        deadline."""
        if tracked.sent is not None:
            receipt = await self.chain.receipt(tracked.sent)
            if receipt is not None:
                tracked.transaction_hash = tracked.sent
                tracked.block_number = int(receipt["blockNumber"], 16)
                if receipt["status"] == "0x1":
                    tracked.status = Status.INCLUDED
                else:
                    tracked.status = Status.REJECTED
                    tracked.reason = "reverted on-chain"
                return
            tracked.sent = None
        if target > tracked.submission.max_block_number:
            tracked.status = Status.EXPIRED

    def _within_limits(self, gas: int, call_bytes: int) -> None:
        if gas > self._config.max_gas:
            raise InvalidSubmissionError("gas", f"over {self._config.max_gas}")
        if call_bytes > self._config.max_call_bytes:
            raise InvalidSubmissionError("call", f"over {self._config.max_call_bytes} bytes")

    def _supported(self, token: ChecksumAddress) -> None:
        if token not in self._tokens:
            raise UnsupportedError(f"{token} isn't accepted")

    async def _fees(self) -> Fees:
        """The next block's fees: exactly its base fee, and the priority fee."""
        priority = self._config.priority_fee
        return Fees(await self.chain.next_base_fee() + priority, priority)

    async def _max_fee(self, blocks: int) -> int:
        """The most a transaction's fee can be `blocks` blocks after the next: the next base
        fee risen by an eighth each block, and the priority fee."""
        rise = MAX_BASE_FEE_RISE**blocks
        return ceil(await self.chain.next_base_fee() * rise) + self._config.priority_fee

    async def _price(self, token: ChecksumAddress, wei: int) -> int:
        """`wei` in `token`'s base units, margins included, rounded up."""
        return ceil(Fraction(wei) / await self._prices.wei(token) * self._margins[token])

    async def _empty(self, authorities: Sequence[ChecksumAddress]) -> int:
        """How many of `authorities` are empty: no balance, no nonce, no code."""
        empty = 0
        for authority in authorities:
            if (
                await self.chain.balance(authority) == 0
                and await self.chain.nonce(authority) == 0
                and await self.chain.code(authority) == "0x"
            ):
                empty += 1
        return empty

    async def _plan(self, submission: Submission, schedule: GasSchedule) -> _Plan:
        try:
            authorities = tuple(to_checksum_address(a.authority) for a in submission.authorizations)
        except (BadSignature, ValidationError) as e:
            raise InvalidSubmissionError("authorizationList", f"unrecoverable: {e}") from e
        delegated = set(authorities)
        if (await self.chain.code(submission.call.to)).startswith(_DELEGATION_PREFIX):
            delegated.add(submission.call.to)
        payment = Payment(self._tokens[submission.token], submission.payee, submission.amount)
        try:
            runner = in_call_runner(submission.call, payment, schedule, delegated)
        except RunnerError as e:
            raise InvalidSubmissionError("call", str(e)) from e
        gas_limit = transaction_gas(
            runner.initcode,
            runner.frame_gas(submission.gas),
            schedule,
            len(authorities),
            await self._empty(authorities),
        )
        return _Plan(runner, gas_limit)

    async def _simulate(self, submission: Submission, plan: _Plan, sender: ChecksumAddress) -> None:
        """The submission's transaction as eth_call from `sender`: raise its revert data if
        it reverts."""
        tx: dict[str, object] = {
            "from": sender,
            "to": DEPLOYER,
            "data": "0x" + plan.runner.initcode.hex(),
            "gas": hex(plan.gas_limit),
        }
        if submission.authorizations:
            tx["authorizationList"] = [authorization_json(a) for a in submission.authorizations]
        try:
            await self.chain.rpc("eth_call", [tx, "latest"])
        except JsonRpcError as e:
            data = e.data if isinstance(e.data, str) else "0x"
            raise SimulationFailedError(bytes.fromhex(data.removeprefix("0x"))) from e


async def open_relay(
    client: httpx.AsyncClient,
    chain_id: int,
    config: ChainConfig,
    float_account: LocalAccount,
    forks: tuple[tuple[int, GasSchedule], ...],
) -> Relay:
    """The relay for chain `chain_id`, its nodes checked and each token's balanceOf gas
    measured against the payee. ETH (ERC-7528's address) is read with BALANCE: nothing to
    measure, and no pools."""
    chain = Chain(client, chain_id, config.rpc, forks)
    await chain.check_chain()
    schedule = await chain.schedule()
    payee = to_checksum_address(config.payee)
    tokens: dict[ChecksumAddress, Erc20 | None] = {}
    for token in config.tokens:
        address = to_checksum_address(token.address)
        if address == NATIVE_ASSET:
            if token.price:
                raise ConfigError("ETH is priced at 1 wei a wei: it has no pools")
            tokens[address] = None
            continue
        if not token.price:
            raise ConfigError(f"{address}: no pools to price it by")
        tokens[address] = await erc20(client, config.rpc[0], address, payee, schedule)
    prices = Prices(
        chain,
        to_checksum_address(config.weth),
        {to_checksum_address(t.address): t.price for t in config.tokens},
        config.price_ttl_seconds,
    )
    builders = [
        Builder(b.url, b.mode, None if b.identity_key is None else read_key(Path(b.identity_key)))
        for b in config.bundlers
    ]
    return Relay(chain, config, float_account, tokens, prices, builders, client)
