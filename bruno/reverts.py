"""A transaction that reverts when it's simulated or estimated (SimulationFailedError), and
what its revert data says, read as a refusal (revert_reason): Solidity's reason or panic, the
runner's own errors, and the EntryPoint's FailedOp."""

from .abi import selector
from .common import BrunoError
from .refusals import Refusal, Refused, Wording, message, refused
from .runner import BALANCE_UNREADABLE, GAS_EXHAUSTED, UNDERPAID
from .userops import FAILED_OP, FAILED_OP_WITH_REVERT


class SimulationFailedError(BrunoError):
    """The submission's transaction reverts: with `revert_data`, what it says read as its
    refusal (revert_reason, `refused`)."""

    def __init__(self, revert_data: bytes) -> None:
        self.refused = revert_reason(revert_data)
        super().__init__(message(Wording.SIMULATION_FAILED, reason=str(self.refused)))
        self.revert_data = revert_data


# Solidity's revert reason and panic
_ERROR = selector("Error(string)")
_PANIC = selector("Panic(uint256)")


def revert_reason(data: bytes) -> Refused:
    """What a simulation's revert data says, as its refusal: Solidity's revert reason or panic,
    the runner's own errors, the EntryPoint's FailedOp and FailedOpWithRevert (the revert
    inside it read as any is); anything else by its selector."""
    head = data[:4]
    if head == UNDERPAID:
        return refused(Refusal.REVERT_UNDERPAID)
    if head == GAS_EXHAUSTED:
        return refused(Refusal.REVERT_GAS_EXHAUSTED)
    if head == BALANCE_UNREADABLE:
        return refused(Refusal.REVERT_BALANCE_UNREADABLE)
    if head == FAILED_OP and (text := _string_at(data, 1)) is not None:
        return refused(Refusal.REVERT_FAILED_OP, text=text)
    if (
        head == FAILED_OP_WITH_REVERT
        and (text := _string_at(data, 1)) is not None
        and (inner := _bytes_at(data, 2)) is not None
    ):
        return refused(
            Refusal.REVERT_FAILED_OP_WITH_REVERT, text=text, inner=str(revert_reason(inner))
        )
    if head == _ERROR and (text := _string_at(data, 0)) is not None:
        return refused(Refusal.REVERT_ERROR, text=text)
    if head == _PANIC and len(data) == 36:
        return refused(Refusal.REVERT_PANIC, code=f"{int.from_bytes(data[4:]):x}")
    if not data:
        return refused(Refusal.REVERT_EMPTY)
    return refused(Refusal.REVERT_OTHER, selector="0x" + head.hex())


def _string_at(data: bytes, index: int) -> str | None:
    """The ABI-encoded string whose offset is argument `index` of `data`'s arguments, as UTF-8;
    None if it doesn't decode."""
    raw = _bytes_at(data, index)
    if raw is None:
        return None
    try:
        return raw.decode()
    except UnicodeDecodeError:
        return None


def _bytes_at(data: bytes, index: int) -> bytes | None:
    """The ABI-encoded bytes (or string) whose offset is argument `index` of `data`'s
    arguments; None if they don't decode."""
    args = data[4:]
    if len(args) < 32 * (index + 1):
        return None
    offset = int.from_bytes(args[32 * index : 32 * index + 32])
    if offset + 32 > len(args):
        return None
    length = int.from_bytes(args[offset : offset + 32])
    if offset + 32 + length > len(args):
        return None
    return args[offset + 32 : offset + 32 + length]
