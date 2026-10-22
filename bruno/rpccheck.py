"""Every node a configuration lists, each checked on its own (make rpc-check): read-only, for
choosing and spreading nodes. Each is named by its host and its URL's last four characters, so
two keys at one provider tell apart and neither is shown.

For each node: its chain id, how far its head lags the chain's best, one batch of every method
the relay reads by (as the treasury's and a measurement's are, padded to BATCH_CALLS), and that
batch sent BURST times back to back, which is where a free tier's rate limit shows.

    python -m bruno.rpccheck CONFIG
"""

import asyncio
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .chain import Chain, ChainError, empty_call, host
from .config import load
from .evm import NATIVE_ASSET
from .forks import NETWORK_NAMES
from .net.jsonrpc import Call, JsonRpcError

# the batch: every method the relay reads by, padded with balance reads to this many calls,
# about the largest the relay sends (a measurement, a treasury run)
BATCH_CALLS = 20
# the same batch this many times back to back
BURST = 5
TIMEOUT_SECONDS = 15
# an address read for the padding: the zero address, which every node has
_ZERO = "0x0000000000000000000000000000000000000000"


@dataclass(frozen=True, slots=True)
class Checked:
    """A node as checked: its chain id, head, the batch's outcome and time, and how many of the
    burst it answered; or why it couldn't be checked at all."""

    name: str
    chain_id: int | None = None
    head: int | None = None
    batch: str = ""
    batch_ms: float = 0.0
    burst: int = 0
    failed: str | None = None


def name(url: str) -> str:
    """A node's host, and its URL's last four characters where it has a path: enough to tell
    two keys apart, too little to use."""
    tail = url.rstrip("/")[len(f"https://{host(url)}") :]
    return host(url) if len(tail) <= 1 else f"{host(url)} …{tail[-4:]}"


def calls(token: str | None) -> list[Call]:
    """One of each method the relay reads by, padded to BATCH_CALLS with balance reads."""
    batch = [
        Call("eth_chainId", []),
        Call("eth_getBlockByNumber", ["latest", False]),
        Call("eth_feeHistory", [hex(5), "latest", [50]]),
        empty_call(),
        Call("eth_getCode", [_ZERO, "latest"]),
        Call("eth_getTransactionCount", [_ZERO, "latest"]),
        Call("eth_getTransactionReceipt", ["0x" + "00" * 32]),
    ]
    if token is not None:  # balanceOf(the zero address)
        batch.append(Call("eth_call", [{"to": token, "data": "0x70a08231" + "00" * 32}, "latest"]))
    while len(batch) < BATCH_CALLS:
        batch.append(Call("eth_getBalance", [_ZERO, "latest"]))
    return batch


async def check(client: httpx.AsyncClient, chain_id: int, url: str, token: str | None) -> Checked:
    node = Chain(client, chain_id, (url,))
    try:
        reported = int(await node.rpc("eth_chainId", []), 16)
        head = int(await node.rpc("eth_blockNumber", []), 16)
    except (ChainError, JsonRpcError, ValueError, TypeError) as e:
        return Checked(name(url), failed=str(e))
    batch = calls(token)
    start = time.monotonic()
    try:
        answers = await node.batch(batch)
        errors: list[str] = []
        for c, a in zip(batch, answers, strict=True):
            try:
                a.value()
            except JsonRpcError as e:
                errors.append(f"{c.method}: {e}")
        outcome = "ok" if not errors else f"{len(errors)} refused ({errors[0]})"
    except ChainError as e:
        outcome = str(e).split("no node answered: ")[-1].removeprefix(f"{host(url)}: ")
    elapsed = (time.monotonic() - start) * 1000
    answered = 0
    for _ in range(BURST):
        try:
            await node.batch(batch)
            answered += 1
        except ChainError:
            pass
    return Checked(name(url), reported, head, outcome, elapsed, answered)


async def _main(config_path: Path) -> int:
    config = load(config_path)
    unserved = 0
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        for chain_id, chain in config.chains.items():
            token = next((t.address for t in chain.tokens if t.address != NATIVE_ASSET), None)
            checked = await asyncio.gather(*(check(client, chain_id, u, token) for u in chain.rpc))
            best = max((c.head for c in checked if c.head is not None), default=0)
            print(f">> {NETWORK_NAMES.get(chain_id, str(chain_id))}: {len(checked)} nodes")
            usable = 0
            for c in checked:
                if c.failed is not None:
                    print(f"   {c.name:44} FAILED {c.failed}")
                    continue
                wrong = "" if c.chain_id == chain_id else f"  WRONG CHAIN {c.chain_id}"
                lag = best - (c.head or 0)
                ok = c.batch == "ok" and not wrong
                usable += ok
                print(
                    f"   {c.name:44} lag {lag:2}  batch {c.batch} ({c.batch_ms:.0f} ms)"
                    f"  burst {c.burst}/{BURST}{wrong}"
                )
            if usable == 0:
                print("   NO USABLE NODE")
                unserved += 1
    return 1 if unserved else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(Path(sys.argv[1]))))
