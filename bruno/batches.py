"""Each batch and each block on one chain: the queue's best mapped onto concurrency accounts,
funded from the float, and their transactions signed and sent each block, and what was sent
reconciled with its receipts.

Every `batch_seconds` a batch is made: the queue ranked by profit (the amount in wei less the
transaction's cost at the next block's fee), the top `accounts` that still simulate and
cover the price mapped onto concurrency accounts, each to the lowest-numbered free account
that already covers it, else the lowest-numbered free account, topped up from the float to
`funding_multiplier` times its transaction's cost. One funding transaction at a time, not
waited for: a bundle of its own, signed again each block at the float's nonce until it lands,
and its accounts join the batch's blocks once it has.

Each block, each of the batch's submissions on a ready account is simulated again, checked
against that block's price and its account's balance, signed at the account's nonce, and
sent to every builder in its mode. The next batch replaces the mapping; a submission that
hasn't landed is a queue entry again."""

import asyncio
import contextlib
import logging
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, replace
from fractions import Fraction
from math import ceil

import httpx
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress

from .accounts import derive
from .builders import Builder, send_bundle
from .bundle import BundleParams, TopUp, atomic_params, bundles, funding_json, sign_funding
from .chain import (
    Chain,
    ChainError,
    Latest,
    quantity,
    receipt_of,
)
from .common import BrunoError
from .config import ChainConfig
from .evm import NATIVE_ASSET
from .forks import GasSchedule
from .jsonrpc import Answer, Call, JsonRpcError, result_of
from .maintenance import Denomination, monitor, survey
from .measurements import (
    Measurements,
    Measuring,
    UnavailableError,
    schedule_of,
)
from .plan import (
    Plan,
    Planned,
    SimulationFailedError,
    Step,
    make_plans,
    plan_of,
)
from .prices import PriceError
from .pricing import Pricing, UnsupportedError, fee_ceiling
from .simulation import (
    BlockUnseenError,
    SignedSimulation,
    Simulating,
    Simulation,
    fails,
    simulations,
)
from .sponsorship import Sponsorship
from .submission import (
    InvalidSubmissionError,
)
from .tracked import MAX_REVERT_BYTES, SENT_KEPT, Queue, Rejection, Status, Tracked
from .transactions import SignedTransaction, TransactionError, sign

logger = logging.getLogger(__name__)

# what one submission's work can fail with, without failing the others' in its batch or block
_ITEM_ERRORS = (
    TransactionError,
    PriceError,
    ChainError,
    JsonRpcError,
    httpx.HTTPError,
    UnsupportedError,
    UnavailableError,
    InvalidSubmissionError,
    SimulationFailedError,
)


class FundingFailsError(BrunoError):
    """The float's funding fails, simulated before it's sent: a bug of ours (its gas schedule
    short, say), so it isn't sent."""

    def __init__(self, accounts: int) -> None:
        super().__init__(f"the funding of {accounts} accounts fails, simulated: not sent")


@dataclass(frozen=True, slots=True)
class _Slot:
    """A batch's submission, on concurrency account `account`."""

    tracked: Tracked
    account: int


@dataclass(frozen=True, slots=True)
class _Run:
    """A tracked submission, and its transactions' simulation (as planned, or as signed)."""

    tracked: Tracked
    simulation: Simulating


@dataclass(frozen=True, slots=True)
class _Batch:
    """A batch's submissions on their accounts, and the accounts still awaiting the funding
    transaction."""

    slots: tuple[_Slot, ...]
    awaiting: frozenset[int]


@dataclass(frozen=True, slots=True)
class _Funding:
    """The float's funding, not yet landed: its top-ups, the accounts they're for, and the
    float's nonce it's signed at; the transactions sent for it (the latest SENT_KEPT, one a
    block, each signed at that block's fees), and the block it was last sent for."""

    top_ups: tuple[TopUp, ...]
    accounts: frozenset[int]
    nonce: int
    sent: tuple[bytes, ...] = ()
    sent_for: int = 0


class Batches:
    def __init__(
        self,
        chain: Chain,
        config: ChainConfig,
        float_account: LocalAccount,
        builders: Sequence[Builder],
        client: httpx.AsyncClient,
        payee: ChecksumAddress,
        queue: Queue,
        pricing: Pricing,
        measuring: Measuring,
    ) -> None:
        self._chain = chain
        self._config = config
        self._float = float_account
        self._builders = builders
        self._client = client
        self._payee = payee
        self._queue = queue
        self._pricing = pricing
        self._measuring = measuring
        self._funding_multiplier = Fraction(config.funding_multiplier)
        self._batch = _Batch((), frozenset())
        self._funding: _Funding | None = None

    @property
    def funding_in_flight(self) -> bool:
        """Whether the float's funding is out, not yet landed."""
        return self._funding is not None

    @contextlib.contextmanager
    def _item(self, what: str) -> Generator[None]:
        """One submission's work, `what`: failing, the rest go on. What it may meet is logged;
        anything else is a bug of ours, logged and listed with the chain's problems, so it's
        seen. As the Worker's itemFailed."""
        try:
            yield
        except _ITEM_ERRORS as e:
            logger.warning("%s: %s", what, e)
        except Exception as e:
            logger.exception("%s: internal error", what)
            self._measuring.internal_error(what, e)

    async def close_batch(self, latest: Latest) -> None:
        """Make the next batch, as of `latest`: rank the queue, take the top `accounts` that
        still simulate and cover the next block's price, map them onto accounts, and send the
        float's funding for those that need it, for the next block (unless a funding is in
        flight: they wait for the next batch). Nothing queued, nothing read. The queue's reads, its
        simulations and the accounts' state are each one batch."""
        pending = self._queue.pending()
        if not pending:
            self._batch = _Batch((), frozenset())
            return
        measured = await self._measuring.measurements()
        if measured.unavailable is not None:  # nothing sent: the queue waits, to its deadlines
            self._batch = _Batch((), frozenset())
            return
        schedule = schedule_of(measured)
        fees = self._pricing.fees(latest)
        sender = self.sender
        plans = await make_plans(
            self._chain,
            [t.submission for t in pending],
            [t.proof for t in pending],
            schedule,
            measured.tokens,
            sender,
            measured.payee_holds,
            payee_new=measured.payee_new,
        )
        ranked: list[tuple[Fraction, Tracked, Plan]] = []
        covered = await self._covered(measured, latest, pending, lambda t: t, plans, "ranking")
        for tracked, plan in covered:
            with self._item(f"ranking 0x{tracked.submission.id.hex()}"):
                profit = await self._pricing.profit(tracked, fees, latest.next_base_fee)
                ranked.append((profit, tracked, plan))
        ranked.sort(key=lambda r: r[0], reverse=True)
        # every ranked one simulated together: the best that still simulate are chosen
        simulating = await self._still_simulate(
            [_Run(t, Simulation(t.submission, p, sender)) for _, t, p in ranked]
        )
        chosen = [(t, p) for (_, t, p), ok in zip(ranked, simulating, strict=True) if ok]
        chosen = chosen[: self._config.accounts]
        if not chosen:
            self._batch = _Batch((), frozenset())
            return
        status = await survey(
            self._chain,
            self._float,
            self._config.accounts,
            latest.number,
            self._payee,
            payee_tokens(measured),
        )
        self._measuring.record_accounts(status)
        free = list(status.accounts)
        ceiling = fee_ceiling(latest, self._config.quote_blocks)
        slots: list[_Slot] = []
        top_ups: list[TopUp] = []
        awaiting: set[int] = set()
        for tracked, plan in chosen:
            if tracked.status is not Status.PENDING:  # evicted while the accounts were read
                continue
            cost = plan.cost(ceiling)
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
        self._batch = _Batch(tuple(slots), frozenset(awaiting))
        if top_ups:
            funding = _Funding(tuple(top_ups), frozenset(awaiting), status.float_nonce)
            self._funding = funding
            await self._send_funding(funding, latest, schedule, status.float_balance)

    async def on_block(self, latest: Latest) -> None:
        """`latest` is the latest block: reconcile what was sent for it and the funding in
        flight, then send the funding again if it hasn't landed, and the batch's bundles, for
        the next. Each slot's transactions are signed if they cover the block's price, their
        account covers their cost, and they still simulate as they'll be signed (from the
        account, at its nonces and the block's fees, validated); the receipts, the slots' reads,
        their accounts' state and their simulations are each one batch, and none is read when
        there's none to read."""
        target = latest.number + 1
        self._queue.prune()
        await self._reconcile(latest)
        slots = [
            s
            for s in self._batch.slots
            if s.tracked.status is Status.PENDING and s.account not in self._batch.awaiting
        ]
        if not slots:
            return
        measured = await self._measuring.measurements()
        if measured.unavailable is not None:  # reconciled, but nothing sent
            return
        schedule = schedule_of(measured)
        fees = self._pricing.fees(latest)
        plans = await make_plans(
            self._chain,
            [s.tracked.submission for s in slots],
            [s.tracked.proof for s in slots],
            schedule,
            measured.tokens,
            self.sender,
            measured.payee_holds,
            payee_new=measured.payee_new,
        )
        covered = await self._covered(
            measured, latest, slots, lambda s: s.tracked, plans, "signing"
        )
        if not covered:
            return
        states = await monitor(
            self._chain, self._float, [s.account for s, _ in covered], hex(latest.number)
        )
        # an account short of its plan's cost: fees rose past its funding, the next batch tops
        # it up
        funded = [
            (slot, plan, state)
            for (slot, plan), state in zip(covered, states, strict=True)
            if state.balance >= plan.cost(fees)
        ]
        # each as it'll be signed: from its account, at its nonces, at the block's fees
        simulating = await self._still_simulate(
            [
                _Run(
                    slot.tracked,
                    SignedSimulation(
                        slot.tracked.submission,
                        plan,
                        derive(self._float, slot.account).address,
                        state.nonce,
                        fees,
                    ),
                )
                for slot, plan, state in funded
            ]
        )
        ready = [f for f, ok in zip(funded, simulating, strict=True) if ok]
        signed: list[tuple[SignedTransaction, ...]] = []
        for slot, plan, state in ready:
            tracked = slot.tracked
            if tracked.status is not Status.PENDING:  # evicted while it was checked
                continue
            with self._item(f"signing 0x{tracked.submission.id.hex()}"):
                # its plan's transactions, in order: its own at its account's consecutive
                # nonces, another account's as it signed them
                txs: list[SignedTransaction] = []
                own: list[bytes] = []
                for step in plan.steps:
                    if not isinstance(step, Step):
                        txs.append(step.transaction.signed)
                        continue
                    tx = sign(
                        derive(self._float, slot.account),
                        self._chain.chain_id,
                        state.nonce + len(own),
                        fees,
                        step.gas_limit,
                        step.transaction.to,
                        step.transaction.data,
                        step.transaction.value,
                        authorizations=step.authorizations,
                    )
                    txs.append(tx)
                    own.append(tx.hash)
                tracked.sent = (*tracked.sent, *own)[-SENT_KEPT * len(own) :]
                tracked.attempts += 1
                signed.append(tuple(txs))
        if signed:
            await self._send(target, signed)

    async def _send(self, target: int, signed: Sequence[Sequence[SignedTransaction]]) -> None:
        """Every builder its bundles for block `target` of the submissions' transactions as
        `signed`. A submission of one transaction goes in the builder's bundles of them, where
        it may be left out alone. One of several is a bundle of its own, with nothing a builder
        may leave out: it lands whole or not at all."""
        alone = [txs[0] for txs in signed if len(txs) == 1]
        together = [txs for txs in signed if len(txs) > 1]
        sends: list[tuple[Builder, BundleParams]] = []
        for builder in self._builders:
            if alone:
                sends += [(builder, p) for p in bundles(builder.mode, target, None, alone)]
            sends += [(builder, atomic_params(target, txs)) for txs in together]
        await self._offer(target, sends)

    async def _send_funding(
        self, funding: _Funding, latest: Latest, schedule: GasSchedule, float_balance: int
    ) -> None:
        """`funding` signed at the float's nonce and the fees of the block after `latest`, and
        sent to every builder for that block, a bundle of its own in either mode: only one
        transaction at the nonce can land, so only one of those sent for it. Not sent again for
        a block it's been sent for. One the float, holding `float_balance`, can't cover isn't
        sent and is dropped: the next batch funds what's needed then, the float perhaps topped
        up. Before it's first sent it's simulated, at `latest`: one that fails is a bug of ours,
        not sent to fail at every builder, dropped and reported (FundingFailsError). Nothing sent
        for an earlier block can still land."""
        target = latest.number + 1
        if funding.sent_for >= target:
            return
        tx = sign_funding(
            self._float,
            self._chain.chain_id,
            funding.nonce,
            self._pricing.fees(latest),
            funding.top_ups,
            schedule,
        )
        if float_balance < tx.max_cost:
            logger.warning(
                "funding: the float holds %d wei, the funding %d", float_balance, tx.max_cost
            )
            self._funding = None
            return
        if not funding.sent:
            transaction = funding_json(self._float.address, funding.top_ups, schedule)
            if await fails(self._chain, transaction, latest.number):
                e = FundingFailsError(len(funding.top_ups))
                logger.error("funding: %s", e)
                self._measuring.internal_error("funding", e)
                self._funding = None
                return
        # recorded before it's sent: a copy sent is always looked for
        self._funding = replace(
            funding, sent=(*funding.sent, tx.hash)[-SENT_KEPT:], sent_for=target
        )
        await self._offer(
            target, [(builder, atomic_params(target, [tx])) for builder in self._builders]
        )

    async def _offer(self, target: int, sends: Sequence[tuple[Builder, BundleParams]]) -> None:
        """Each bundle in `sends` to its builder, for block `target`: one refused is logged."""
        results = await asyncio.gather(
            *(send_bundle(self._client, b, params) for b, params in sends),
            return_exceptions=True,
        )
        for (builder, _), result in zip(sends, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("block %d: %s refused a bundle: %s", target, builder.url, result)

    async def _reconcile(self, latest: Latest) -> None:
        """Whether each pending submission's latest transactions landed, and the funding in
        flight, as of `latest`: every receipt looked for in one batch, with the float's nonce,
        and its balance if there's a funding. Receipts take no block, but the nonce is read at
        `latest`, and a node without that block fails it: so the receipts are a node's that has
        the block, never one behind it with none of the block's yet, and a nonce the funding
        moved has its receipt there to be found. Each transaction is looked for again until
        it's SENT_KEPT sends old. A submission past its deadline, unlanded, expires; a funding
        not landed is sent again for the next block."""
        target = latest.number + 1
        pending = self._queue.pending()
        funding = self._funding
        hashes = [h for t in pending for h in (*t.sent, *_signed_hashes(t))]
        methods: tuple[str, ...] = ("eth_getTransactionCount",) if hashes else ()
        if funding is not None:
            hashes.extend(funding.sent)
            methods = ("eth_getTransactionCount", "eth_getBalance")
        floats = [Call(method, [self._float.address, hex(latest.number)]) for method in methods]
        answers = await self._chain.batch(
            [*(Call("eth_getTransactionReceipt", ["0x" + h.hex()]) for h in hashes), *floats],
            atomic=True,
        )
        receipts = dict(zip(hashes, answers[: len(hashes)], strict=True))
        # the receipts are as of `latest` only if the nonce beside them is: read it first
        nonce = quantity(result_of(answers[len(hashes)])) if floats else 0
        for tracked in pending:
            if tracked.status is not Status.PENDING:  # evicted while the receipts were read
                continue
            with self._item(f"reconciling 0x{tracked.submission.id.hex()}"):
                self._reconcile_one(tracked, target, receipts)
        if funding is not None:
            balance = quantity(result_of(answers[len(hashes) + 1]))
            if self._reconcile_funding(funding, receipts, nonce):
                measured = await self._measuring.measurements()
                if measured.unavailable is None:
                    await self._send_funding(funding, latest, schedule_of(measured), balance)

    def _reconcile_one(
        self, tracked: Tracked, target: int, receipts: Mapping[bytes, Answer]
    ) -> None:
        """Whether one of `tracked`'s sends landed: each send is its plan's transactions, as
        many as its basis has. Included if every one of a send's landed and succeeded, its
        transaction then its last, which lands only after the others (its account's nonces);
        reverted if one that landed failed. If some landed, each succeeding, and not the rest,
        a builder broke a bundle it was to land whole or not at all: it's rejected as that,
        nothing more of it sent, and listed with the chain's problems with the ETH those that
        landed sent. What became of it is decided by the relay's own transactions; its hashes
        are every transaction of it that landed, a sponsored account's own among them, which
        follow the relay's first."""
        steps = len(tracked.basis)
        for first in range(0, len(tracked.sent), steps):
            send = tracked.sent[first : first + steps]
            landed = [(h, receipt_of(result_of(receipts[h]))) for h in send]
            found = [(h, receipt) for h, receipt in landed if receipt is not None]
            if found:
                _, last = found[-1]
                theirs = [
                    h
                    for h in _signed_hashes(tracked)
                    if receipt_of(result_of(receipts[h])) is not None
                ]
                own = [h for h, _ in found]
                tracked.transaction_hashes = (own[0], *theirs, *own[1:])
                tracked.block_number = last.block_number
                if any(receipt.status != 1 for _, receipt in found):
                    self._queue.finish(tracked, Status.REJECTED, Rejection.REVERTED)
                elif len(found) == steps:
                    self._queue.finish(tracked, Status.INCLUDED)
                else:
                    sent = sum(basis.value for basis in tracked.basis[: len(found)])
                    self._measuring.broken_bundle(
                        f"0x{tracked.submission.id.hex()}: {len(found)} of its {steps} "
                        f"transactions landed, {sent} wei sent"
                    )
                    self._queue.finish(tracked, Status.REJECTED, Rejection.BUNDLE_BROKEN)
                return
        if target > tracked.submission.max_block_number:
            self._queue.finish(tracked, Status.EXPIRED)

    def _reconcile_funding(
        self, funding: _Funding, receipts: Mapping[bytes, Answer], float_nonce: int
    ) -> bool:
        """Whether the funding in flight is still to land, by its transactions' `receipts` and
        the float's nonce. Landed, its accounts are ready. One whose nonce the float has used
        for something else is dropped: the next batch funds what's needed then."""
        for sent in funding.sent:
            receipt = receipt_of(result_of(receipts[sent]))
            if receipt is not None:
                self._funding = None
                if receipt.status == 1:
                    awaiting = self._batch.awaiting - funding.accounts
                    self._batch = _Batch(self._batch.slots, awaiting)
                else:
                    logger.error("funding 0x%s reverted", sent.hex())
                return False
        if float_nonce > funding.nonce:
            self._funding = None
            logger.error(
                "funding: the float's nonce %d was used by another transaction", funding.nonce
            )
            return False
        return True

    async def _still_simulate(self, runs: Sequence[_Run]) -> list[bool]:
        """Which of `runs` still simulate, in one batch; those that don't are rejected, with
        their revert data. One with a transaction out only sits out: it may have landed,
        which its receipt tells, and a landed call no longer simulates. So does one whose node
        hadn't the block, or that a block wouldn't take as signed (its nonce, fee or balance):
        nothing is held against it, and the next block tries it again, until its deadline. One
        evicted while they were simulated stays as it was."""
        still: list[bool] = []
        reverts = await simulations(self._chain, [r.simulation for r in runs])
        for t, revert in zip((r.tracked for r in runs), reverts, strict=True):
            if isinstance(revert, BlockUnseenError | ChainError):  # it sits out, unknown yet
                logger.warning("simulating 0x%s: %s", t.submission.id.hex(), revert)
            elif revert is not None and not t.sent and t.status is Status.PENDING:
                t.revert_data = revert[:MAX_REVERT_BYTES]
                self._queue.finish(t, Status.REJECTED, Rejection.SIMULATION_FAILED)
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
        (_planned_or_failed); one that can't be planned or priced sits out, logged as `doing`,
        and the rest go on; one a bug of ours met sits out too, reported."""
        covered: list[tuple[T, Plan]] = []
        for item, planned in zip(items, plans, strict=True):
            one = tracked_of(item)
            if one.status is not Status.PENDING:  # evicted while the plans or a charge were read
                continue
            with self._item(f"{doing} 0x{one.submission.id.hex()}"):
                plan = self._planned_or_failed(one, planned)
                if plan is None:
                    continue
                one.basis = plan.basis
                charge = await self._pricing.charge(measured, latest, one.submission.token, plan)
                if one.paying(latest.next_base_fee) >= charge:
                    covered.append((item, plan))
        return covered

    @property
    def sender(self) -> ChecksumAddress:
        """The float's account 0: what a submission's transaction is planned and simulated as
        sent from."""
        return derive(self._float, 0).address

    def _planned_or_failed(self, tracked: Tracked, planned: Planned) -> Plan | None:
        """`tracked`'s plan; or, its estimate having reverted, None, it rejected with that
        revert data as a simulation that fails is (unless its transaction is out, when it only
        sits out: it may have landed). Why else there's no plan, raised."""
        if isinstance(planned, SimulationFailedError):
            if not tracked.sent:
                tracked.revert_data = planned.revert_data[:MAX_REVERT_BYTES]
                self._queue.finish(tracked, Status.REJECTED, Rejection.SIMULATION_FAILED)
            return None
        return plan_of(planned)


def _signed_hashes(tracked: Tracked) -> tuple[bytes, ...]:
    """The hashes of `tracked`'s transactions another account signed (a sponsorship's own),
    once it's been sent: looked for with the relay's, only to say which landed."""
    taken = tracked.submission
    if not isinstance(taken, Sponsorship) or not tracked.sent:
        return ()
    return tuple(t.signed.hash for t in taken.transactions)


def payee_tokens(measured: Measurements) -> dict[ChecksumAddress, Denomination]:
    """Each measured token but ETH, and its decimals and symbol: what the payee's holdings are
    read of."""
    return {
        t: Denomination(measured.decimals[t], measured.symbols[t])
        for t in measured.tokens
        if t != NATIVE_ASSET
    }
