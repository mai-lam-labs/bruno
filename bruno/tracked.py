"""A submission taken, and what's become of it."""

import time
from dataclasses import dataclass
from enum import StrEnum

from .submission import Submission

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
    hash of the UserOp it sends, if it's one. Once it finishes, only a Finished record of it is
    kept: its `status` then says so, to anything still holding it."""

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

    def finish(self, status: Status, reason: str | None = None) -> Finished:
        """It finished, as `status`: what's kept of it."""
        self.status = status
        return Finished(
            status=status,
            attempts=self.attempts,
            transaction_hash=self.transaction_hash,
            block_number=self.block_number,
            reason=reason,
            revert_data=self.revert_data,
            finished_at=time.monotonic(),
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
    however many there are. `finished_at` is when it finished (time.monotonic())."""

    status: Status
    attempts: int
    transaction_hash: bytes | None
    block_number: int | None
    reason: str | None
    revert_data: bytes | None
    finished_at: float
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
