"""What the relay needs to know about a payment token, read from the chain: the calls, which
the relay batches as it opens, and what they answer."""

from eth_typing import ChecksumAddress

from .abi import BALANCE_OF, address_word, selector
from .forks import GasSchedule
from .jsonrpc import Call, eth_call
from .runner import Erc20

_DECIMALS = selector("decimals()")
_SYMBOL = selector("symbol()")
_ALLOWANCE = selector("allowance(address,address)")


def _balance_of(holder: ChecksumAddress) -> bytes:
    return BALANCE_OF + address_word(holder)


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


def balance_of_call(token: ChecksumAddress, holder: ChecksumAddress) -> Call:
    """`token`'s balanceOf(holder)."""
    return eth_call(token, _balance_of(holder))


def allowance_call(
    token: ChecksumAddress, owner: ChecksumAddress, spender: ChecksumAddress
) -> Call:
    """`token`'s allowance(owner, spender)."""
    return eth_call(token, _ALLOWANCE + address_word(owner) + address_word(spender))


def decimals_call(token: ChecksumAddress) -> Call:
    """`token`'s decimals()."""
    return eth_call(token, _DECIMALS)


def symbol_call(token: ChecksumAddress) -> Call:
    """`token`'s symbol()."""
    return eth_call(token, _SYMBOL)


def symbol_of(answer: bytes) -> str:
    """A symbol() answer as text: an ABI string, or, as some older tokens answer (MKR's),
    bytes32 padded with zeros; undecodable bytes replaced."""
    if len(answer) == 32:
        return answer.rstrip(b"\0").decode("utf-8", errors="replace")
    length = int.from_bytes(answer[32:64])
    return answer[64 : 64 + length].decode("utf-8", errors="replace")
