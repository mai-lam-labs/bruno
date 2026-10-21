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
import contextlib
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from fractions import Fraction
from math import ceil
from pathlib import Path
from typing import TypeGuard

import httpx
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress, HexStr
from eth_utils.address import to_checksum_address

from .accounts import derive, read_key
from .builders import Builder, send_bundle
from .bundle import TopUp, bundles, sign_funding
from .chain import Chain, ChainError, Latest, lasting_fees, market_tip, risen
from .common import BrunoError
from .config import ChainConfig, ConfigError, Eip712Domain
from .deployer import DEPLOYER, transaction_gas
from .eip3009 import CALLDATA_BYTES, last_block_before, transfer_gas
from .evm import NATIVE_ASSET, EncodedCall
from .forks import GasSchedule
from .maintenance import AccountsStatus, monitor, survey
from .measurements import Measurements, measure
from .net.jsonrpc import Answer, Call, JsonRpcError
from .plan import (
    Plan,
    Simulation,
    SimulationFailedError,
    empty,
    make_plan,
    make_plans,
    simulate,
    simulations,
)
from .prices import PriceError, Prices
from .runner import Payment, Transfer, in_call_runner
from .submission import InvalidSubmissionError, Submission, check_authorizations
from .tracked import MAX_REVERT_BYTES, SENT_KEPT, Status, Tracked
from .transactions import Fees, SignedTransaction, TransactionError, sign

logger = logging.getLogger(__name__)

# how far the tip may rise over a quote's blocks, as a multiple of today's: a quote allows for
# it, and an account is funded for it
TIP_ALLOWANCE = 2
# how long after a slot's start its block is looked for
SLOT_OFFSET_SECONDS = 1.0
# the least a step of the clock waits for the next
MIN_STEP_SECONDS = 0.1
# how soon a step that fails (the nodes not answering, say) is tried again: twice as long after
# each failure in a row, up to MAX_RETRY_SECONDS
RETRY_SECONDS = 1.0
MAX_RETRY_SECONDS = 300.0
# how soon measurements that couldn't be taken again are tried again, the last kept meanwhile
MEASURE_RETRY_SECONDS = 60.0
# the largest amount: a quote's runner pushes it, so its initcode is never longer than the
# one paid for
_MAX_AMOUNT = 2**256 - 1


def fee_ceiling(latest: Latest, quote_blocks: int) -> int:
    """What a gas can cost by a quote's last block, as of `latest`: the next base fee risen
    for `quote_blocks` blocks, and the tip doubled. Accounts are funded to it."""
    return risen(latest.next_base_fee, quote_blocks) + TIP_ALLOWANCE * market_tip(latest.tips)


class RelayError(BrunoError):
    pass


class UnsupportedError(RelayError):
    pass


class UnderpaidError(RelayError):
    def __init__(self, token: ChecksumAddress, required: int) -> None:
        super().__init__(f"underpaid: {required} of {token} is required")
        self.token = token
        self.required = required


class NoCapacityError(RelayError):
    """The queue is full of more profitable submissions: `required` of `token` would beat
    the least profitable of them."""

    def __init__(self, token: ChecksumAddress, required: int) -> None:
        super().__init__(f"no capacity: {required} of {token} would be taken")
        self.token = token
        self.required = required


class UnknownSubmissionError(RelayError):
    pass


class UnavailableError(RelayError):
    """The chain can't be served now: its nodes, its fork or its deployer aren't what the relay
    needs (measurements), or it hasn't been measured yet."""


# what one submission's work can fail with, without failing the others' in its batch or block
_ITEM_ERRORS = (
    TransactionError,
    PriceError,
    ChainError,
    JsonRpcError,
    httpx.HTTPError,
    RelayError,
    InvalidSubmissionError,
    SimulationFailedError,
)


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
        prices: Prices,
        builders: Sequence[Builder],
        client: httpx.AsyncClient,
        payee: ChecksumAddress,
    ) -> None:
        """Nothing is read here: the chain is measured when it's first needed (measurements)."""
        self.chain = chain
        self._config = config
        self._float = float_account
        self._margins = {
            to_checksum_address(t.address): Fraction(config.margin_multiplier)
            * Fraction(t.margin_multiplier)
            for t in config.tokens
        }
        self._prices = prices
        self._builders = builders
        self._client = client
        self.payee = payee
        # each token taken over x402, and its EIP-712 domain, as configured
        self.domains: Mapping[ChecksumAddress, Eip712Domain] = {
            to_checksum_address(t.address): t.eip3009
            for t in config.tokens
            if t.eip3009 is not None and to_checksum_address(t.address) != NATIVE_ASSET
        }
        self._funding_multiplier = Fraction(config.funding_multiplier)
        self._tracked: dict[bytes, Tracked] = {}
        self._batch = _Batch((), frozenset())
        self._funding: _Funding | None = None
        # submissions are queued one at a time: a duplicate or a full queue is decided once
        self._queue_lock = asyncio.Lock()
        # the chain as last measured; measured once at a time
        self._measured: Measurements | None = None
        self._measuring = asyncio.Lock()
        # the relay's own clock, once started: running while there's work
        self._clocked = False
        self._clock: asyncio.Task[None] | None = None
        # whether the clock is only waiting to forget finished submissions
        self._idle = False
        self._head: int | None = None
        # the float and its accounts as last read, by a batch or the treasury: what the root
        # shows
        self.accounts_status: AccountsStatus | None = None
        self._batch_due = 0.0
        # the latest block as the x402 challenge last read it, and when the next is due
        self._challenged: tuple[Latest, float] | None = None

    @property
    def config(self) -> ChainConfig:
        return self._config

    @property
    def float_account(self) -> LocalAccount:
        return self._float

    @property
    def prices(self) -> Prices:
        return self._prices

    @property
    def builders(self) -> Sequence[Builder]:
        return self._builders

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    def record_accounts(self, status: AccountsStatus) -> None:
        """`status` kept as the accounts' latest, unless one read at a later block is."""
        if self.accounts_status is None or status.block >= self.accounts_status.block:
            self.accounts_status = status

    def record_payee(self, holdings: Mapping[ChecksumAddress, int]) -> None:
        """What the payee holds of each x402 token, as the treasury last read it."""
        if self._measured is not None:
            self._measured = self._measured.holding(
                {t: a for t, a in holdings.items() if t in self.domains}
            )

    async def measurements(self) -> Measurements:
        """The chain as measured: measured again once `measure_seconds` old. If the nodes don't
        answer, the last are kept and tried again a minute later; with none, unavailable."""
        if self._fresh(self._measured):
            return self._measured
        async with self._measuring:
            if self._fresh(self._measured):
                return self._measured
            try:
                measured = await measure(self.chain, self._config, self.payee)
            except (ChainError, JsonRpcError, httpx.HTTPError) as e:
                if self._measured is None:
                    raise UnavailableError(f"not measured: {e}") from e
                logger.warning("chain %d: measuring: %s", self.chain.chain_id, e)
                retry_at = self._config.measure_seconds - MEASURE_RETRY_SECONDS
                measured = replace(self._measured, at=time.monotonic() - retry_at)
            for problem in measured.problems():
                logger.error("chain %d: %s", self.chain.chain_id, problem)
            self._measured = measured
            return measured

    def _fresh(self, measured: Measurements | None) -> TypeGuard[Measurements]:
        return (
            measured is not None and time.monotonic() - measured.at < self._config.measure_seconds
        )

    async def available(self) -> Measurements:
        """The measurements, if the chain can be served; else UnavailableError, why."""
        measured = await self.measurements()
        if measured.unavailable is not None:
            raise UnavailableError(measured.unavailable)
        return measured

    @property
    def problems(self) -> tuple[str, ...]:
        """What's wrong with the chain, as last measured: what the root shows."""
        if self._measured is None:
            return ()
        return self._measured.problems()

    @property
    def x402_timeout_seconds(self) -> int:
        """How long an x402 payment may wait to be included: the deadlines' furthest."""
        return self._config.max_deadline_blocks * self.chain.slot_seconds

    def terms(self) -> Terms:
        return Terms(
            payee=self.payee,
            tokens=tuple(to_checksum_address(t.address) for t in self._config.tokens),
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
        far as it can in `quote_blocks` blocks and the tip doubled, and our fee."""
        self._within_limits(gas, call_bytes)
        measured = await self.available()
        for token in tokens:
            self._supported(measured, token)
        latest = await self.chain.latest()
        return await self._quote(
            measured, latest, gas, call_bytes, authority, tokens, transferring=False
        )

    async def x402_quote(
        self,
        gas: int,
        call_bytes: int,
        authority: ChecksumAddress | None,
        *,
        existing: bool = False,
    ) -> Quotes:
        """`quote` in each token accepted over x402, the runner making its transfer: as of the
        latest block, read at most once a slot however many ask. `existing`: the authority is
        taken to exist, unread."""
        self._within_limits(gas, call_bytes)
        measured = await self.available()
        latest = await self._recent_latest()
        return await self._quote(
            measured,
            latest,
            gas,
            call_bytes,
            authority,
            self.x402_tokens(measured),
            transferring=True,
            existing=existing,
        )

    def x402_tokens(self, measured: Measurements) -> tuple[ChecksumAddress, ...]:
        """The tokens taken over x402 as measured: each configured one whose transfer was."""
        return tuple(
            t
            for t in self.domains
            if (erc20 := measured.tokens.get(t)) is not None and erc20.transfer_gas is not None
        )

    async def x402_deadline(self, valid_after: int, valid_before: int) -> int:
        """The last block an x402 payment valid after `valid_after` and before `valid_before`
        (Unix seconds) can be included in: at most `max_deadline_blocks` ahead."""
        latest = await self._recent_latest()
        if valid_after >= latest.timestamp + self.chain.slot_seconds:
            raise InvalidSubmissionError("validAfter", "not valid by the next block")
        last = last_block_before(latest, valid_before, self.chain.slot_seconds)
        if last <= latest.number:
            raise InvalidSubmissionError("validBefore", "passed")
        return min(last, latest.number + self._config.max_deadline_blocks)

    async def _recent_latest(self) -> Latest:
        """The latest block, read again only once the next is due: the x402 challenge is
        answered to anyone who asks, probes included."""
        if self._challenged is not None and time.time() < self._challenged[1]:
            return self._challenged[0]
        latest = await self.chain.latest()
        due = latest.timestamp + self.chain.slot_seconds + SLOT_OFFSET_SECONDS
        self._challenged = (latest, due)
        return latest

    async def _quote(
        self,
        measured: Measurements,
        latest: Latest,
        gas: int,
        call_bytes: int,
        authority: ChecksumAddress | None,
        tokens: Sequence[ChecksumAddress],
        *,
        transferring: bool,
        existing: bool = False,
    ) -> Quotes:
        schedule = _schedule(measured)
        target = authority if authority is not None else self.payee
        call = EncodedCall(target, HexStr("0x" + "ff" * call_bytes))
        authorities = () if authority is None else (authority,)
        empties = 0 if existing else await empty(self.chain, authorities)
        # the base fee risen and the tip doubled, as they can be by the quote's last block
        max_fee = risen(
            latest.next_base_fee, self._config.quote_blocks - 1
        ) + TIP_ALLOWANCE * self._tip(latest)
        quotes: list[TokenQuote] = []
        for token in tokens:
            erc20 = measured.tokens[token]
            transfer = None
            if transferring and erc20 is not None and erc20.transfer_gas is not None:
                bound = transfer_gas(erc20.transfer_gas, schedule, token in measured.payee_holds)
                transfer = Transfer(b"\xff" * CALLDATA_BYTES, bound)
            payment = Payment(erc20, self.payee, _MAX_AMOUNT, transfer)
            runner = in_call_runner(call, payment, schedule, delegated=set(authorities))
            limit = transaction_gas(
                runner.initcode, runner.frame_gas(gas), schedule, len(authorities), empties
            )
            cost = limit * max_fee + await self._our_fee(measured, latest, limit)
            quotes.append(TokenQuote(token, await self._price(token, cost)))
        return Quotes(
            valid_until_block=latest.number + self._config.quote_blocks, quotes=tuple(quotes)
        )

    async def submit(self, submission: Submission) -> Accepted:
        """Take `submission`, checked; or raise why not."""
        data = bytes.fromhex(submission.call.data.removeprefix("0x"))
        self._within_limits(submission.gas, len(data))
        measured = await self.available()
        self._supported(measured, submission.token)
        if submission.payee != self.payee:
            raise UnsupportedError(f"the payee is {self.payee}, not {submission.payee}")
        check_authorizations(
            submission.authorizations, self.chain.chain_id, self._config.max_authorizations
        )
        transfer = submission.transfer
        if transfer is not None:
            if submission.token not in self.x402_tokens(measured):
                raise UnsupportedError(f"{submission.token} isn't accepted over x402")
            if transfer.to != submission.payee or transfer.value != submission.amount:
                raise InvalidSubmissionError("payment", "the transfer isn't the payment")
        id_ = submission.id
        if id_ in self._tracked:
            return Accepted(id_, new=False)
        latest = await self.chain.latest()
        head = latest.number
        if submission.max_block_number <= head:
            raise InvalidSubmissionError("maxBlockNumber", "passed")
        if submission.max_block_number > head + self._config.max_deadline_blocks:
            raise InvalidSubmissionError(
                "maxBlockNumber", f"over {self._config.max_deadline_blocks} blocks ahead"
            )
        schedule = _schedule(measured)
        plan = await make_plan(
            self.chain, submission, schedule, measured.tokens, measured.payee_holds
        )
        fee = self._fees(latest).max_fee_per_gas
        required = await self._charge(measured, latest, submission.token, plan.gas_limit)
        if submission.amount < required:
            raise UnderpaidError(submission.token, required)
        await simulate(self.chain, submission, plan, derive(self._float, 0).address)
        tracked = Tracked(submission, plan.gas_limit)
        async with self._queue_lock:
            if id_ in self._tracked:  # taken while this one was checked
                return Accepted(id_, new=False)
            queue = self._in(Status.PENDING)
            if len(queue) >= self._config.max_pending:
                await self._make_room(tracked, queue, fee)
            self._tracked[id_] = tracked
        self._wake()
        return Accepted(id_, new=True)

    async def _make_room(self, tracked: Tracked, queue: list[Tracked], fee: int) -> None:
        """Evict the least profitable of `queue` for `tracked`, if `tracked` is more profitable;
        or raise NoCapacityError with what would be taken. A submission whose transaction is
        out isn't evicted: it may land."""
        profits = [(await self._profit(t, fee), t) for t in queue]
        evictable = [(p, t) for p, t in profits if not t.sent]
        worst_profit, worst = min(evictable or profits, key=lambda p: p[0])
        if not evictable or await self._profit(tracked, fee) <= worst_profit:
            submission = tracked.submission
            cost = tracked.gas_limit * fee
            wei = await self._prices.wei(submission.token)
            raise NoCapacityError(submission.token, (worst_profit + cost) // wei + 1)
        worst.finish(Status.REJECTED, "outbid")

    def status(self, id_: bytes) -> Tracked:
        tracked = self._tracked.get(id_)
        if tracked is None:
            raise UnknownSubmissionError(f"no submission 0x{id_.hex()}")
        return tracked

    @property
    def busy(self) -> bool:
        """Whether there's work for the next block: a submission pending, or funding in
        flight."""
        return self._funding is not None or bool(self._in(Status.PENDING))

    def start(self) -> None:
        """Run the relay's own clock from now: each slot's block and each batch as it falls
        due, while there's work. A submission taken wakes it."""
        self._clocked = True
        self._wake()

    async def stop(self) -> None:
        """Stop the clock."""
        self._clocked = False
        if self._clock is not None:
            self._clock.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._clock
            self._clock = None

    def _wake(self) -> None:
        """The clock running, if it's started: a new one in place of one only waiting to
        forget."""
        running = self._clock is not None and not self._clock.done()
        if not self._clocked or (running and not self._idle):
            return
        if self._clock is not None:
            self._clock.cancel()
        self._clock = asyncio.create_task(self._run())

    async def _run(self) -> None:
        """While there's work, a step each slot: its block, and the batch as it falls due. A
        step that fails is tried again later, twice as long after each failure in a row.
        Without work, the finished submissions forgotten as they fall due, nothing read; then
        nothing at all."""
        failures = 0
        while True:
            if not self.busy:
                self._prune()
                forget_at = self._forget_at()
                if forget_at is None:
                    return
                self._idle = True
                try:
                    await asyncio.sleep(max(forget_at - time.monotonic(), 0))
                finally:
                    self._idle = False
                continue
            try:
                delay = await self._step()
                failures = 0
            except Exception:
                logger.exception("chain %d: a slot's work", self.chain.chain_id)
                delay = min(RETRY_SECONDS * 2**failures, MAX_RETRY_SECONDS)
                failures += 1
            await asyncio.sleep(delay)

    async def _step(self) -> float:
        """The block, if there's a new one, and the batch, if it's due, both as of one read of
        the latest block: how long until the next step."""
        latest = await self.chain.latest()
        if latest.number != self._head:
            await self.on_block(latest)
            self._head = latest.number
        if time.monotonic() >= self._batch_due:
            # due again a batch later whether this one succeeds or not: a failing batch isn't
            # retried every step
            self._batch_due = time.monotonic() + self._config.batch_seconds
            await self.close_batch(latest)
        slot = latest.timestamp + self.chain.slot_seconds + SLOT_OFFSET_SECONDS - time.time()
        return max(min(slot, self._batch_due - time.monotonic()), MIN_STEP_SECONDS)

    async def close_batch(self, latest: Latest) -> None:
        """Make the next batch, as of `latest`: rank the queue, take the top `accounts` that
        still simulate and cover the next block's price, map them onto accounts, and send the
        float's funding for those that need it (unless a funding transaction is in flight:
        they wait for the next batch). Nothing queued, nothing read. The queue's reads, its
        simulations and the accounts' state are each one batch."""
        pending = self._in(Status.PENDING)
        if not pending:
            self._batch = _Batch((), frozenset())
            return
        measured = await self.measurements()
        if measured.unavailable is not None:  # nothing sent: the queue waits, to its deadlines
            self._batch = _Batch((), frozenset())
            return
        schedule = _schedule(measured)
        fee = self._fees(latest).max_fee_per_gas
        plans = await make_plans(
            self.chain,
            [t.submission for t in pending],
            schedule,
            measured.tokens,
            measured.payee_holds,
        )
        ranked: list[tuple[Fraction, Tracked, Plan]] = []
        for tracked, plan in zip(pending, plans, strict=True):
            try:
                if isinstance(plan, Exception):
                    raise plan
                tracked.gas_limit = plan.gas_limit
                token = tracked.submission.token
                required = await self._charge(measured, latest, token, plan.gas_limit)
                if tracked.submission.amount >= required:
                    ranked.append((await self._profit(tracked, fee), tracked, plan))
            except _ITEM_ERRORS as e:  # this one waits; the rest go on
                logger.warning("ranking 0x%s: %s", tracked.submission.id.hex(), e)
        ranked.sort(key=lambda r: r[0], reverse=True)
        # every ranked one simulated together: the best that still simulate are chosen
        sender = derive(self._float, 0).address
        simulating = await self._still_simulate(
            [t for _, t, _ in ranked], [Simulation(t.submission, p, sender) for _, t, p in ranked]
        )
        chosen = [(t, p) for (_, t, p), ok in zip(ranked, simulating, strict=True) if ok]
        chosen = chosen[: self._config.accounts]
        if not chosen:
            self._batch = _Batch((), frozenset())
            return
        status = await survey(self.chain, self._float, self._config.accounts, latest.number)
        self.record_accounts(status)
        free = list(status.accounts)
        tip = self._tip(latest)
        ceiling = fee_ceiling(latest, self._config.quote_blocks)
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
            nonce, balance = status.float_nonce, status.float_balance
            # valid until it's given up on: its timeout's blocks
            blocks = ceil(self._config.funding_timeout_seconds / self.chain.slot_seconds)
            fees = lasting_fees(latest, tip, blocks)
            tx = sign_funding(self._float, self.chain.chain_id, nonce, fees, top_ups, schedule)
            if balance < tx.max_cost:
                # not sent: a node would refuse it. The next batch tries again, the float
                # perhaps topped up by then
                logger.warning(
                    "funding: the float holds %d wei, the funding %d", balance, tx.max_cost
                )
            else:
                # recorded before it's sent: the float's nonce is never signed for twice
                self._funding = _Funding(tx, frozenset(awaiting), time.monotonic())
                try:
                    await self.chain.rpc("eth_sendRawTransaction", ["0x" + tx.raw.hex()])
                except JsonRpcError as e:  # refused, so not in flight: the next batch tries
                    logger.warning("funding 0x%s refused: %s", tx.hash.hex(), e)
                    self._funding = None
        self._batch = _Batch(tuple(slots), frozenset(awaiting))

    async def on_block(self, latest: Latest) -> None:
        """`latest` is the latest block: settle what was sent for it and the funding in
        flight, then send the batch's bundles for the next. Each slot's transaction is signed
        if it covers the block's price, still simulates, and its account covers it; the
        receipts, the slots' reads, their simulations and their accounts' state are each one
        batch, and none is read when there's none to read."""
        target = latest.number + 1
        self._prune()
        await self._settle(target)
        slots = [
            s
            for s in self._batch.slots
            if s.tracked.status is Status.PENDING and s.account not in self._batch.awaiting
        ]
        if not slots:
            return
        measured = await self.measurements()
        if measured.unavailable is not None:  # settled, but nothing sent
            return
        schedule = _schedule(measured)
        fees = self._fees(latest)
        plans = await make_plans(
            self.chain,
            [s.tracked.submission for s in slots],
            schedule,
            measured.tokens,
            measured.payee_holds,
        )
        covered: list[tuple[_Slot, Plan]] = []
        for slot, plan in zip(slots, plans, strict=True):
            tracked = slot.tracked
            try:
                if isinstance(plan, Exception):
                    raise plan
                tracked.gas_limit = plan.gas_limit
                token = tracked.submission.token
                charge = await self._charge(measured, latest, token, plan.gas_limit)
                if tracked.submission.amount >= charge:
                    covered.append((slot, plan))
            except _ITEM_ERRORS as e:  # this one sits out the block; the rest go on
                logger.warning("signing 0x%s: %s", tracked.submission.id.hex(), e)
        simulating = await self._still_simulate(
            [s.tracked for s, _ in covered],
            [
                Simulation(s.tracked.submission, p, derive(self._float, s.account).address)
                for s, p in covered
            ],
        )
        ready = [c for c, ok in zip(covered, simulating, strict=True) if ok]
        if not ready:
            return
        states = await monitor(self.chain, self._float, [s.account for s, _ in ready])
        txs: list[SignedTransaction] = []
        for (slot, plan), state in zip(ready, states, strict=True):
            tracked = slot.tracked
            try:
                tx = sign(
                    derive(self._float, slot.account),
                    self.chain.chain_id,
                    state.nonce,
                    fees,
                    plan.gas_limit,
                    DEPLOYER,
                    plan.runner.initcode,
                    authorizations=tracked.submission.authorizations,
                )
            except _ITEM_ERRORS as e:
                logger.warning("signing 0x%s: %s", tracked.submission.id.hex(), e)
                continue
            if state.balance < tx.max_cost:
                continue  # fees rose past its funding: the next batch tops it up
            tracked.sent = (*tracked.sent, tx.hash)[-SENT_KEPT:]
            tracked.attempts += 1
            txs.append(tx)
        if txs:
            await self._send(target, txs)

    async def _send(self, target: int, txs: Sequence[SignedTransaction]) -> None:
        """Every builder its bundles of `txs` for block `target`."""
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

    def _prune(self) -> None:
        """Forget the submissions finished more than `retention_seconds` ago."""
        cutoff = time.monotonic() - self._config.retention_seconds
        for id_, tracked in list(self._tracked.items()):
            if tracked.finished_at is not None and tracked.finished_at <= cutoff:
                del self._tracked[id_]

    def _forget_at(self) -> float | None:
        """When the next finished submission is due to be forgotten: None if none is."""
        finished = [t.finished_at for t in self._tracked.values() if t.finished_at is not None]
        return min(finished) + self._config.retention_seconds if finished else None

    async def _settle(self, target: int) -> None:
        """Whether each pending submission's latest transactions landed, and the funding in
        flight: every receipt looked for in one batch. Each transaction is looked for again
        until it's SENT_KEPT sends old: a node without the block yet has no receipt. A
        submission past its deadline, unlanded, expires."""
        pending = self._in(Status.PENDING)
        funding = self._funding
        hashes = [h for t in pending for h in t.sent]
        if funding is not None:
            hashes.append(funding.tx.hash)
        answers = await self.chain.batch(
            [Call("eth_getTransactionReceipt", ["0x" + h.hex()]) for h in hashes]
        )
        receipts = dict(zip(hashes, answers, strict=True))
        for tracked in pending:
            try:
                self._settle_one(tracked, target, receipts)
            except _ITEM_ERRORS as e:
                logger.warning("settling 0x%s: %s", tracked.submission.id.hex(), e)
        if funding is not None:
            self._settle_funding(funding, receipts[funding.tx.hash].value())

    def _settle_one(self, tracked: Tracked, target: int, receipts: Mapping[bytes, Answer]) -> None:
        for sent in tracked.sent:
            receipt = receipts[sent].value()
            if receipt is not None:
                tracked.transaction_hash = sent
                tracked.block_number = int(receipt["blockNumber"], 16)
                if receipt["status"] == "0x1":
                    tracked.finish(Status.INCLUDED)
                else:
                    tracked.finish(Status.REJECTED, "reverted on-chain")
                return
        if target > tracked.submission.max_block_number:
            tracked.finish(Status.EXPIRED)

    def _settle_funding(self, funding: _Funding, receipt: dict[str, str] | None) -> None:
        """Whether the funding transaction in flight landed (its receipt, or None): its
        accounts are then ready. One that reverted, or wasn't seen in
        `funding_timeout_seconds`, is given up on."""
        if receipt is not None:
            self._funding = None
            if receipt["status"] == "0x1":
                self._batch = _Batch(self._batch.slots, self._batch.awaiting - funding.accounts)
            else:
                logger.error("funding 0x%s reverted", funding.tx.hash.hex())
        elif time.monotonic() - funding.sent_at > self._config.funding_timeout_seconds:
            self._funding = None
            logger.error("funding 0x%s not seen in time", funding.tx.hash.hex())

    async def _still_simulate(
        self, tracked: Sequence[Tracked], sims: Sequence[Simulation]
    ) -> list[bool]:
        """Which of `sims` still simulate, in one batch; those that don't are rejected, with
        their revert data. One with a transaction out only sits out: it may have landed,
        which its receipt tells, and a landed call no longer simulates."""
        still: list[bool] = []
        for t, revert in zip(tracked, await simulations(self.chain, sims), strict=True):
            if revert is not None and not t.sent:
                t.finish(Status.REJECTED, "simulation failed")
                t.revert_data = revert[:MAX_REVERT_BYTES]
            still.append(revert is None)
        return still

    async def _profit(self, tracked: Tracked, fee: int) -> Fraction:
        """The submission's amount in wei, less its transaction's cost at `fee` a gas."""
        wei = await self._prices.wei(tracked.submission.token)
        return tracked.submission.amount * wei - tracked.gas_limit * fee

    def _in(self, status: Status) -> list[Tracked]:
        return [t for t in self._tracked.values() if t.status is status]

    def _within_limits(self, gas: int, call_bytes: int) -> None:
        if gas > self._config.max_gas:
            raise InvalidSubmissionError("gas", f"over {self._config.max_gas}")
        if call_bytes > self._config.max_call_bytes:
            raise InvalidSubmissionError("call", f"over {self._config.max_call_bytes} bytes")

    def _supported(self, measured: Measurements, token: ChecksumAddress) -> None:
        if token not in self._margins:
            raise UnsupportedError(f"{token} isn't accepted")
        if token in measured.suspended:
            raise UnsupportedError(f"{token} is suspended: {measured.suspended[token]}")

    def _tip(self, latest: Latest) -> int:
        """The tip each transaction offers: the market's (market_tip)."""
        return market_tip(latest.tips)

    def _fees(self, latest: Latest) -> Fees:
        """The fees of the block after `latest`: exactly its base fee, and the tip."""
        tip = self._tip(latest)
        return Fees(latest.next_base_fee + tip, tip)

    async def _our_fee(self, measured: Measurements, latest: Latest, gas_limit: int) -> int:
        """Our fee on a transaction of `gas_limit`, in wei: the tip a gas, at least
        `min_fee_per_gas`; and at least `min_fee`, by its token's measured decimals."""
        fee = max(self._tip(latest), self._config.min_fee_per_gas) * gas_limit
        min_fee = self._config.min_fee
        if min_fee is None:
            return fee
        token = to_checksum_address(min_fee.token)
        decimals = measured.decimals.get(token)
        if decimals is None:
            raise UnavailableError(f"min_fee's token {token} is suspended")
        units = ceil(min_fee.amount * 10**decimals)
        return max(fee, ceil(units * await self._prices.wei(token)))

    async def _charge(
        self, measured: Measurements, latest: Latest, token: ChecksumAddress, gas_limit: int
    ) -> int:
        """What a transaction of `gas_limit` is charged in `token` in the block after
        `latest`: its gas at that block's fee, and our fee, margins included."""
        cost = gas_limit * self._fees(latest).max_fee_per_gas
        return await self._price(token, cost + await self._our_fee(measured, latest, gas_limit))

    async def _price(self, token: ChecksumAddress, wei: int) -> int:
        """`wei` in `token`'s base units, margins included, rounded up."""
        return ceil(Fraction(wei) / await self._prices.wei(token) * self._margins[token])


def open_relay(
    client: httpx.AsyncClient,
    chain_id: int,
    config: ChainConfig,
    float_account: LocalAccount,
) -> Relay:
    """The relay for chain `chain_id`, reading nothing: it measures the chain when it's first
    needed. ETH (ERC-7528's address) has no pools; WETH needs none either. A bundler without an
    identity key is signed for by the float."""
    if config.payee is None:
        raise ConfigError(f"chain {chain_id}: no payee (its own, the configuration's, or --payee)")
    for token in config.tokens:
        address = to_checksum_address(token.address)
        if address == NATIVE_ASSET:
            if token.price:
                raise ConfigError("ETH is priced at 1 wei a wei: it has no pools")
        elif not token.price and address != to_checksum_address(config.weth):
            raise ConfigError(f"{address}: no pools to price it by")
    if config.min_fee is not None and to_checksum_address(config.min_fee.token) not in {
        to_checksum_address(t.address) for t in config.tokens
    }:
        raise ConfigError(f"min_fee: {config.min_fee.token} isn't one of the chain's tokens")
    chain = Chain(client, chain_id, config.rpc, config.slot_seconds)
    prices = Prices(
        chain,
        to_checksum_address(config.weth),
        {to_checksum_address(t.address): t.price for t in config.tokens},
        config.price_ttl_seconds,
    )
    builders = [
        Builder(
            b.url,
            b.mode,
            float_account if b.identity_key is None else read_key(Path(b.identity_key)),
        )
        for b in config.bundlers
    ]
    return Relay(
        chain,
        config,
        float_account,
        prices,
        builders,
        client,
        to_checksum_address(config.payee),
    )


def _schedule(measured: Measurements) -> GasSchedule:
    """The measured schedule of an available chain."""
    if measured.schedule is None:
        raise UnavailableError(measured.unavailable or "no schedule measured")
    return measured.schedule
