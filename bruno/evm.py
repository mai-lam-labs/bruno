"""EVM values shared across the relay, and the gas facts every supported fork shares."""

from dataclasses import dataclass

from eth_typing import ChecksumAddress, HexAddress, HexStr

# a warm account's access (EIP-2929), the same in every fork the relay supports; the rest of
# the gas schedule is per fork (bruno.forks)
WARM_ACCOUNT_ACCESS = 100
# secp256k1's group order: a signature's r and s are under it, and s at most half of it (EIP-2)
SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
# ETH where an asset's address is expected (ERC-7528)
NATIVE_ASSET: ChecksumAddress = ChecksumAddress(
    HexAddress(HexStr("0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"))
)


@dataclass(frozen=True, slots=True)
class EncodedCall:
    """A call as a transaction would make it: a target, its calldata, and the wei sent.

    A client's calls are always value 0 (the runner holds no ETH to send); value is for
    calls the service or the tests make."""

    to: ChecksumAddress
    data: HexStr
    value: int = 0


def has_code(code: str) -> bool:
    """Whether `code`, as eth_getCode answers it, is any: nodes answer an account without
    code "0x", some "0x0"."""
    return code not in ("0x", "0x0")


def memory_gas(words: int) -> int:
    """Memory's cost at `words` 32-byte words (Yellow Paper): an expansion costs the
    difference."""
    return 3 * words + words * words // 512
