"""Before deploying (make preflight; worker-deploy runs it first): every chain a configuration
serves, checked against its live nodes, read-only. What the relay will measure (its nodes'
chain id, the fork, the deployer, every token's gas, decimals and symbol, every x402 token's
transfer), every token priced along its pools, the treasury's router and WETH there, and every
builder answering an empty bundle, which carries nothing to land. Anything wrong is printed,
and the exit status is 1.

    python -m bruno.preflight CONFIG PAYEE
"""

import asyncio
import sys
from pathlib import Path

import httpx
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address

from .chain import Chain, ChainError
from .config import ChainConfig, load
from .evm import NATIVE_ASSET
from .forks import NETWORK_NAMES
from .measurements import measure
from .net.jsonrpc import JsonRpcError
from .prices import PriceError, Prices

BUILDER_TIMEOUT_SECONDS = 15


async def check(
    client: httpx.AsyncClient, chain_id: int, config: ChainConfig, payee: ChecksumAddress
) -> list[str]:
    """What's wrong with chain `chain_id` as configured: nothing if it's ready."""
    chain = Chain(client, chain_id, config.rpc, config.slot_seconds)
    try:
        measured = await measure(chain, config, payee)
    except (ChainError, JsonRpcError, httpx.HTTPError) as e:
        return [f"not measured: {e}"]
    problems = list(measured.problems())
    if measured.unavailable is not None:
        return problems
    weth = to_checksum_address(config.weth)
    prices = Prices(
        chain, weth, {to_checksum_address(t.address): t.price for t in config.tokens}, 0
    )
    for token in measured.tokens:
        if token in (NATIVE_ASSET, weth):
            continue
        try:
            await prices.wei(token)
        except (PriceError, ChainError, JsonRpcError) as e:
            problems.append(f"{token} not priced: {e}")
    if config.treasury is not None:
        for name, address in (("the router", config.treasury.router), ("WETH", config.weth)):
            if await chain.code(address) in ("0x", "0x0"):
                problems.append(f"{name} isn't at {address}")
    head = await chain.head()
    nothing: list[str] = []
    for bundler in config.bundlers:
        probe = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_sendBundle",
            "params": [{"txs": nothing, "blockNumber": hex(head + 1)}],
        }
        try:
            answer = await client.post(bundler.url, json=probe, timeout=BUILDER_TIMEOUT_SECONDS)
            if "jsonrpc" not in answer.json():
                problems.append(f"{bundler.url} doesn't answer JSON-RPC ({answer.status_code})")
        except (httpx.HTTPError, ValueError) as e:
            problems.append(f"{bundler.url} doesn't answer: {e}")
    return problems


async def _main(config_path: Path, payee: str) -> int:
    config = load(config_path)
    wrong = 0
    async with httpx.AsyncClient(timeout=30) as client:
        for chain_id, chain_config in config.chains.items():
            own = chain_config.payee if chain_config.payee is not None else payee
            problems = await check(client, chain_id, chain_config, to_checksum_address(own))
            name = NETWORK_NAMES.get(chain_id, str(chain_id))
            print(f">> {name}: {'ready' if not problems else 'NOT READY'}")
            for problem in problems:
                print(f"   {problem}")
            wrong += len(problems)
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(Path(sys.argv[1]), sys.argv[2])))
