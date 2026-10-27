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
from collections.abc import Callable, Mapping, Sequence
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
from .chain import Chain, ChainError, Finality, Latest, lasting_fees, market_tip, risen
from .common import BrunoError
from .config import ChainConfig, ConfigError, Eip712Domain
from .deployer import DEPLOYER
from .eip3009 import last_block_before, quoted_calldata, transfer_gas
from .erc6492 import Deployment
from .evm import NATIVE_ASSET, EncodedCall, has_code
from .forks import GasSchedule
from .maintenance import AccountsStatus, monitor, survey
from .measurements import Measurements, measure
from .net.jsonrpc import Answer, Call, JsonRpcError
from .plan import (
    BlockUnseenError,
    Plan,
    Planned,
    Simulation,
    SimulationFailedError,
    empty,
    make_plan,
    make_plans,
    runner_plan,
    simulate,
    simulations,
)
from .prices import PriceError, Prices
from .runner import Payment, Transfer, in_call_runner
from .submission import InvalidSubmissionError, Submission, check_authorizations
from .tracked import MAX_REVERT_BYTES, SENT_KEPT, Status, Tracked
from .transactions import Fees, SignedTransaction, TransactionError, sign
from .wallets import verification

logger = logging.getLogger(__name__)

# how far the tip may rise over a quote's blocks, as a multiple of today's: a quote allows for
# it, and an account is funded for it
TIP_ALLOWANCE = 2
# how long after a slot's start its block is looked for
SLOT_OFFSET_SECONDS = 1.0
# how soon a step that fails (the nodes not answering, say) is tried again: twice as long after
# each failure in a row, up to MAX_RETRY_SECONDS
RETRY_SECONDS = 1.0
MAX_RETRY_SECONDS = 300.0
# how soon measurements that couldn't be taken again are tried again, the last kept meanwhile
MEASURE_RETRY_SECONDS = 60.0
# the largest amount: a quote's runner pushes it, so its initcode is never longer than the
# one paid for
_MAX_AMOUNT = 2**256 - 1


def fee_ceiling(latest: Latest, quote_blocks: int) -> Fees:
    """The fees a transaction can pay by a quote's last block, as of `latest`: the next base
    fee risen for `quote_blocks` blocks, and the tip doubled; what's paid beyond gas, risen for
    them too. Accounts are funded to it."""
    tip = market_tip(latest.tips)
    data_fee = None if latest.data_fee is None else latest.data_fee.risen(quote_blocks)
    return Fees(risen(latest.next_base_fee, quote_blocks) + TIP_ALLOWANCE * tip, tip, data_fee)


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
class Payer:
    """What a quote is told of its payment's signer, each if known: its `address` (the payment
    then a transfer, EIP-3009), an account to `deploy` first, and the gas its signature check
    takes (`verify_gas`)."""

    address: ChecksumAddress | None = None
    deployment: Deployment | None = None
    verify_gas: int | None = None


# a quote told nothing of its payment's signer: the call pays, or (over x402) a key's transfer
NO_PAYER = Payer()


@dataclass(frozen=True, slots=True)
class _Signing:
    """How a quote prices its payment's transfer: whether there is one, and if its signer is a
    contract, its check's gas (`verify_gas`) and its signature's length (`signature_bytes`); and
    the gas its deployment's factory call takes, if one is made."""

    transferring: bool
    verify_gas: int = 0
    signature_bytes: int | None = None
    deployment: Deployment | None = None
    deploy_gas: int = 0


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
        # with nothing measured, when the nodes may be asked again after they didn't answer
        self._unmeasured_until = 0.0
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
        # the latest and finalized blocks as a status last read them, and when the next is due
        self._final: tuple[Finality, float] | None = None
        # each held request's submission, set once it's no longer pending
        self._outcomes: dict[bytes, asyncio.Event] = {}

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
        answer, the last are kept and tried again a minute later; with none, unavailable, and
        not tried again for a minute. Why stays in the log: a node's URL isn't a client's."""
        if self._fresh(self._measured):
            return self._measured
        async with self._measuring:
            if self._fresh(self._measured):
                return self._measured
            if self._measured is None and time.monotonic() < self._unmeasured_until:
                raise UnavailableError("not measured yet: its nodes didn't answer")
            try:
                measured = await measure(self.chain, self._config, self.payee)
            except (ChainError, JsonRpcError, httpx.HTTPError) as e:
                logger.warning("chain %d: measuring: %s", self.chain.chain_id, e)
                if self._measured is None:
                    self._unmeasured_until = time.monotonic() + MEASURE_RETRY_SECONDS
                    raise UnavailableError("not measured yet: its nodes didn't answer") from e
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
        """How long an x402 payment's request may be held: its deadline's furthest."""
        return self._config.x402_deadline_blocks * self.chain.slot_seconds

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
        payer: Payer = NO_PAYER,
    ) -> Quotes:
        """What a call given `gas`, of `call_bytes` bytes, with an authorization from
        `authority` if any, costs in each of `tokens`: enough for the next base fee risen as
        far as it can in `quote_blocks` blocks and the tip doubled, and our fee; paid by a
        transfer from `payer`, if it names one, its deployment made first."""
        self._within_limits(gas, call_bytes)
        measured = await self.available()
        for token in tokens:
            self._supported(measured, token)
        latest = await self._latest()
        signing = await self._signing(measured, payer, transferring=payer.address is not None)
        return await self._quote(measured, latest, gas, call_bytes, authority, tokens, signing)

    async def x402_quote(
        self,
        gas: int,
        call_bytes: int,
        authority: ChecksumAddress | None,
        payer: Payer = NO_PAYER,
        *,
        existing: bool = False,
    ) -> Quotes:
        """`quote` in each token accepted over x402, the runner making its transfer, from
        `payer` as far as it's known: as of the latest block, read at most once a slot however
        many ask. `existing`: the authority is taken to exist, unread."""
        self._within_limits(gas, call_bytes)
        measured = await self.available()
        latest = await self._recent_latest()
        signing = await self._signing(measured, payer, transferring=True)
        return await self._quote(
            measured,
            latest,
            gas,
            call_bytes,
            authority,
            self.x402_tokens(measured),
            signing,
            existing=existing,
        )

    async def _signing(
        self, measured: Measurements, payer: Payer, *, transferring: bool
    ) -> _Signing:
        """How `payer`'s transfer is priced, if `transferring`. Its signer is `payer.address`,
        else the account its deployment deploys. A contract (one with `verify_gas` given, one
        deployed, or one with code) has its check priced at `verify_gas`, else at the wallet gas
        schedule's figure for its deployment's factory, else at the schedule's largest. A
        deployment whose account has code isn't made; one that is has its factory call
        estimated: InvalidSubmissionError if it reverts, or if `verify_gas` is over the most gas
        a call may have. One read batch, if any."""
        if payer.verify_gas is not None and payer.verify_gas > self._config.max_gas:
            raise InvalidSubmissionError("verifyGas", f"over {self._config.max_gas}")
        deployment = payer.deployment
        signer = payer.address
        if signer is None and transferring and deployment is not None:
            signer = deployment.account
        calls: list[Call] = []
        if deployment is not None:
            calls += [
                Call("eth_getCode", [deployment.account, "latest"]),
                Call(
                    "eth_estimateGas",
                    [{"to": deployment.factory, "data": "0x" + deployment.factory_data.hex()}],
                ),
            ]
        # the signer's own code, unless it's the account deployed
        read_signer = signer is not None and (deployment is None or deployment.account != signer)
        if read_signer:
            calls.append(Call("eth_getCode", [signer, "latest"]))
        answers = await self.chain.batch(calls) if calls else []
        schedule = _schedule(measured)
        deploy_gas = 0
        deploys = False
        if deployment is not None:
            deploys = not has_code(answers[0].value())
            if deploys:
                estimate = answers[1]
                if estimate.error is not None:
                    raise InvalidSubmissionError(
                        "deployment", f"its factory call fails: {estimate.error.message}"
                    )
                intrinsic, _ = schedule.intrinsic(deployment.factory_data)
                deploy_gas = int(estimate.value(), 16) - intrinsic
        signer_has_code = read_signer and has_code(answers[-1].value())
        contract = transferring and (
            payer.verify_gas is not None
            or (deployment is not None and deployment.account == signer)
            or signer_has_code
        )
        if not contract:
            return _Signing(
                transferring, deployment=deployment if deploys else None, deploy_gas=deploy_gas
            )
        known = verification(None if deployment is None else deployment.factory, schedule)
        return _Signing(
            transferring,
            verify_gas=known.gas if payer.verify_gas is None else payer.verify_gas,
            signature_bytes=known.signature_bytes,
            deployment=deployment if deploys else None,
            deploy_gas=deploy_gas,
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
        (Unix seconds) can be included in: at most `x402_deadline_blocks` ahead."""
        latest = await self._recent_latest()
        if valid_after >= latest.timestamp + self.chain.slot_seconds:
            raise InvalidSubmissionError("validAfter", "not valid by the next block")
        last = last_block_before(latest, valid_before, self.chain.slot_seconds)
        if last <= latest.number:
            raise InvalidSubmissionError("validBefore", "passed")
        return min(last, latest.number + self._config.x402_deadline_blocks)

    async def _recent_latest(self) -> Latest:
        """The latest block, read again only once the next is due: the x402 challenge is
        answered to anyone who asks, probes included."""
        if self._challenged is not None and time.time() < self._challenged[1]:
            return self._challenged[0]
        latest = await self._latest()
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
        signing: _Signing,
        *,
        existing: bool = False,
    ) -> Quotes:
        schedule = _schedule(measured)
        target = authority if authority is not None else self.payee
        call = EncodedCall(target, HexStr("0x" + "ff" * call_bytes))
        authorities = () if authority is None else (authority,)
        empties = 0 if existing else await empty(self.chain, authorities)
        # the base fee risen and the tip doubled, as they can be by the quote's last block
        fees = fee_ceiling(latest, self._config.quote_blocks - 1)
        quotes: list[TokenQuote] = []
        for token in tokens:
            erc20 = measured.tokens[token]
            transfer = None
            if signing.transferring and erc20 is not None and erc20.transfer_gas is not None:
                bound = transfer_gas(erc20.transfer_gas, schedule, token in measured.payee_holds)
                transfer = Transfer(
                    quoted_calldata(signing.signature_bytes), bound + signing.verify_gas
                )
            payment = Payment(erc20, self.payee, _MAX_AMOUNT, transfer)
            runner = in_call_runner(
                call,
                payment,
                schedule,
                delegated=set(authorities),
                client_gas=gas,
                deployment=signing.deployment,
            )
            plan = runner_plan(
                self.chain.chain_id,
                runner,
                gas,
                schedule,
                len(authorities),
                empties,
                extra_gas=signing.deploy_gas,
            )
            quotes.append(
                TokenQuote(token, await self._charge(measured, latest, token, plan, fees))
            )
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
                raise UnsupportedError(f"{submission.token} takes no EIP-3009 transfer")
            if transfer.to != submission.payee or transfer.value != submission.amount:
                raise InvalidSubmissionError("payment", "the transfer isn't the payment")
        id_ = submission.id
        if id_ in self._tracked:
            return Accepted(id_, new=False)
        latest = await self._latest()
        head = latest.number
        if submission.max_block_number <= head:
            raise InvalidSubmissionError("maxBlockNumber", "passed")
        if submission.max_block_number > head + self._config.max_deadline_blocks:
            raise InvalidSubmissionError(
                "maxBlockNumber", f"over {self._config.max_deadline_blocks} blocks ahead"
            )
        schedule = _schedule(measured)
        sender = self._sender
        plan = await make_plan(
            self.chain, submission, schedule, measured.tokens, sender, measured.payee_holds
        )
        fees = self._fees(latest)
        required = await self._charge(measured, latest, submission.token, plan)
        if submission.amount < required:
            raise UnderpaidError(submission.token, required)
        try:
            await simulate(self.chain, submission, plan, sender)
        except BlockUnseenError as e:  # nothing known of it yet: the client tries again
            raise UnavailableError(str(e)) from e
        tracked = Tracked(submission, plan.gas_limit, plan.size)
        async with self._queue_lock:
            if id_ in self._tracked:  # taken while this one was checked
                return Accepted(id_, new=False)
            queue = self._in(Status.PENDING)
            if len(queue) >= self._config.max_pending:
                await self._make_room(tracked, queue, fees)
            self._tracked[id_] = tracked
        self._wake()
        return Accepted(id_, new=True)

    async def _make_room(self, tracked: Tracked, queue: list[Tracked], fees: Fees) -> None:
        """Evict the least profitable of `queue` for `tracked`, if `tracked` is more profitable;
        or raise NoCapacityError with what would be taken. A submission whose transaction is
        out isn't evicted: it may land."""
        profits = [(await self._profit(t, fees), t) for t in queue]
        evictable = [(p, t) for p, t in profits if not t.sent]
        worst_profit, worst = min(evictable or profits, key=lambda p: p[0])
        if not evictable or await self._profit(tracked, fees) <= worst_profit:
            submission = tracked.submission
            cost = fees.cost(tracked.gas_limit, tracked.size)
            wei = await self._prices.wei(submission.token)
            raise NoCapacityError(submission.token, (worst_profit + cost) // wei + 1)
        self._finish(worst, Status.REJECTED, "outbid")

    def status(self, id_: bytes) -> Tracked:
        tracked = self._tracked.get(id_)
        if tracked is None:
            raise UnknownSubmissionError(f"no submission 0x{id_.hex()}")
        return tracked

    async def outcome(self, id_: bytes) -> Tracked:
        """The submission once it's no longer pending: included, rejected or expired. Waiting
        reads nothing: it's the settlement the relay runs each block that ends it."""
        tracked = self.status(id_)
        if tracked.status is Status.PENDING:
            await self._outcomes.setdefault(id_, asyncio.Event()).wait()
        return tracked

    def pending_like(self, submission: Submission) -> Tracked | None:
        """A pending x402 submission of the same call by the same payer (its token, call, gas
        and authorizations), paid by another authorization: a client that timed out and signed
        again. None if there's none."""
        transfer = submission.transfer
        if transfer is None:
            return None
        for t in self._in(Status.PENDING):
            other = t.submission
            if (
                other.transfer is not None
                and other.transfer.payer == transfer.payer
                and (other.token, other.call, other.gas, other.authorizations)
                == (submission.token, submission.call, submission.gas, submission.authorizations)
            ):
                return t
        return None

    async def finality(self) -> Finality:
        """The latest and finalized blocks, read again only once the next block is due: a
        status's confirmations, however many ask."""
        if self._final is not None and time.time() < self._final[1]:
            return self._final[0]
        final = await self.chain.finality()
        due = final.timestamp + self.chain.slot_seconds + SLOT_OFFSET_SECONDS
        self._final = (final, due)
        return final

    def _finish(self, tracked: Tracked, status: Status, reason: str | None = None) -> None:
        """`tracked` finished, as `status`: a request held for it is answered."""
        tracked.finish(status, reason)
        outcome = self._outcomes.pop(tracked.submission.id, None)
        if outcome is not None:
            outcome.set()

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
        latest = await self._latest()
        if latest.number != self._head:
            await self.on_block(latest)
            self._head = latest.number
        if time.monotonic() >= self._batch_due:
            # due again a batch later whether this one succeeds or not: a failing batch isn't
            # retried every step
            self._batch_due = time.monotonic() + self._config.batch_seconds
            await self.close_batch(latest)
        slot = latest.timestamp + self.chain.slot_seconds + SLOT_OFFSET_SECONDS - time.time()
        return max(min(slot, self._batch_due - time.monotonic()), self._config.min_step_seconds)

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
        fees = self._fees(latest)
        sender = self._sender
        plans = await make_plans(
            self.chain,
            [t.submission for t in pending],
            schedule,
            measured.tokens,
            sender,
            measured.payee_holds,
        )
        ranked: list[tuple[Fraction, Tracked, Plan]] = []
        covered = await self._covered(measured, latest, pending, lambda t: t, plans, "ranking")
        for tracked, plan in covered:
            try:
                ranked.append((await self._profit(tracked, fees), tracked, plan))
            except _ITEM_ERRORS as e:  # this one waits; the rest go on
                logger.warning("ranking 0x%s: %s", tracked.submission.id.hex(), e)
        ranked.sort(key=lambda r: r[0], reverse=True)
        # every ranked one simulated together: the best that still simulate are chosen
        simulating = await self._still_simulate(
            [t for _, t, _ in ranked], [Simulation(t.submission, p, sender) for _, t, p in ranked]
        )
        chosen = [(t, p) for (_, t, p), ok in zip(ranked, simulating, strict=True) if ok]
        chosen = chosen[: self._config.accounts]
        if not chosen:
            self._batch = _Batch((), frozenset())
            return
        status = await survey(
            self.chain,
            self._float,
            self._config.accounts,
            latest.number,
            self.payee,
            payee_tokens(measured),
        )
        self.record_accounts(status)
        free = list(status.accounts)
        tip = self._tip(latest)
        ceiling = fee_ceiling(latest, self._config.quote_blocks)
        slots: list[_Slot] = []
        top_ups: list[TopUp] = []
        awaiting: set[int] = set()
        for tracked, plan in chosen:
            cost = ceiling.cost(plan.gas_limit, plan.size)
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
        funding = None
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
                funding = self._funding = _Funding(tx, frozenset(awaiting), time.monotonic())
        self._batch = _Batch(tuple(slots), frozenset(awaiting))
        if funding is not None:
            # no node answering leaves it in flight: each block sends it again
            try:
                await self.chain.rpc("eth_sendRawTransaction", ["0x" + funding.tx.raw.hex()])
            except JsonRpcError as e:  # refused, so not in flight: the next batch tries
                logger.warning("funding 0x%s refused: %s", funding.tx.hash.hex(), e)
                self._funding = None

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
            self._sender,
            measured.payee_holds,
        )
        covered = await self._covered(
            measured, latest, slots, lambda s: s.tracked, plans, "signing"
        )
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
            await self._settle_funding(funding, receipts[funding.tx.hash].value())

    def _settle_one(self, tracked: Tracked, target: int, receipts: Mapping[bytes, Answer]) -> None:
        for sent in tracked.sent:
            receipt = receipts[sent].value()
            if receipt is not None:
                tracked.transaction_hash = sent
                tracked.block_number = int(receipt["blockNumber"], 16)
                if receipt["status"] == "0x1":
                    self._finish(tracked, Status.INCLUDED)
                else:
                    self._finish(tracked, Status.REJECTED, "reverted on-chain")
                return
        if target > tracked.submission.max_block_number:
            self._finish(tracked, Status.EXPIRED)

    async def _settle_funding(self, funding: _Funding, receipt: dict[str, str] | None) -> None:
        """Whether the funding transaction in flight landed (its receipt, or None): its
        accounts are then ready. One that reverted, or wasn't seen in
        `funding_timeout_seconds`, is given up on; one still out is sent again."""
        if receipt is not None:
            self._funding = None
            if receipt["status"] == "0x1":
                self._batch = _Batch(self._batch.slots, self._batch.awaiting - funding.accounts)
            else:
                logger.error("funding 0x%s reverted", funding.tx.hash.hex())
        elif time.monotonic() - funding.sent_at > self._config.funding_timeout_seconds:
            self._funding = None
            logger.error("funding 0x%s not seen in time", funding.tx.hash.hex())
        else:
            # the same bytes again: a node that has them already says so, which is no failure
            with contextlib.suppress(JsonRpcError):
                await self.chain.rpc("eth_sendRawTransaction", ["0x" + funding.tx.raw.hex()])

    async def _still_simulate(
        self, tracked: Sequence[Tracked], sims: Sequence[Simulation]
    ) -> list[bool]:
        """Which of `sims` still simulate, in one batch; those that don't are rejected, with
        their revert data. One with a transaction out only sits out: it may have landed,
        which its receipt tells, and a landed call no longer simulates. So does one whose node
        hadn't the block: nothing is known of it until the next."""
        still: list[bool] = []
        for t, revert in zip(tracked, await simulations(self.chain, sims), strict=True):
            if isinstance(revert, BlockUnseenError):
                logger.warning("simulating 0x%s: %s", t.submission.id.hex(), revert)
            elif revert is not None and not t.sent:
                t.revert_data = revert[:MAX_REVERT_BYTES]
                self._finish(t, Status.REJECTED, "simulation failed")
            still.append(revert is None)
        return still

    async def _covered[T](
        self,
        measured: Measurements,
        latest: Latest,
        items: Sequence[T],
        tracked_of: Callable[[T], Tracked],
        plans: Sequence[Planned],
        doing: str,
    ) -> list[tuple[T, Plan]]:
        """Of `items`, each a submission tracked (`tracked_of`) and planned as `plans`, those
        whose amount covers their charge in the block after `latest`, with their plans; each
        kept its plan's gas limit and size. One whose plan failed simulation is rejected
        (_failed); one that can't be planned or priced sits out, logged as `doing`, and the
        rest go on."""
        covered: list[tuple[T, Plan]] = []
        for item, plan in zip(items, plans, strict=True):
            one = tracked_of(item)
            try:
                if isinstance(plan, SimulationFailedError):
                    self._failed(one, plan)
                    continue
                if isinstance(plan, Exception):
                    raise plan
                one.gas_limit, one.size = plan.gas_limit, plan.size
                charge = await self._charge(measured, latest, one.submission.token, plan)
                if one.submission.amount >= charge:
                    covered.append((item, plan))
            except _ITEM_ERRORS as e:
                logger.warning("%s 0x%s: %s", doing, one.submission.id.hex(), e)
        return covered

    @property
    def _sender(self) -> ChecksumAddress:
        """The float's account 0: what a submission's transaction is planned and simulated as
        sent from."""
        return derive(self._float, 0).address

    def _failed(self, tracked: Tracked, failed: SimulationFailedError) -> None:
        """`tracked` rejected, its estimate having reverted with `failed`'s revert data, as a
        simulation that fails is; unless its transaction is out, when it only sits out (it may
        have landed)."""
        if not tracked.sent:
            tracked.revert_data = failed.revert_data[:MAX_REVERT_BYTES]
            self._finish(tracked, Status.REJECTED, "simulation failed")

    async def _profit(self, tracked: Tracked, fees: Fees) -> Fraction:
        """The submission's amount in wei, less its transaction's cost at `fees`."""
        wei = await self._prices.wei(tracked.submission.token)
        return tracked.submission.amount * wei - fees.cost(tracked.gas_limit, tracked.size)

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
        """The fees of the block after `latest`: exactly its base fee, and the tip; what's paid
        beyond gas risen for the one block (its L1 inputs may be a new L1 block's)."""
        tip = self._tip(latest)
        data_fee = None if latest.data_fee is None else latest.data_fee.risen(1)
        return Fees(latest.next_base_fee + tip, tip, data_fee)

    async def _latest(self) -> Latest:
        """The latest block, with what's paid beyond gas on an OP-stack chain: measured first
        (from the last measurements, read again only once they're old), so it's known which."""
        try:
            measured = await self.measurements()
        except UnavailableError:  # nothing priced or sent until it's measured
            return await self.chain.latest()
        return await self.chain.latest(measured.op_stack)

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
        self,
        measured: Measurements,
        latest: Latest,
        token: ChecksumAddress,
        plan: Plan,
        fees: Fees | None = None,
    ) -> int:
        """What `plan`'s transaction is charged in `token`: its cost at `fees` (the fees of the
        block after `latest`, if not given), and our fee, margins included."""
        cost = (self._fees(latest) if fees is None else fees).cost(plan.gas_limit, plan.size)
        fee = await self._our_fee(measured, latest, plan.gas_limit)
        return await self._price(token, cost + fee)

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


def payee_tokens(measured: Measurements) -> dict[ChecksumAddress, tuple[int, str]]:
    """Each measured token but ETH, and its decimals and symbol: what the payee's holdings are
    read of."""
    return {
        t: (measured.decimals[t], measured.symbols[t]) for t in measured.tokens if t != NATIVE_ASSET
    }


def _schedule(measured: Measurements) -> GasSchedule:
    """The measured schedule of an available chain."""
    if measured.schedule is None:
        raise UnavailableError(measured.unavailable or "no schedule measured")
    return measured.schedule
