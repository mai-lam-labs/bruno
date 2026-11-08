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
from .bundle import TopUp, atomic_params, bundles, sign_funding
from .chain import (
    Chain,
    ChainError,
    Latest,
    quantity,
    receipt_of,
)
from .config import ChainConfig
from .evm import NATIVE_ASSET
from .forks import GasSchedule
from .jsonrpc import Answer, Call, JsonRpcError
from .maintenance import Denomination, monitor, survey
from .measurements import (
    Measurements,
    Measuring,
    UnavailableError,
    schedule_of,
)
from .plan import (
    BlockUnseenError,
    Plan,
    Planned,
    Simulation,
    SimulationFailedError,
    make_plans,
    simulations,
)
from .prices import PriceError
from .pricing import Pricing, UnsupportedError, fee_ceiling
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


@dataclass(frozen=True, slots=True)
class _Slot:
    """A batch's submission, on concurrency account `account`."""

    tracked: Tracked
    account: int


@dataclass(frozen=True, slots=True)
class _Run:
    """A tracked submission's transaction, as planned, from `sender`."""

    tracked: Tracked
    plan: Plan
    sender: ChecksumAddress


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
        )
        ranked: list[tuple[Fraction, Tracked, Plan]] = []
        covered = await self._covered(measured, latest, pending, lambda t: t, plans, "ranking")
        for tracked, plan in covered:
            with self._item(f"ranking 0x{tracked.submission.id.hex()}"):
                profit = await self._pricing.profit(tracked, fees, latest.next_base_fee)
                ranked.append((profit, tracked, plan))
        ranked.sort(key=lambda r: r[0], reverse=True)
        # every ranked one simulated together: the best that still simulate are chosen
        simulating = await self._still_simulate([_Run(t, p, sender) for _, t, p in ranked])
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
        self._batch = _Batch(tuple(slots), frozenset(awaiting))
        if top_ups:
            funding = _Funding(tuple(top_ups), frozenset(awaiting), status.float_nonce)
            self._funding = funding
            await self._send_funding(funding, latest, schedule, status.float_balance)

    async def on_block(self, latest: Latest) -> None:
        """`latest` is the latest block: reconcile what was sent for it and the funding in
        flight, then send the funding again if it hasn't landed, and the batch's bundles, for
        the next. Each slot's transaction is signed
        if it covers the block's price, still simulates, and its account covers it; the
        receipts, the slots' reads, their simulations and their accounts' state are each one
        batch, and none is read when there's none to read."""
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
        )
        covered = await self._covered(
            measured, latest, slots, lambda s: s.tracked, plans, "signing"
        )
        simulating = await self._still_simulate(
            [_Run(s.tracked, p, derive(self._float, s.account).address) for s, p in covered]
        )
        ready = [c for c, ok in zip(covered, simulating, strict=True) if ok]
        if not ready:
            return
        states = await monitor(self._chain, self._float, [s.account for s, _ in ready])
        txs: list[SignedTransaction] = []
        for (slot, plan), state in zip(ready, states, strict=True):
            tracked = slot.tracked
            with self._item(f"signing 0x{tracked.submission.id.hex()}"):
                tx = sign(
                    derive(self._float, slot.account),
                    self._chain.chain_id,
                    state.nonce,
                    fees,
                    plan.gas_limit,
                    plan.transaction.to,
                    bytes.fromhex(plan.transaction.data.removeprefix("0x")),
                    authorizations=tracked.submission.authorizations,
                )
                if state.balance < tx.max_cost:
                    continue  # fees rose past its funding: the next batch tops it up
                tracked.sent = (*tracked.sent, tx.hash)[-SENT_KEPT:]
                tracked.attempts += 1
                txs.append(tx)
        if txs:
            await self._send(target, txs)

    async def _send(self, target: int, txs: Sequence[SignedTransaction]) -> None:
        """Every builder its bundles of `txs` for block `target`."""
        await self._offer(
            target,
            [
                (builder, params)
                for builder in self._builders
                for params in bundles(builder.mode, target, None, txs)
            ],
        )

    async def _send_funding(
        self, funding: _Funding, latest: Latest, schedule: GasSchedule, float_balance: int
    ) -> None:
        """`funding` signed at the float's nonce and the fees of the block after `latest`, and
        sent to every builder for that block, a bundle of its own in either mode: only one
        transaction at the nonce can land, so only one of those sent for it. Not sent again for
        a block it's been sent for. One the float, holding `float_balance`, can't cover isn't
        sent and is dropped: the next batch funds what's needed then, the float perhaps topped
        up. Nothing sent for an earlier block can still land."""
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
        # recorded before it's sent: a copy sent is always looked for
        self._funding = replace(
            funding, sent=(*funding.sent, tx.hash)[-SENT_KEPT:], sent_for=target
        )
        await self._offer(
            target, [(builder, atomic_params(target, [tx])) for builder in self._builders]
        )

    async def _offer(self, target: int, sends: Sequence[tuple[Builder, dict[str, object]]]) -> None:
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
        flight, as of `latest`: every receipt looked for in one batch, with the float's nonce
        and balance if there's a funding. Each transaction is looked for again until it's
        SENT_KEPT sends old: a node without the block yet has no receipt. A submission past its
        deadline, unlanded, expires; a funding not landed is sent again for the next block."""
        target = latest.number + 1
        pending = self._queue.pending()
        funding = self._funding
        hashes = [h for t in pending for h in t.sent]
        floats: list[Call] = []
        if funding is not None:
            hashes.extend(funding.sent)
            floats = [
                Call(method, [self._float.address, "latest"])
                for method in ("eth_getTransactionCount", "eth_getBalance")
            ]
        answers = await self._chain.batch(
            [*(Call("eth_getTransactionReceipt", ["0x" + h.hex()]) for h in hashes), *floats]
        )
        receipts = dict(zip(hashes, answers[: len(hashes)], strict=True))
        for tracked in pending:
            with self._item(f"reconciling 0x{tracked.submission.id.hex()}"):
                self._reconcile_one(tracked, target, receipts)
        if funding is not None:
            nonce, balance = (quantity(a.value()) for a in answers[len(hashes) :])
            if self._reconcile_funding(funding, receipts, nonce):
                measured = await self._measuring.measurements()
                if measured.unavailable is None:
                    await self._send_funding(funding, latest, schedule_of(measured), balance)

    def _reconcile_one(
        self, tracked: Tracked, target: int, receipts: Mapping[bytes, Answer]
    ) -> None:
        for sent in tracked.sent:
            receipt = receipt_of(receipts[sent].value())
            if receipt is not None:
                tracked.transaction_hash = sent
                tracked.block_number = receipt.block_number
                if receipt.status == 1:
                    self._queue.finish(tracked, Status.INCLUDED)
                else:
                    self._queue.finish(tracked, Status.REJECTED, Rejection.REVERTED)
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
            receipt = receipt_of(receipts[sent].value())
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
        hadn't the block: nothing is known of it until the next."""
        sims = [Simulation(r.tracked.submission, r.plan, r.sender) for r in runs]
        still: list[bool] = []
        reverts = await simulations(self._chain, sims)
        for t, revert in zip((r.tracked for r in runs), reverts, strict=True):
            if isinstance(revert, BlockUnseenError | ChainError):  # it sits out, unknown yet
                logger.warning("simulating 0x%s: %s", t.submission.id.hex(), revert)
            elif revert is not None and not t.sent:
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
            with self._item(f"{doing} 0x{one.submission.id.hex()}"):
                plan = self._planned_or_failed(one, planned)
                if plan is None:
                    continue
                one.gas_limit, one.size = plan.gas_limit, plan.size
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
        if isinstance(planned, Exception):
            raise planned
        return planned


def payee_tokens(measured: Measurements) -> dict[ChecksumAddress, Denomination]:
    """Each measured token but ETH, and its decimals and symbol: what the payee's holdings are
    read of."""
    return {
        t: Denomination(measured.decimals[t], measured.symbols[t])
        for t in measured.tokens
        if t != NATIVE_ASSET
    }
