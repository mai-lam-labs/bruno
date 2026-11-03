"""A submission taken, what's become of it, and the queue of them."""

import asyncio
from dataclasses import dataclass
from enum import StrEnum

from .common import BrunoError
from .submission import Submission
from .units import Millis
from .userops import FeePayment

# how many of a submission's latest transactions are looked for, a node lagging a block or two
# behind another notwithstanding
SENT_KEPT = 4
# the most of a failed simulation's revert data kept with a rejected submission
MAX_REVERT_BYTES = 1_024


class Status(StrEnum):
    PENDING = "pending"
    INCLUDED = "included"
    EXPIRED = "expired"
    REJECTED = "rejected"


class Rejection(StrEnum):
    """Why a submission was rejected, its status' reason: outbid from a full queue, no longer
    simulating, or its transaction reverted on-chain."""

    OUTBID = "outbid"
    SIMULATION_FAILED = "simulation failed"
    REVERTED = "reverted on-chain"


class Stage(StrEnum):
    """Where a submission is, finer than its status: taken but not yet sent; offered to the
    builders; included, short of finality; its block finalized; rejected or expired."""

    WAITING = "waiting"
    SENDING = "sending"
    CONFIRMING = "confirming"
    DONE = "done"
    FAILED = "failed"


@dataclass(slots=True)
class Tracked:
    """A submission taken and pending, and what's become of it so far. `gas_limit` and `size`
    are its transaction's (its gas limit, its most bytes unsigned), as last planned; `sent` its
    latest transactions, each looked for until it's SENT_KEPT sends old; `user_operation` the
    hash of the UserOp it sends, if it's one, and `fee_payment` what it pays if it pays by its
    fees. Once it finishes, only a Finished record of it is kept: its `status` then says so, to
    anything still holding it."""

    submission: Submission
    gas_limit: int
    size: int
    status: Status = Status.PENDING
    sent: tuple[bytes, ...] = ()
    attempts: int = 0
    transaction_hash: bytes | None = None
    block_number: int | None = None
    revert_data: bytes | None = None
    user_operation: bytes | None = None
    fee_payment: FeePayment | None = None

    def paying(self, base_fee: int) -> int:
        """What it pays in a block of `base_fee`: its amount; or a UserOp paying by its fees,
        what they pay then (its amount, what the runner checks, what they pay in any block)."""
        if self.fee_payment is None:
            return self.submission.amount
        return self.fee_payment.at(base_fee)

    def finish(self, status: Status, reason: Rejection | None = None) -> Finished:
        """It finished, as `status`: what's kept of it."""
        self.status = status
        return Finished(
            status=status,
            attempts=self.attempts,
            transaction_hash=self.transaction_hash,
            block_number=self.block_number,
            reason=reason,
            revert_data=self.revert_data,
            finished_at=Millis.monotonic(),
            user_operation=self.user_operation,
        )

    def stage(self, finalized: int | None) -> Stage:
        """Its stage: offered to the builders once it's been sent, else waiting."""
        del finalized  # a pending submission's isn't read
        return Stage.SENDING if self.sent else Stage.WAITING


@dataclass(frozen=True, slots=True)
class Finished:
    """A submission no longer pending, as much of it as its status needs: what became of it,
    not what it was. Its call and payment are dropped as it finishes: nothing reads them after,
    and a relay keeps finished submissions for `retention_seconds`, so what's kept stays small
    however many there are. `finished_at` is when it finished, by the relay's clock."""

    status: Status
    attempts: int
    transaction_hash: bytes | None
    block_number: int | None
    reason: Rejection | None
    revert_data: bytes | None
    finished_at: Millis
    user_operation: bytes | None = None

    def stage(self, finalized: int | None) -> Stage:
        """Its stage, the finalized block being `finalized` (None: not read, as for one not
        included)."""
        if self.status is not Status.INCLUDED:
            return Stage.FAILED
        block = self.block_number
        if block is not None and finalized is not None and block <= finalized:
            return Stage.DONE
        return Stage.CONFIRMING


class UnknownSubmissionError(BrunoError):
    pass


class Queue:
    """The submissions taken: pending, and finished, each kept `retention` once it is
    (Finished); the UserOps among them, by hash; and the requests held for one, answered as it
    finishes. Submissions are taken one at a time (`lock`): a duplicate or a full queue is
    decided once."""

    def __init__(self, retention: Millis) -> None:
        self._retention = retention
        self._pending: dict[bytes, Tracked] = {}
        self._finished: dict[bytes, Finished] = {}
        # the UserOps sent as submissions, pending or kept finished: each one's hash to its id
        self._operations: dict[bytes, bytes] = {}
        # each held request's submission, set once it's no longer pending
        self._outcomes: dict[bytes, asyncio.Event] = {}
        self.lock = asyncio.Lock()

    def pending(self) -> list[Tracked]:
        return list(self._pending.values())

    def add(self, tracked: Tracked) -> None:
        """`tracked` taken: pending, and found by its UserOp's hash if it sends one."""
        id_ = tracked.submission.id
        self._pending[id_] = tracked
        if tracked.user_operation is not None:
            self._operations[tracked.user_operation] = id_

    def known(self, id_: bytes) -> bool:
        """Whether the submission `id_` was taken: pending, or finished and still kept."""
        return id_ in self._pending or id_ in self._finished

    def operation(self, op_hash: bytes) -> bytes | None:
        """The id of the submission carrying the UserOp `op_hash`, if the relay took it and
        hasn't forgotten it."""
        return self._operations.get(op_hash)

    def status(self, id_: bytes) -> Tracked | Finished:
        """The submission `id_`, pending or finished: UnknownSubmissionError if it's neither."""
        tracked = self._pending.get(id_) or self._finished.get(id_)
        if tracked is None:
            raise UnknownSubmissionError(f"no submission 0x{id_.hex()}")
        return tracked

    async def outcome(self, id_: bytes) -> Finished:
        """The submission once it's no longer pending: included, rejected or expired. Waiting
        reads nothing: it's the reconciling the relay does each block that ends it."""
        if isinstance(self.status(id_), Tracked):
            await self._outcomes.setdefault(id_, asyncio.Event()).wait()
        return self._finished[id_]

    def pending_like(self, submission: Submission) -> Tracked | None:
        """A pending x402 submission of the same call by the same payer (its token, call, gas
        and authorizations), paid by another authorization: a client that timed out and signed
        again. None if there's none."""
        transfer = submission.transfer
        if transfer is None:
            return None
        for t in self._pending.values():
            other = t.submission
            if (
                other.transfer is not None
                and other.transfer.payer == transfer.payer
                and (other.token, other.call, other.gas, other.authorizations)
                == (submission.token, submission.call, submission.gas, submission.authorizations)
            ):
                return t
        return None

    def finish(self, tracked: Tracked, status: Status, reason: Rejection | None = None) -> None:
        """`tracked` finished, as `status`: only what its status needs is kept, and a request
        held for it is answered."""
        id_ = tracked.submission.id
        self._finished[id_] = tracked.finish(status, reason)
        del self._pending[id_]
        outcome = self._outcomes.pop(id_, None)
        if outcome is not None:
            outcome.set()

    def prune(self) -> None:
        """Forget the submissions finished more than `retention` ago."""
        cutoff = Millis.monotonic() - self._retention
        for id_, finished in list(self._finished.items()):
            if finished.finished_at <= cutoff:
                del self._finished[id_]
                if finished.user_operation is not None:
                    del self._operations[finished.user_operation]

    def forget_at(self) -> Millis | None:
        """When the next finished submission is due to be forgotten: None if none is."""
        finished = [f.finished_at for f in self._finished.values()]
        return Millis(min(finished) + self._retention) if finished else None
