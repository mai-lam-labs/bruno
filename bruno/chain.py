"""A chain through its nodes: reads, fees, and sending a transaction and waiting for it.
Each call goes to the first node that answers."""

import asyncio
import os
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from math import ceil
from typing import Any

import httpx

from .forks import GasSchedule, schedule_of
from .net.jsonrpc import Answer, BatchError, Call, batch, call
from .transactions import Fees, SignedTransaction

# how often a sent transaction's receipt is looked for: an engineering bound, under a slot
RECEIPT_POLL_SECONDS = 2
# the slot time on Ethereum and its testnets: the next block is a slot after the latest
SLOT_SECONDS = 12
# the most calls in one batch request: nodes cap a batch (geth at 1,000), and a larger batch
# is sent as several
MAX_BATCH = 100
# the tip is read from the last TIP_BLOCKS blocks' median tips, and the last TIP_RECENT of
# them: enough history to be steady, and recent enough to rise within a few blocks
TIP_BLOCKS = 20
TIP_RECENT = 5
# EIP-1559: the base fee rises by at most an eighth a block
MAX_BASE_FEE_RISE = Fraction(9, 8)


class ChainError(Exception):
    """The nodes failed, or a transaction did: unexpected, not a client's doing, so not a
    BrunoError."""


@dataclass(frozen=True, slots=True)
class Latest:
    """The latest block, the next one's base fee, and the median tip of each of the last
    TIP_BLOCKS blocks, oldest first: what a slot's work is priced and scheduled by, read
    once."""

    number: int
    timestamp: int
    base_fee_per_gas: int
    next_base_fee: int
    tips: tuple[int, ...]


def market_tip(tips: Sequence[int]) -> int:
    """The tip the market pays: the larger of the median of `tips` (each a block's median
    tip, oldest first) and the median of the last TIP_RECENT. The upper median of an even
    count; 0 for none."""
    return max(_median(tips), _median(tips[-TIP_RECENT:]))


def _median(values: Sequence[int]) -> int:
    return sorted(values)[len(values) // 2] if values else 0


def risen(base_fee: int, blocks: int) -> int:
    """The most `base_fee` can be `blocks` blocks later: risen by an eighth each block,
    rounded up."""
    return ceil(base_fee * MAX_BASE_FEE_RISE**blocks)


def lasting_fees(latest: Latest, priority_fee: int, blocks: int) -> Fees:
    """Fees valid for `blocks` blocks however the base fee rises: the next base fee risen for
    them, and `priority_fee`. A transaction pays only the base fee of its block, and the tip."""
    return Fees(risen(latest.next_base_fee, blocks) + priority_fee, priority_fee)


def empty_call() -> Call:
    """eth_estimateGas of a plain call to an account made for it: no code, no value, no data.
    Its gas is the fork's base transaction cost, which tells the forks apart (schedule_of)."""
    return Call("eth_estimateGas", [{"to": "0x" + os.urandom(20).hex()}])


class Chain:
    def __init__(
        self,
        client: httpx.AsyncClient,
        chain_id: int,
        rpc_urls: tuple[str, ...],
        slot_seconds: int = SLOT_SECONDS,
    ) -> None:
        """`slot_seconds`: its block time."""
        self._client = client
        self.chain_id = chain_id
        self.rpc_urls = rpc_urls
        self.slot_seconds = slot_seconds

    async def rpc(self, method: str, params: list[Any]) -> Any:
        """`method` at the first node that answers. A node's error answer (JsonRpcError, a
        revert included) is raised as it is: the next node would answer the same."""
        failures: list[str] = []
        for url in self.rpc_urls:
            try:
                return await call(self._client, url, method, params)
            except httpx.HTTPError as e:
                failures.append(f"{url}: {e}")
        raise ChainError(f"{method}: no node answered: {'; '.join(failures)}")

    async def batch(self, calls: Sequence[Call]) -> list[Answer]:
        """Each of `calls`, as batches of at most MAX_BATCH, each at the first node that answers
        all of it: the answers in the calls' order, a node's error answer to one call its own,
        not the batch's."""
        answers: list[Answer] = []
        for start in range(0, len(calls), MAX_BATCH):
            chunk = calls[start : start + MAX_BATCH]
            failures: list[str] = []
            for url in self.rpc_urls:
                try:
                    answers += await batch(self._client, url, chunk)
                    break
                except (httpx.HTTPError, BatchError) as e:
                    failures.append(f"{url}: {e}")
            else:
                methods = ", ".join(sorted({c.method for c in chunk}))
                raise ChainError(f"{methods}: no node answered: {'; '.join(failures)}")
        return answers

    async def latest(self) -> Latest:
        """The latest block, the next one's base fee (eth_feeHistory's last) and the last
        TIP_BLOCKS blocks' median tips (its 50th percentile), in one request. A block without
        a reward (none of its transactions tipped) tips 0."""
        block, history = (
            a.value()
            for a in await self.batch(
                [
                    Call("eth_getBlockByNumber", ["latest", False]),
                    Call("eth_feeHistory", [hex(TIP_BLOCKS), "latest", [50]]),
                ]
            )
        )
        return Latest(
            int(block["number"], 16),
            int(block["timestamp"], 16),
            int(block["baseFeePerGas"], 16),
            int(history["baseFeePerGas"][-1], 16),
            tuple(int(r[0], 16) if r else 0 for r in history.get("reward") or []),
        )

    async def check_chain(self) -> None:
        """Raise unless the nodes are the chain this was made for."""
        chain_id = int(await self.rpc("eth_chainId", []), 16)
        if chain_id != self.chain_id:
            raise ChainError(f"the nodes are chain {chain_id}, not {self.chain_id}")

    async def balance(self, account: str) -> int:
        return int(await self.rpc("eth_getBalance", [account, "latest"]), 16)

    async def nonce(self, account: str) -> int:
        return int(await self.rpc("eth_getTransactionCount", [account, "latest"]), 16)

    async def code(self, account: str) -> str:
        return await self.rpc("eth_getCode", [account, "latest"])

    async def head(self) -> int:
        return int(await self.rpc("eth_blockNumber", []), 16)

    async def schedule(self) -> GasSchedule:
        """The gas schedule in force, measured: a plain call to an account made for it."""
        call = empty_call()
        gas = int(await self.rpc(call.method, call.params), 16)
        schedule = schedule_of(gas)
        if schedule is None:
            raise ChainError(f"a fork no schedule knows: a plain call costs {gas}")
        return schedule

    async def base_fee(self) -> int:
        block = await self.rpc("eth_getBlockByNumber", ["latest", False])
        return int(block["baseFeePerGas"], 16)

    async def next_base_fee(self) -> int:
        """The next block's base fee: eth_feeHistory's last, which follows from the latest
        block's header."""
        history = await self.rpc("eth_feeHistory", [hex(1), "latest", []])
        return int(history["baseFeePerGas"][-1], 16)

    async def call(self, to: str, data: bytes) -> bytes:
        """eth_call of `data` to `to`, at the latest block."""
        result: str = await self.rpc("eth_call", [{"to": to, "data": "0x" + data.hex()}, "latest"])
        return bytes.fromhex(result[2:])

    async def fees(self, priority_fee: int) -> Fees:
        """Fees that stay valid through several blocks of rising base fee: twice the base fee,
        and `priority_fee`."""
        base = await self.base_fee()
        return Fees(max_fee_per_gas=2 * base + priority_fee, max_priority_fee_per_gas=priority_fee)

    async def receipt(self, tx_hash: bytes) -> dict[str, Any] | None:
        return await self.rpc("eth_getTransactionReceipt", ["0x" + tx_hash.hex()])

    async def send(self, tx: SignedTransaction, what: str, timeout: float) -> dict[str, Any]:
        """Send `tx` to the public mempool and wait up to `timeout` seconds for its receipt,
        which must be a success."""
        await self.rpc("eth_sendRawTransaction", ["0x" + tx.raw.hex()])
        for _ in range(int(timeout / RECEIPT_POLL_SECONDS) + 1):
            receipt = await self.receipt(tx.hash)
            if receipt is not None:
                if receipt["status"] != "0x1":
                    raise ChainError(f"{what} reverted: 0x{tx.hash.hex()}")
                return receipt
            await asyncio.sleep(RECEIPT_POLL_SECONDS)
        raise ChainError(f"{what}: no receipt for 0x{tx.hash.hex()} in {timeout}s")
