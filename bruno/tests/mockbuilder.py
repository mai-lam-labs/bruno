"""A block builder on anvil that takes eth_sendBundle's parameters, exactly as they'd be sent
to a real builder, and commits each bundle as rbuilder does (source/rbuilder at 6037fa7,
crates/rbuilder/src/building/order_commit.rs):

- commit_bundle (698-732): a bundle whose blockNumber isn't the block being built, or whose
  timestamps exclude it, isn't committed to it; one that fails is rolled back whole;
- commit_bundle_no_rollback (824-878), each transaction in order:
  - executed and reverted: in droppingTxHashes, rolled back and skipped; in
    revertingTxHashes, kept; in neither, the bundle fails;
  - invalid (commit_tx's Err: nonce, balance, fee under the base fee, gas left): in
    droppingTxHashes, or in revertingTxHashes when the builder allows skipping, skipped;
    otherwise the bundle fails;
  - no gas used at the end: the bundle fails (EmptyBundle);
- rbuilder-primitives/src/serialize.rs: droppingTxHashes in a bundle of version v1 is
  rejected (FieldNotSupportedByVersion).

anvil executes, so a block's outcome is anvil's EVM's. A bundle is committed by mining its
transactions in one block at the target's number and timestamp, then going back: the first
transaction that fails the rule is dropped (or fails the bundle) and the rest mined again,
until none does. Execution is sequential, so that's rbuilder's roll back and continue. The
block is then mined for real with every committed bundle's transactions.

anvil must run with --order fifo (a block keeps the pool's order) and
--disable-pool-balance-checks (a sender the bundle funds is admitted): an invalid
transaction is then one anvil won't take or won't mine."""

import asyncio
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from eth_utils.crypto import keccak

from bruno.common import BrunoError
from bruno.tests.localchain import LocalChain, LocalChainError

# the slot time: each built block's timestamp is its parent's and this
SLOT_SECONDS = 12


class MockBuilderError(BrunoError):
    pass


@dataclass(frozen=True, slots=True)
class RawTx:
    raw: bytes

    @property
    def hash(self) -> bytes:
        return keccak(self.raw)


@dataclass(frozen=True, slots=True)
class Bundle:
    txs: tuple[RawTx, ...]
    block_number: int | None
    dropping: frozenset[bytes]
    reverting: frozenset[bytes]
    min_timestamp: int | None
    max_timestamp: int | None


class Execution(StrEnum):
    """A transaction's outcome in a trial block."""

    SUCCEEDED = "succeeded"
    REVERTED = "reverted"
    INVALID = "invalid"


@dataclass(slots=True)
class BundleResult:
    """What became of a bundle: committed with `included` (its transactions in the block),
    `dropped` left out; or not, and why."""

    bundle: Bundle
    committed: bool = False
    included: list[bytes] = field(default_factory=list)
    dropped: list[bytes] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True, slots=True)
class BuiltBlock:
    number: int
    transactions: tuple[bytes, ...]
    bundles: tuple[BundleResult, ...]


def _hashes(params: dict[str, Any], key: str) -> frozenset[bytes]:
    return frozenset(bytes.fromhex(h[2:]) for h in params.get(key) or [])


def parse_bundle(params: dict[str, Any]) -> Bundle:
    """eth_sendBundle's parameter object, as rbuilder's RawBundle reads it."""
    dropping = _hashes(params, "droppingTxHashes")
    if params.get("version") == "v1" and dropping:
        raise MockBuilderError("droppingTxHashes isn't supported in a v1 bundle")
    block = params.get("blockNumber")
    return Bundle(
        txs=tuple(RawTx(bytes.fromhex(t[2:])) for t in params["txs"]),
        block_number=None if block is None else int(block, 16),
        dropping=dropping,
        reverting=_hashes(params, "revertingTxHashes"),
        min_timestamp=params.get("minTimestamp"),
        max_timestamp=params.get("maxTimestamp"),
    )


class MockBuilder:
    def __init__(
        self, chain: LocalChain, *, allow_tx_skip: bool = True, slot_seconds: int = SLOT_SECONDS
    ) -> None:
        """`allow_tx_skip`: rbuilder's discard_txs (builder-playground sets it);
        `slot_seconds`: how far apart its blocks' timestamps are."""
        self._chain = chain
        self._allow_tx_skip = allow_tx_skip
        self._slot_seconds = slot_seconds
        self._bundles: list[Bundle] = []

    async def send_bundle(self, params: dict[str, Any]) -> str:
        """eth_sendBundle: hold the bundle for its block; its bundle hash."""
        bundle = parse_bundle(params)
        self._bundles.append(bundle)
        return "0x" + keccak(b"".join(t.hash for t in bundle.txs)).hex()

    async def build_block(self) -> BuiltBlock:
        """Build and mine the next block from the bundles held for it, in arrival order.
        Bundles for later blocks are kept; for earlier ones, discarded."""
        head = await self._chain.rpc("eth_getBlockByNumber", ["latest", False])
        number = int(head["number"], 16) + 1
        timestamp = int(head["timestamp"], 16) + self._slot_seconds
        current = [b for b in self._bundles if b.block_number in (None, number)]
        self._bundles = [b for b in self._bundles if (b.block_number or 0) > number]
        committed: list[RawTx] = []
        results: list[BundleResult] = []
        await self._chain.rpc("evm_setAutomine", [False])
        try:
            for bundle in current:
                result = await self._commit(bundle, committed, timestamp)
                if result.committed:
                    committed += [t for t in bundle.txs if t.hash in result.included]
                results.append(result)
            outcomes = await self._mine(committed, timestamp)
        finally:
            await self._chain.rpc("anvil_dropAllTransactions", [])
            await self._chain.rpc("evm_setAutomine", [True])
        if any(o is not Execution.SUCCEEDED and o is not Execution.REVERTED for o in outcomes):
            raise MockBuilderError("the block didn't mine as its bundles were committed")
        return BuiltBlock(number, tuple(t.hash for t in committed), tuple(results))

    async def _commit(self, bundle: Bundle, before: list[RawTx], timestamp: int) -> BundleResult:
        result = BundleResult(bundle)
        if not (bundle.min_timestamp or 0) <= timestamp <= (bundle.max_timestamp or 2**64):
            result.error = "IncorrectTimestamp"
            return result
        candidate = list(bundle.txs)
        while True:
            snapshot = await self._chain.snapshot()
            try:
                outcomes = (await self._mine(before + candidate, timestamp))[len(before) :]
            finally:
                await self._chain.rpc("anvil_dropAllTransactions", [])
                await self._chain.revert(snapshot)
            failing = next(
                (
                    i
                    for i, (tx, outcome) in enumerate(zip(candidate, outcomes, strict=True))
                    if outcome is Execution.INVALID
                    or (outcome is Execution.REVERTED and tx.hash not in bundle.reverting)
                ),
                None,
            )
            if failing is None:
                if not candidate:
                    result.error = "EmptyBundle"
                    return result
                result.committed = True
                result.included = [t.hash for t in candidate]
                return result
            tx = candidate[failing]
            skippable = tx.hash in bundle.dropping or (
                outcomes[failing] is Execution.INVALID
                and self._allow_tx_skip
                and tx.hash in bundle.reverting
            )
            if not skippable:
                result.error = (
                    "TransactionReverted"
                    if outcomes[failing] is Execution.REVERTED
                    else "InvalidTransaction"
                )
                return result
            result.dropped.append(tx.hash)
            del candidate[failing]

    async def _mine(self, txs: list[RawTx], timestamp: int) -> list[Execution]:
        """Mine `txs` in one block at `timestamp`: each one's outcome."""
        admitted: list[bool] = []
        for tx in txs:
            try:
                await self._chain.rpc("eth_sendRawTransaction", ["0x" + tx.raw.hex()])
                admitted.append(True)
            except LocalChainError:
                admitted.append(False)  # e.g. a nonce already used
        await self._chain.rpc("evm_setNextBlockTimestamp", [timestamp])
        await self._chain.rpc("evm_mine", [])
        block = await self._chain.rpc("eth_getBlockByNumber", ["latest", False])
        mined = {bytes.fromhex(h[2:]) for h in block["transactions"]}
        outcomes: list[Execution] = []
        for tx, was_admitted in zip(txs, admitted, strict=True):
            if not was_admitted or tx.hash not in mined:
                outcomes.append(Execution.INVALID)
                continue
            receipt = await self._chain.rpc("eth_getTransactionReceipt", ["0x" + tx.hash.hex()])
            outcomes.append(
                Execution.SUCCEEDED if receipt["status"] == "0x1" else Execution.REVERTED
            )
        return outcomes


async def serve(builder: MockBuilder, host: str, port: int) -> asyncio.Server:
    """`builder` over HTTP, for a relay in another process (the Worker, under workerd):
    eth_sendBundle as a JSON-RPC POST, answered with its bundle hash. HTTP/1.1, one request
    a connection, in the caller's event loop."""

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            length = 0
            for line in head.decode("latin-1").split("\r\n")[1:]:
                name, _, value = line.partition(":")
                if name.strip().lower() == "content-length":
                    length = int(value)
            request = json.loads(await reader.readexactly(length))
            if request.get("method") == "eth_sendBundle":
                result = await builder.send_bundle(request["params"][0])
                reply = {"jsonrpc": "2.0", "id": request.get("id"), "result": result}
            else:
                error = {"code": -32601, "message": f"method not found: {request.get('method')}"}
                reply = {"jsonrpc": "2.0", "id": request.get("id"), "error": error}
            body = json.dumps(reply).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        finally:
            writer.close()

    return await asyncio.start_server(answer, host, port)
