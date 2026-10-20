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


@dataclass(slots=True)
class Tracked:
    """A submission taken, and what's become of it. `gas_limit` is its transaction's, as
    last planned; `sent` its latest transactions, each looked for until it's SENT_KEPT sends
    old; `finished_at` when it stopped being pending (time.monotonic())."""

    submission: Submission
    gas_limit: int
    status: Status = Status.PENDING
    sent: tuple[bytes, ...] = ()
    attempts: int = 0
    transaction_hash: bytes | None = None
    block_number: int | None = None
    reason: str | None = None
    revert_data: bytes | None = None
    finished_at: float | None = None

    def finish(self, status: Status, reason: str | None = None) -> None:
        self.status = status
        self.reason = reason
        self.finished_at = time.monotonic()
