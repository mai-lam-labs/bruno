"""EVM values shared across the relay, and the gas facts every supported fork shares."""

from dataclasses import dataclass

from eth_typing import ChecksumAddress, HexStr

# a warm account's access (EIP-2929), the same in every fork the relay supports; the rest of
# the gas schedule is per fork (bruno.forks)
WARM_ACCOUNT_ACCESS = 100


@dataclass(frozen=True, slots=True)
class EncodedCall:
    """A call as a transaction would make it: a target, its calldata, and the wei sent.

    A client's calls are always value 0 (the runner holds no ETH to send); value is for
    calls the service or the tests make."""

    to: ChecksumAddress
    data: HexStr
    value: int = 0


def memory_gas(words: int) -> int:
    """Memory's cost at `words` 32-byte words (Yellow Paper): an expansion costs the
    difference."""
    return 3 * words + words * words // 512
