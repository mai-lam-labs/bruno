"""What the relay needs to know about a payment token, read from the chain: the calls, which
the relay batches as it opens, and what they answer."""

from eth_typing import ChecksumAddress
from eth_utils.crypto import keccak

from .forks import GasSchedule
from .net.jsonrpc import Call
from .runner import Erc20

_BALANCE_OF = keccak(text="balanceOf(address)")[:4]
_DECIMALS = keccak(text="decimals()")[:4]


def _balance_of(holder: ChecksumAddress) -> bytes:
    return _BALANCE_OF + bytes(12) + bytes.fromhex(holder[2:])


def measuring(token: ChecksumAddress, holder: ChecksumAddress) -> Call:
    """eth_estimateGas of `token`'s balanceOf(holder): what erc20 measures by."""
    data = _balance_of(holder)
    return Call("eth_estimateGas", [{"to": token, "data": "0x" + data.hex()}])


def erc20(
    token: ChecksumAddress, holder: ChecksumAddress, estimate: int, schedule: GasSchedule
) -> Erc20:
    """`token`, its balance_gas measured: `estimate` (measuring's answer) less the call's
    intrinsic gas. Run as a transaction, its storage is cold, the most it costs on that path;
    the runner calls it warm-addressed, so the estimate covers it."""
    standard, _ = schedule.intrinsic(_balance_of(holder))
    return Erc20(address=token, balance_gas=estimate - standard)


def decimals_call(token: ChecksumAddress) -> Call:
    """`token`'s decimals()."""
    return Call("eth_call", [{"to": token, "data": "0x" + _DECIMALS.hex()}, "latest"])
