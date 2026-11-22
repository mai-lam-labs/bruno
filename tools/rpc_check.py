"""Every node a configuration lists, each checked on its own for what the relay needs of it
(make rpc-check): read-only. Each is named by its host and its URL's last four characters, so
two keys at one provider tell apart and neither is shown.

Every node of a chain is first asked its chain id and head at once, so their lags compare.
Then each in turn is sent the largest request the relay sends it: one batch of every method
the relay reads by, but those it's configured `without`, padded to its max_batch. Its answers
name the methods it refuses (eth_simulateV1 on a keyless tier, most often), and its answering
in full shows it takes batches as large as it's configured for. Only if that batch fails
whole is each method asked alone, to name the one it refuses, and the batch halved down to
the largest it answers. A node the relay would rest (Chain.batch) is asked all the same.

    python tools/rpc_check.py CONFIG
"""

import asyncio
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import httpx

from bruno.abi import BALANCE_OF, address_word
from bruno.chain import Chain, ChainError, empty_call, host, quantity, simulating_call
from bruno.config import load
from bruno.evm import NATIVE_ASSET
from bruno.forks import NETWORK_NAMES
from bruno.jsonrpc import MAX_BATCH, Call, JsonRpcError, Node, eth_call, result_of

TIMEOUT_SECONDS = 15
# an address read for the padding: the zero address, which every node has
_ZERO = "0x0000000000000000000000000000000000000000"


@dataclass(frozen=True, slots=True)
class Checked:
    """A node as checked: its batch cap and the methods it's without, as configured; its chain
    id and head; the batch of every method's outcome and time; the methods it refused (each
    with why); and, if the batch failed whole, the largest batch it answered (0: none) and why
    the next size wasn't. Or why it couldn't be checked at all."""

    name: str
    max_batch: int = MAX_BATCH
    without: tuple[str, ...] = ()
    chain_id: int | None = None
    head: int | None = None
    batch: str = ""
    batch_ms: float = 0.0
    refused: tuple[str, ...] = ()
    largest: int | None = None
    over: str = ""
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


def calls(configured: Node, token: str | None) -> list[Call]:
    """The methods the node is asked, padded with balance reads to its max_batch."""
    batch = asked(configured, methods(token))
    while len(batch) < configured.max_batch:
        batch.append(Call("eth_getBalance", [_ZERO, "latest"]))
    return batch


def _why(url: str, error: ChainError | JsonRpcError) -> str:
    """Why a node didn't answer, without what every line already says (its host)."""
    return str(error).split("no node answered: ")[-1].removeprefix(f"{host(url)}: ")


def _refusal(call: Call, result: object) -> str | None:
    """Why an answered call counts as refused: an eth_simulateV1 answered with anything but a
    simulation's list, as a measurement counts it."""
    if call.method == "eth_simulateV1" and not isinstance(result, list):
        return f"{call.method} (no simulation in its answer)"
    return None


async def refused(
    client: httpx.AsyncClient, chain_id: int, configured: Node, token: str | None
) -> tuple[str, ...]:
    """Each method the node is asked, asked of it on its own: those it refuses, each with why."""
    found: list[str] = []
    node = Chain(client, chain_id, (configured,))
    for call in asked(configured, methods(token)):
        try:
            result = await node.rpc(call.method, call.params)
        except (ChainError, JsonRpcError) as e:
            why = _why(configured.url, e).removeprefix(f"{call.method}: ")
            found.append(f"{call.method} ({why})")
            continue
        if (why := _refusal(call, result)) is not None:
            found.append(why)
    return tuple(found)


async def largest_batch(
    client: httpx.AsyncClient, chain_id: int, configured: Node
) -> tuple[int, str]:
    """The largest batch of balance reads the node answers in full as one request, its
    max_batch then halved (0: not even a batch of one), and why the size above it wasn't
    answered. At its max_batch, the batch of every method failed for a method, not its size."""
    size, over = configured.max_batch, ""
    while size > 0:
        node = Chain(client, chain_id, (Node(configured.url, size),))
        try:
            for answer in await node.batch([Call("eth_getBalance", [_ZERO, "latest"])] * size):
                result_of(answer)
        except (ChainError, JsonRpcError) as e:
            over = f"{size}: {_why(configured.url, e)}"
            size //= 2
            continue
        return size, over
    return 0, over


async def head(client: httpx.AsyncClient, chain_id: int, configured: Node) -> Checked:
    """The node's chain id and head, each asked alone."""
    node = Chain(client, chain_id, (configured,))
    without = tuple(sorted(configured.without))
    try:
        reported = quantity(await node.rpc("eth_chainId", []))
        number = quantity(await node.rpc("eth_blockNumber", []))
    except (ChainError, JsonRpcError) as e:
        return Checked(name(configured.url), configured.max_batch, without, failed=str(e))
    return Checked(name(configured.url), configured.max_batch, without, reported, number)


async def check(
    client: httpx.AsyncClient, chain_id: int, configured: Node, token: str | None, found: Checked
) -> Checked:
    """The node's batch of every method, as large as its max_batch: what it refuses; and only
    if it fails whole, each method asked alone and the largest batch it answers."""
    batch = calls(configured, token)
    start = time.monotonic()
    try:
        answers = await Chain(client, chain_id, (configured,)).batch(batch, atomic=True)
    except ChainError as e:
        elapsed = (time.monotonic() - start) * 1000
        alone = await refused(client, chain_id, configured, token)
        largest, over = await largest_batch(client, chain_id, configured)
        outcome = _why(configured.url, e)
        return replace(
            found, batch=outcome, batch_ms=elapsed, refused=alone, largest=largest, over=over
        )
    elapsed = (time.monotonic() - start) * 1000
    refusals: list[str] = []
    for c, a in zip(batch, answers, strict=True):
        try:
            why = _refusal(c, result_of(a))
        except JsonRpcError as e:
            why = f"{c.method} ({_why(configured.url, e).removeprefix(f'{c.method}: ')})"
        if why is not None and why not in refusals:
            refusals.append(why)
    outcome = "ok" if not refusals else f"{len(refusals)} refused"
    return replace(found, batch=outcome, batch_ms=elapsed, refused=tuple(refusals))


async def _main(config_path: Path) -> int:
    config = load(config_path)
    unserved = 0
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
        for chain_id, chain in config.chains.items():
            token = next((t.address for t in chain.tokens if t.address != NATIVE_ASSET), None)
            nodes = chain.nodes
            heads = await asyncio.gather(*(head(client, chain_id, n) for n in nodes))
            checked = [
                c
                if c.failed is not None or c.chain_id != chain_id
                else await check(client, chain_id, n, token, c)
                for n, c in zip(nodes, heads, strict=True)
            ]
            best = max((c.head for c in checked if c.head is not None), default=0)
            print(f">> {NETWORK_NAMES.get(chain_id, str(chain_id))}: {len(checked)} nodes")
            usable = 0
            for c in checked:
                if c.failed is not None:
                    print(f"   {c.name:44} FAILED {c.failed}")
                    continue
                if c.chain_id != chain_id:
                    print(f"   {c.name:44} WRONG CHAIN {c.chain_id}")
                    continue
                lag = best - (c.head or 0)
                ok = c.batch == "ok"
                usable += ok
                capped = "" if c.max_batch == MAX_BATCH else f", by {c.max_batch}"
                print(
                    f"   {c.name:44} lag {lag:2}  all methods {c.batch}"
                    f" ({c.batch_ms:.0f} ms{capped})"
                )
                if c.without:
                    print(f"   {'':44} without {', '.join(c.without)}")
                for method in c.refused:
                    print(f"   {'':44} REFUSES {method}")
                if c.largest == 0:
                    print(f"   {'':44} NO BATCHES ({c.over})")
                elif c.largest is not None and c.largest < c.max_batch:
                    print(f"   {'':44} SET max_batch {c.largest} ({c.over})")
            if usable == 0:
                print("   NO USABLE NODE")
                unserved += 1
    return 1 if unserved else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(Path(sys.argv[1]))))
