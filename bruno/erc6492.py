"""ERC-6492: a signature from a contract account that may not be deployed yet, carrying its
deployment. A wrapped signature is abi.encode((address, bytes, bytes), (factory,
factoryCalldata, signature)) followed by the 32 magic bytes, 0x6492 repeated: the factory,
called with the calldata, deploys the account (or prepares one that's deployed but can't check
the signature yet), and the inner signature is then the account's ERC-1271 one.

A deployment is the same triple a submission can also name for itself: an account, and the
factory call that deploys it (ERC-4337's `factory` and `factoryData`)."""

from dataclasses import dataclass

from eth_abi.abi import decode
from eth_abi.exceptions import DecodingError
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address

from .common import BrunoError

MAGIC = bytes.fromhex("6492" * 16)
_WRAPPER_TYPES = ["address", "bytes", "bytes"]


class WrapperError(BrunoError):
    """A signature ends in ERC-6492's magic bytes, but what's before them isn't its wrapper."""


@dataclass(frozen=True, slots=True)
class Deployment:
    """`account`, deployed by calling `factory` with `factory_data` when it has no code."""

    account: ChecksumAddress
    factory: ChecksumAddress
    factory_data: bytes

    def encoded(self) -> tuple[ChecksumAddress, ChecksumAddress, bytes]:
        """Its fields in order, as a submission's id encodes them."""
        return (self.account, self.factory, self.factory_data)


# a submission's id encodes a deployment as this tuple
ID_TYPE = "(address,address,bytes)"


def wrapped(signature: bytes) -> bool:
    """Whether `signature` is ERC-6492's wrapper: it ends in the magic bytes."""
    return signature.endswith(MAGIC)


def unwrapped(signature: bytes, account: ChecksumAddress) -> tuple[bytes, Deployment | None]:
    """`signature`'s inner signature, and the deployment of `account` its wrapper carries: as it
    is, and None, if it isn't wrapped."""
    if not wrapped(signature):
        return signature, None
    try:
        factory, data, inner = decode(_WRAPPER_TYPES, signature[: -len(MAGIC)])
    # eth_abi's: what's before the magic bytes doesn't decode; an offset past any index
    except (DecodingError, OverflowError) as e:
        raise WrapperError(f"not ERC-6492's wrapper: {e}") from e
    return bytes(inner), Deployment(account, to_checksum_address(factory), bytes(data))
