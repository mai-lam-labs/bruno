"""The relay on one chain: what it offers, its quotes, the submissions it takes, and its clock;
it holds its queue, pricing, measuring and batches (batches.py), each owning its own state.

A submission is checked when it's taken (its token, payee, limits, a deadline at most
`max_deadline_blocks` ahead, its amount against the next block's price, and a simulation),
then queued: at most `max_pending`, a full queue taking a submission only by evicting its
least profitable. Queued submissions never hold an account."""

import asyncio
import contextlib
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import httpx
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress

from .accounts import read_key
from .batches import Batches
from .builders import Builder
from .chain import (
    Chain,
    Finality,
    Latest,
)
from .common import BrunoError
from .config import ChainConfig, ConfigError, Eip712Domain, Misconfiguration
from .eip3009 import UnsignedTransfer, last_block_before
from .evm import NATIVE_ASSET
from .measurements import (
    Measurements,
    Measuring,
    Unavailability,
    UnavailableError,
    schedule_of,
)
from .plan import (
    BlockUnseenError,
    Proof,
    make_plan,
    simulate,
)
from .prices import Prices
from .pricing import (
    NO_PAYER,
    Payer,
    Pricing,
    QuotedCall,
    Quotes,
    TokenQuote,
    Unsupported,
    UnsupportedError,
)
from .refusals import Refusal, message
from .submission import (
    Bound,
    InvalidSubmissionError,
    Submission,
    check_authorizations,
)
from .tracked import Queue, Rejection, Status, Tracked
from .transactions import Fees
from .units import Millis, Seconds

logger = logging.getLogger(__name__)

# how long after a slot's start its block is looked for
SLOT_OFFSET = Seconds(1)
# how soon a step that fails (the nodes not answering, say) is tried again: twice as long after
# each failure in a row, up to MAX_RETRY
RETRY = Seconds(1)
MAX_RETRY = Seconds(300)


class RelayError(BrunoError):
    pass


@dataclass(frozen=True, slots=True)
class _Recent[T]:
    """What was last read of the chain, and when the next block, which may change it, is due
    (by the wall clock, as block timestamps are)."""

    read: T
    due: Millis

    @classmethod
    def of(cls, read: T, timestamp: int, slot: Seconds) -> _Recent[T]:
        """`read` as of the block at `timestamp`: the next is due a slot and SLOT_OFFSET on."""
        return cls(read, Seconds(timestamp + slot + SLOT_OFFSET).millis)

    @property
    def fresh(self) -> bool:
        return Millis.unix() < self.due


class UnderpaidError(RelayError):
    def __init__(self, token: ChecksumAddress, required: int) -> None:
        super().__init__(message(Refusal.UNDERPAID, required=required, token=token))
        self.token = token
        self.required = required


class NoCapacityError(RelayError):
    """The queue is full of more profitable submissions: `required` of `token` would beat
    the least profitable of them."""

    def __init__(self, token: ChecksumAddress, required: int) -> None:
        super().__init__(message(Refusal.NO_CAPACITY, required=required, token=token))
        self.token = token
        self.required = required


@dataclass(frozen=True, slots=True)
class Terms:
    """What a client needs before it signs: who to pay, in what, and the limits."""

    payee: ChecksumAddress
    tokens: tuple[ChecksumAddress, ...]
    max_gas: int
    max_call_bytes: int


@dataclass(frozen=True, slots=True)
class Accepted:
    """A submission's id, and whether it's new: one already taken is taken once."""

    id: bytes
    new: bool


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
        self._prices = prices
        self._builders = builders
        self._client = client
        self.payee = payee
        # each token taken over x402, and its EIP-712 domain, as configured
        self.domains: Mapping[ChecksumAddress, Eip712Domain] = {
            t.address: t.eip3009
            for t in config.tokens
            if t.eip3009 is not None and t.address != NATIVE_ASSET
        }
        # what a call costs, and whether it fits
        self.pricing = Pricing(chain, config, prices, payee)
        # the submissions taken, pending and finished
        self.queue = Queue(Millis.of_seconds(config.retention_seconds))
        # what's known of the chain as last read
        self.measuring = Measuring(chain, config, payee, self.domains)
        # each batch, mapped onto the concurrency accounts, and the funding in flight
        self.batches = Batches(
            chain,
            config,
            float_account,
            builders,
            client,
            payee,
            self.queue,
            self.pricing,
            self.measuring,
        )
        # the relay's own clock, once started: running while there's work
        self._clocked = False
        self._clock: asyncio.Task[None] | None = None
        # whether the clock is only waiting to forget finished submissions
        self._idle = False
        self._head: int | None = None
        self._batch_due = Millis(0)
        # the latest block as the x402 challenge last read it
        self._challenged: _Recent[Latest] | None = None
        # the latest and finalized blocks as a status last read them
        self._final: _Recent[Finality] | None = None

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

    @property
    def x402_timeout_seconds(self) -> int:
        """How long an x402 payment's request may be held: its deadline's furthest."""
        return self._config.x402_deadline_blocks * self.chain.slot

    def terms(self) -> Terms:
        return Terms(
            payee=self.payee,
            tokens=tuple(t.address for t in self._config.tokens),
            max_gas=self._config.max_gas,
            max_call_bytes=self._config.max_call_bytes,
        )

    async def quote(
        self,
        quoted: QuotedCall,
        tokens: Sequence[ChecksumAddress],
        payer: Payer = NO_PAYER,
    ) -> Quotes:
        """What `quoted` costs in each of `tokens`, its gas estimated if not given: enough for
        the next base fee risen as far as it can in `quote_blocks` blocks and the tip doubled,
        and our fee; paid by a transfer from `payer`, if it names one, its deployment made
        first."""
        self._check(quoted)
        measured = await self.measuring.available()
        for token in tokens:
            self.pricing.supported(measured, token)
        latest = await self.measuring.latest()
        signing = await self.pricing.signing(
            measured, payer, transferring=payer.address is not None
        )
        quotes = await self.pricing.quote_at(
            measured, latest, quoted, tokens, signing, self.batches.sender
        )
        if payer.address is None:
            return quotes
        return replace(
            quotes,
            quotes=tuple(self._to_sign(q, payer.address, latest, measured) for q in quotes.quotes),
        )

    def _to_sign(
        self, quote: TokenQuote, payer: ChecksumAddress, latest: Latest, measured: Measurements
    ) -> TokenQuote:
        """`quote` with the transfer its payer signs, if its token takes one: to the payee, of the
        amount, valid from now for the quote's blocks and the longest deadline, its nonce
        fresh."""
        if quote.token not in self.x402_tokens(measured):
            return quote
        lasting = (self._config.quote_blocks + self._config.max_deadline_blocks) * self.chain.slot
        transfer = UnsignedTransfer(
            domain=self.domains[quote.token],
            chain_id=self.chain.chain_id,
            token=quote.token,
            payer=payer,
            to=self.payee,
            value=quote.amount,
            valid_after=0,
            valid_before=latest.timestamp + lasting,
            nonce=os.urandom(32),
        )
        return replace(quote, transfer=transfer)

    async def x402_quote(
        self, quoted: QuotedCall, payer: Payer = NO_PAYER, *, existing: bool = False
    ) -> Quotes:
        """`quote` in each token accepted over x402, the runner making its transfer, from
        `payer` as far as it's known: as of the latest block, read at most once a slot however
        many ask. `existing`: the authority is taken to exist, unread."""
        self._check(quoted)
        measured = await self.measuring.available()
        latest = await self._recent_latest()
        signing = await self.pricing.signing(measured, payer, transferring=True)
        return await self.pricing.quote_at(
            measured,
            latest,
            quoted,
            self.x402_tokens(measured),
            signing,
            self.batches.sender,
            existing=existing,
        )

    async def x402_gas(self, quoted: QuotedCall, payer: Payer) -> int:
        """The gas an x402 payment's call is given: its own, or its estimate, as its challenge
        priced it, estimated again now it's paid."""
        self._check(quoted)
        measured = await self.measuring.available()
        signing = await self.pricing.signing(measured, payer, transferring=True)
        tokens = self.x402_tokens(measured)
        return (
            await self.pricing.priced(measured, quoted, tokens, signing, self.batches.sender)
        ).gas

    def _check(self, quoted: QuotedCall) -> None:
        """A quoted call's own checks, as a submission's: its length its call's, its
        authorizations signed or their signer given, not both, and within the limits."""
        call = quoted.call
        if call is not None:
            length = len(bytes.fromhex(call.data.removeprefix("0x")))
            if length != quoted.call_data_length:
                raise InvalidSubmissionError(
                    "callDataLength",
                    message(
                        Refusal.CALL_LENGTH_MISMATCH, given=quoted.call_data_length, length=length
                    ),
                )
        if quoted.authority is not None and quoted.authorizations:
            raise InvalidSubmissionError("authority", message(Refusal.AUTHORITY_AND_LIST))
        check_authorizations(
            quoted.authorizations, self.chain.chain_id, self._config.max_authorizations
        )
        self.pricing.within_limits(quoted.gas or 0, quoted.call_data_length, quoted.length_field)

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
        if valid_after >= latest.timestamp + self.chain.slot:
            raise InvalidSubmissionError("validAfter", message(Refusal.VALID_AFTER))
        last = last_block_before(latest, valid_before, self.chain.slot)
        if last <= latest.number:
            raise InvalidSubmissionError("validBefore", message(Refusal.VALID_BEFORE))
        return min(last, latest.number + self._config.x402_deadline_blocks)

    async def _recent_latest(self) -> Latest:
        """The latest block, read again only once the next is due: the x402 challenge is
        answered to anyone who asks, probes included."""
        if self._challenged is not None and self._challenged.fresh:
            return self._challenged.read
        latest = await self.measuring.latest()
        self._challenged = _Recent.of(latest, latest.timestamp, self.chain.slot)
        return latest

    async def submit(
        self,
        submission: Submission,
        proof: Proof,
        user_operation: bytes | None = None,
    ) -> Accepted:
        """Take `submission`, paying as `proof` has it, checked; or raise why not.
        `user_operation`: the hash of the UserOp it sends, if it's one."""
        data = bytes.fromhex(submission.call.data.removeprefix("0x"))
        self.pricing.within_limits(submission.gas, len(data))
        measured = await self.measuring.available()
        self.pricing.supported(measured, submission.token)
        if submission.payee != self.payee:
            raise UnsupportedError(
                Unsupported.WRONG_PAYEE,
                message(Refusal.WRONG_PAYEE, payee=self.payee, given=submission.payee),
            )
        check_authorizations(
            submission.authorizations, self.chain.chain_id, self._config.max_authorizations
        )
        transfer = submission.transfer
        if transfer is not None:
            if submission.token not in self.x402_tokens(measured):
                raise UnsupportedError(
                    Unsupported.NO_EIP3009,
                    message(Refusal.NO_EIP3009, token=submission.token),
                )
            if transfer.to != submission.payee or transfer.value != submission.amount:
                raise InvalidSubmissionError(
                    "payment",
                    message(Refusal.TRANSFER_NOT_PAYMENT),
                )
        id_ = submission.id
        if self.queue.known(id_):
            return Accepted(id_, new=False)
        latest = await self.measuring.latest()
        head = latest.number
        if submission.max_block_number <= head:
            raise InvalidSubmissionError("maxBlockNumber", message(Refusal.DEADLINE_PASSED))
        if submission.max_block_number > head + self._config.max_deadline_blocks:
            ahead = self._config.max_deadline_blocks
            raise InvalidSubmissionError(
                "maxBlockNumber",
                message(Refusal.DEADLINE_FAR, ahead=ahead),
                Bound(head + ahead),
            )
        schedule = schedule_of(measured)
        sender = self.batches.sender
        plan = await make_plan(
            self.chain, submission, proof, schedule, measured.tokens, sender, measured.payee_holds
        )
        self.pricing.fits(plan)
        fees = self.pricing.fees(latest)
        required = await self.pricing.charge(measured, latest, submission.token, plan)
        tracked = Tracked(
            submission, plan.gas_limit, plan.size, proof, user_operation=user_operation
        )
        if tracked.paying(latest.next_base_fee) < required:
            raise UnderpaidError(submission.token, required)
        try:
            await simulate(self.chain, submission, plan, sender)
        except BlockUnseenError as e:  # nothing known of it yet: the client tries again
            raise UnavailableError(Unavailability.BLOCK_UNSEEN, str(e)) from e
        async with self.queue.lock:
            if self.queue.known(id_):  # taken while this one was checked
                return Accepted(id_, new=False)
            queue = self.queue.pending()
            if len(queue) >= self._config.max_pending:
                await self._make_room(tracked, queue, fees, latest.next_base_fee)
            self.queue.add(tracked)
        self._wake()
        return Accepted(id_, new=True)

    async def _make_room(
        self, tracked: Tracked, queue: list[Tracked], fees: Fees, base_fee: int
    ) -> None:
        """Evict the least profitable of `queue` for `tracked`, if `tracked` is more profitable;
        or raise NoCapacityError with what would be taken. A submission whose transaction is
        out isn't evicted: it may land."""
        profits = [(await self.pricing.profit(t, fees, base_fee), t) for t in queue]
        evictable = [(p, t) for p, t in profits if not t.sent]
        worst_profit, worst = min(evictable or profits, key=lambda p: p[0])
        if not evictable or await self.pricing.profit(tracked, fees, base_fee) <= worst_profit:
            submission = tracked.submission
            cost = fees.cost(tracked.gas_limit, tracked.size)
            wei = await self._prices.wei(submission.token)
            raise NoCapacityError(submission.token, (worst_profit + cost) // wei + 1)
        self.queue.finish(worst, Status.REJECTED, Rejection.OUTBID)

    async def finality(self) -> Finality:
        """The latest and finalized blocks, read again only once the next block is due: a
        status's confirmations, however many ask."""
        if self._final is not None and self._final.fresh:
            return self._final.read
        final = await self.chain.finality()
        self._final = _Recent.of(final, final.timestamp, self.chain.slot)
        return final

    @property
    def busy(self) -> bool:
        """Whether there's work for the next block: a submission pending, or funding in
        flight."""
        return self.batches.funding_in_flight or bool(self.queue.pending())

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
                self.queue.prune()
                forget_at = self.queue.forget_at()
                if forget_at is None:
                    return
                self._idle = True
                try:
                    await asyncio.sleep(Millis(max(forget_at - Millis.monotonic(), 0)).seconds)
                finally:
                    self._idle = False
                continue
            try:
                delay = await self._step()
                failures = 0
            except Exception:
                logger.exception("chain %d: a slot's work", self.chain.chain_id)
                delay = Seconds(min(RETRY * 2**failures, MAX_RETRY)).millis
                failures += 1
            await asyncio.sleep(delay.seconds)

    async def _step(self) -> Millis:
        """The block, if there's a new one, and the batch, if it's due, both as of one read of
        the latest block: how long until the next step."""
        latest = await self.measuring.latest()
        if latest.number != self._head:
            await self.batches.on_block(latest)
            self._head = latest.number
        if Millis.monotonic() >= self._batch_due:
            # due again a batch later whether this one succeeds or not: a failing batch isn't
            # retried every step
            batch = Millis.of_seconds(self._config.batch_seconds)
            self._batch_due = Millis(Millis.monotonic() + batch)
            await self.batches.close_batch(latest)
        to_slot = Seconds(latest.timestamp + self.chain.slot + SLOT_OFFSET).millis - Millis.unix()
        to_batch = self._batch_due - Millis.monotonic()
        return Millis(max(min(to_slot, to_batch), Millis.of_seconds(self._config.min_step_seconds)))


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
        why = f"chain {chain_id}: none (its own, the configuration's, or --payee)"
        raise ConfigError(Misconfiguration.NO_PAYEE, "payee", why)
    for i, token in enumerate(config.tokens):
        address = token.address
        if address == NATIVE_ASSET:
            if token.price:
                why = "ETH is priced at 1 wei a wei: it has no pools"
                raise ConfigError(Misconfiguration.INVALID, f"tokens[{i}].price", why)
        elif not token.price and address != config.weth:
            why = f"no pools to price {address} by"
            raise ConfigError(Misconfiguration.INVALID, f"tokens[{i}].price", why)
    if config.min_fee is not None and config.min_fee.token not in {
        t.address for t in config.tokens
    }:
        why = f"{config.min_fee.token} isn't one of the chain's tokens"
        raise ConfigError(Misconfiguration.INVALID, "min_fee.token", why)
    chain = Chain(client, chain_id, config.rpc, Seconds(config.slot_seconds))
    prices = Prices(
        chain,
        config.weth,
        {t.address: t.price for t in config.tokens},
        Seconds(config.price_ttl_seconds),
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
        config.payee,
    )
