"""The payee's and the float's funds as the treasury's tests set and read them: the treasury's
last run, what it measures, and the chain set up for its costs."""

from typing import Any

import httpx

from bruno.chain import Chain
from bruno.forks import GasSchedule
from bruno.relay import fee_ceiling
from bruno.tests.deployments import CLIENT_GAS
from bruno.tests.localchain import SWAP_ROUTER, USDC, USDT, WETH, LocalChain
from bruno.tests.served import Served


async def treasury_entry(served: Served) -> dict[str, Any]:
    """The chain's treasury on the root, as its last run left it."""
    root = (await served.http.get("/")).json()
    entry: dict[str, Any] = root["chains"][0]["treasury"]
    return entry


async def probe(chain: LocalChain, served: Served) -> tuple[int, int]:
    """The float's target and the payee's reserve as the treasury finds them now, its gas
    measured: from a run with the payee holding no ETH, so it can send nothing. The float and
    its accounts aren't changed after, so the next run measures no spend."""
    await chain.set_eth_balance(served.payee, 0)
    await served.treasury()
    entry = await treasury_entry(served)
    assert entry["outcome"].startswith("nothing sent"), entry["outcome"]
    return int(entry["target"], 16), int(entry["reserve"], 16)


async def a_transaction(chain: LocalChain, served: Served, schedule: GasSchedule) -> int:
    """What a transaction of the largest gas costs at the fee ceiling: the least a float that
    isn't running dry holds."""
    async with httpx.AsyncClient() as client:
        live = Chain(client, served.chain_id, (chain.url,))
        return CLIENT_GAS * fee_ceiling(await live.latest(), 1).max_fee_per_gas


async def weth_dust(chain: LocalChain, payee: str, forwarded_to: str | None = None) -> int:
    """WETH for the payee worth ten times its unwrap's gas at the base fee, and the forward's
    to `forwarded_to` if given, the gas measured: more than they cost, but too little to be
    worth it, at under 50 times."""
    await chain.mint(WETH, payee, 1)
    withdraw = chain.calldata("withdraw(uint256)", ["uint256"], [1])
    calls: list[dict[str, str]] = [{"from": payee, "to": WETH.address, "data": withdraw}]
    if forwarded_to is not None:
        calls.append({"from": payee, "to": forwarded_to, "value": "0x1"})
    gas = sum([int(await chain.rpc("eth_estimateGas", [c]), 16) for c in calls])
    dust = 10 * gas * await chain.base_fee()
    await chain.mint(WETH, payee, dust - 1)
    return dust


async def approve_router(chain: LocalChain, payee: str) -> None:
    """The router may spend the payee's USDC and USDT, as the treasury approves it ahead of
    its first swaps: for the tests that aren't about that."""
    for token in (USDC, USDT):
        await chain.send(
            payee,
            token.address,
            "approve(address,uint256)",
            ["address", "uint256"],
            [SWAP_ROUTER, 2**256 - 1],
        )


async def a_gwei_base_fee(chain: LocalChain) -> None:
    """The latest block's base fee 1 gwei: a treasury's costs, not anvil's decayed fee."""
    await chain.set_base_fee(10**9)
    await chain.rpc("evm_mine", [])
