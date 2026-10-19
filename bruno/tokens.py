"""What the relay needs to know about a payment token, read from the chain."""

import httpx
from eth_typing import ChecksumAddress
from eth_utils.crypto import keccak

from .forks import GasSchedule
from .net.jsonrpc import call
from .runner import Erc20

_BALANCE_OF = keccak(text="balanceOf(address)")[:4]


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
