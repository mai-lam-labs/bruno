"""What the relay needs to know about a payment token, read from the chain."""

import httpx
from eth_typing import ChecksumAddress
from eth_utils.crypto import keccak

from .chain import Chain
from .evm import NATIVE_ASSET
from .forks import GasSchedule
from .net.jsonrpc import call
from .runner import Erc20

_BALANCE_OF = keccak(text="balanceOf(address)")[:4]
_DECIMALS = keccak(text="decimals()")[:4]


async def erc20(
    client: httpx.AsyncClient,
    url: str,
    token: ChecksumAddress,
    holder: ChecksumAddress,
    schedule: GasSchedule,
) -> Erc20:
    """`token`, its balance_gas measured: eth_estimateGas of balanceOf(holder), less the
    call's intrinsic gas. Run as a transaction, its storage is cold, the most it costs on
    that path; the runner calls it warm-addressed, so the estimate covers it."""
    data = _BALANCE_OF + bytes(12) + bytes.fromhex(holder[2:])
    estimate = int(
        await call(client, url, "eth_estimateGas", [{"to": token, "data": "0x" + data.hex()}]), 16
    )
    standard, _ = schedule.intrinsic(data)
    return Erc20(address=token, balance_gas=estimate - standard)


async def decimals(chain: Chain, token: ChecksumAddress) -> int:
    """How many decimals `token` has (its decimals()): ETH's, 18."""
    if token == NATIVE_ASSET:
        return 18
    return int.from_bytes(await chain.call(token, _DECIMALS))
