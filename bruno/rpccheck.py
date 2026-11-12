"""Every node a configuration lists, each checked on its own for what the relay needs of it
(make rpc-check): read-only. Each is named by its host and its URL's last four characters, so
two keys at one provider tell apart and neither is shown.

For each node: its chain id and how far its head lags the chain's best; every method the relay
reads by, asked on its own, so one it refuses is named (eth_simulateV1 on a keyless tier, most
often), but for those it's configured `without`, which it's never asked; whether it takes
batch requests and how large, the largest of BATCH_SIZES it answers in full, against the
max_batch it's configured with; one batch of every method together (as the treasury's and a
measurement's are, padded to BATCH_CALLS; sent as several where its max_batch is lower); and
that batch BURST times back to back, where a rate limit shows. Each request is made as if the
node were asked for the first time: a node the relay would rest (Chain.batch) is asked all the
same.

    python -m bruno.rpccheck CONFIG
"""

import asyncio
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

from .abi import BALANCE_OF, address_word
from .chain import Chain, ChainError, empty_call, host, quantity, simulating_call
from .config import load
from .evm import NATIVE_ASSET
from .forks import NETWORK_NAMES
from .jsonrpc import MAX_BATCH, Call, JsonRpcError, Node, eth_call

# the batch sizes tried, smallest first: the relay's own default (MAX_BATCH) among them
BATCH_SIZES = (1, 10, 20, 50, 100)
# the batch of every method the relay reads by, padded with balance reads to this many calls,
# about the largest the relay sends (a measurement, a treasury run)
BATCH_CALLS = 20
# the same batch this many times back to back
BURST = 5
TIMEOUT_SECONDS = 15
# an address read for the padding: the zero address, which every node has
_ZERO = "0x0000000000000000000000000000000000000000"


@dataclass(frozen=True, slots=True)
class Checked:
    """A node as checked: its batch cap and the methods it's without, as configured; its chain
    id, head, the methods it refused asked alone (each with
    why), the largest batch it answered (0: none) and why the next size wasn't, the batch of
    every method's outcome and time, and how many of the burst it answered; or why it couldn't
    be checked at all."""

    name: str
    max_batch: int = MAX_BATCH
    without: tuple[str, ...] = ()
    chain_id: int | None = None
    head: int | None = None
    refused: tuple[str, ...] = ()
    largest: int = 0
    over: str = ""
    batch: str = ""
    batch_ms: float = 0.0
    burst: int = 0
    failed: str | None = None


def name(url: str) -> str:
    """A node's host, and its URL's last four characters where it has a path: enough to tell
    two keys apart, too little to use."""
    tail = url.rstrip("/")[len(f"https://{host(url)}") :]
    return host(url) if len(tail) <= 1 else f"{host(url)} …{tail[-4:]}"


def methods(token: str | None) -> list[Call]:
    """One call of each method the relay reads by."""
    each = [
        Call("eth_chainId", []),
        Call("eth_getBlockByNumber", ["latest", False]),
        Call("eth_feeHistory", [hex(5), "latest", [50]]),
        empty_call(),
        simulating_call(),
        Call("eth_getCode", [_ZERO, "latest"]),
        Call("eth_getTransactionCount", [_ZERO, "latest"]),
        Call("eth_getTransactionReceipt", ["0x" + "00" * 32]),
    ]
    if token is not None:  # balanceOf(the zero address)
        each.append(eth_call(token, BALANCE_OF + address_word(_ZERO)))
    return each


def asked(configured: Node, calls: Sequence[Call]) -> list[Call]:
    """Those of `calls` the node is asked: not the methods it's configured without."""
    return [c for c in calls if c.method not in configured.without]


def calls(token: str | None) -> list[Call]:
    """`methods`, padded to BATCH_CALLS with balance reads."""
    batch = methods(token)
    while len(batch) < BATCH_CALLS:
        batch.append(Call("eth_getBalance", [_ZERO, "latest"]))
    return batch


def _why(url: str, error: ChainError | JsonRpcError) -> str:
    """Why a node didn't answer, without what every line already says (its host)."""
    return str(error).split("no node answered: ")[-1].removeprefix(f"{host(url)}: ")


async def refused(
    client: httpx.AsyncClient, chain_id: int, configured: Node, token: str | None
) -> tuple[str, ...]:
    """Each method the node is asked, asked of it on its own: those it refuses, each with why.
    An eth_simulateV1 answered with anything but a simulation's list counts, as a measurement
    counts it."""
    found: list[str] = []
    for call in asked(configured, methods(token)):
        try:
            node = Chain(client, chain_id, (configured,))
            result = await node.rpc(call.method, call.params)
        except (ChainError, JsonRpcError) as e:
            why = _why(configured.url, e).removeprefix(f"{call.method}: ")
            found.append(f"{call.method} ({why})")
            continue
        if call.method == "eth_simulateV1" and not isinstance(result, list):
            found.append(f"{call.method} (no simulation in its answer)")
    return tuple(found)


async def largest_batch(
    client: httpx.AsyncClient, chain_id: int, configured: Node
) -> tuple[int, str]:
    """The largest batch the node answers in full as one request, of BATCH_SIZES and its
    configured max_batch (0: not even a batch of one), and why the next size wasn't."""
    largest = 0
    for size in sorted({*BATCH_SIZES, configured.max_batch}):
        node = Chain(client, chain_id, (Node(configured.url, size),))
        try:
            for answer in await node.batch([Call("eth_getBalance", [_ZERO, "latest"])] * size):
                answer.value()
        except (ChainError, JsonRpcError) as e:
            return largest, f"{size}: {_why(configured.url, e)}"
        largest = size
    return largest, ""


async def check(
    client: httpx.AsyncClient, chain_id: int, configured: Node, token: str | None
) -> Checked:
    """One node checked, its batches capped as configured."""
    url = configured.url
    node = Chain(client, chain_id, (configured,))
    try:
        reported = quantity(await node.rpc("eth_chainId", []))
        head = quantity(await node.rpc("eth_blockNumber", []))
    except (ChainError, JsonRpcError) as e:
        return Checked(name(url), configured.max_batch, failed=str(e))
    alone = await refused(client, chain_id, configured, token)
    largest, over = await largest_batch(client, chain_id, configured)
    batch = asked(configured, calls(token))
    start = time.monotonic()
    try:
        answers = await Chain(client, chain_id, (configured,)).batch(batch)
        errors: list[str] = []
        for c, a in zip(batch, answers, strict=True):
            try:
                a.value()
            except JsonRpcError as e:
                errors.append(f"{c.method}: {e}")
        outcome = "ok" if not errors else f"{len(errors)} refused ({errors[0]})"
    except ChainError as e:
        outcome = _why(url, e)
    elapsed = (time.monotonic() - start) * 1000
    answered = 0
    for _ in range(BURST):
        try:
            for answer in await Chain(client, chain_id, (configured,)).batch(batch):
                answer.value()
            answered += 1
        except ChainError, JsonRpcError:
            pass
    return Checked(
        name(url),
        configured.max_batch,
        tuple(sorted(configured.without)),
        reported,
        head,
        alone,
        largest,
        over,
        outcome,
        elapsed,
        answered,
    )


async def _main(config_path: Path) -> int:
    config = load(config_path)
    unserved = 0
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        for chain_id, chain in config.chains.items():
            token = next((t.address for t in chain.tokens if t.address != NATIVE_ASSET), None)
            nodes = chain.nodes
            checked = await asyncio.gather(*(check(client, chain_id, n, token) for n in nodes))
            best = max((c.head for c in checked if c.head is not None), default=0)
            print(f">> {NETWORK_NAMES.get(chain_id, str(chain_id))}: {len(checked)} nodes")
            usable = 0
            for c in checked:
                if c.failed is not None:
                    print(f"   {c.name:44} FAILED {c.failed}")
                    continue
                wrong = "" if c.chain_id == chain_id else f"  WRONG CHAIN {c.chain_id}"
                lag = best - (c.head or 0)
                sized = c.largest >= c.max_batch
                ok = c.batch == "ok" and sized and not c.refused and not wrong
                usable += ok
                capped = "" if c.max_batch == MAX_BATCH else f", by {c.max_batch}"
                print(
                    f"   {c.name:44} lag {lag:2}  batches to {c.largest:3}"
                    f"  all methods {c.batch} ({c.batch_ms:.0f} ms{capped})"
                    f"  burst {c.burst}/{BURST}{wrong}"
                )
                if c.without:
                    print(f"   {'':44} without {', '.join(c.without)}")
                for method in c.refused:
                    print(f"   {'':44} REFUSES {method}")
                if c.largest == 0:
                    print(f"   {'':44} NO BATCHES ({c.over})")
                elif not sized:
                    print(f"   {'':44} SET max_batch {c.largest} ({c.over})")
            if usable == 0:
                print("   NO USABLE NODE")
                unserved += 1
    return 1 if unserved else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(Path(sys.argv[1]))))
