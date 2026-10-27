"""A chain through its nodes: reads, fees, and sending a transaction and waiting for it.
Each call goes to the first node that answers."""

import asyncio
import os
import random
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from math import ceil
from typing import Any

import httpx

from . import datafee
from .datafee import DataFee
from .forks import GasSchedule, schedule_of
from .net.jsonrpc import Answer, AnswerError, Call, Node, batch, call
from .transactions import Fees, SignedTransaction

# how often a sent transaction's receipt is looked for: an engineering bound, under a slot
RECEIPT_POLL_SECONDS = 2
# the slot time on Ethereum and its testnets: the next block is a slot after the latest
SLOT_SECONDS = 12
# the tip is read from the last TIP_BLOCKS blocks' median tips, and the last TIP_RECENT of
# them: enough history to be steady, and recent enough to rise within a few blocks
TIP_BLOCKS = 20
TIP_RECENT = 5
# EIP-1559: the base fee rises by at most an eighth a block
MAX_BASE_FEE_RISE = Fraction(9, 8)


class ChainError(Exception):
    """The nodes failed, or a transaction did: unexpected, not a client's doing, so not a
    BrunoError."""


def host(url: str) -> str:
    """A node named without what its URL may carry (an API key in its path or query)."""
    return httpx.URL(url).host


def failure(url: str, e: Exception) -> str:
    """Why a node didn't answer, naming it by its host: an HTTP status, or the kind of failure
    (httpx's own message repeats the URL)."""
    if isinstance(e, httpx.HTTPStatusError):
        return f"{host(url)}: HTTP {e.response.status_code}"
    if isinstance(e, AnswerError):
        return f"{host(url)}: {e}"
    return f"{host(url)}: {type(e).__name__}"


@dataclass(frozen=True, slots=True)
class Latest:
    """The latest block, the next one's base fee, and the median tip of each of the last
    TIP_BLOCKS blocks, oldest first; on an OP-stack chain, the inputs of what a transaction
    pays beyond gas: what a slot's work is priced and scheduled by, read once."""

    number: int
    timestamp: int
    base_fee_per_gas: int
    next_base_fee: int
    tips: tuple[int, ...]
    data_fee: DataFee | None = None


@dataclass(frozen=True, slots=True)
class Finality:
    """The latest block's number and timestamp, and the finalized block's number."""

    head: int
    timestamp: int
    finalized: int


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
    them, and `priority_fee`; what's paid beyond gas, its L1 fees risen for them too. A
    transaction pays only the base fee of its block, and the tip."""
    data_fee = None if latest.data_fee is None else latest.data_fee.risen(blocks)
    return Fees(risen(latest.next_base_fee, blocks) + priority_fee, priority_fee, data_fee)


def empty_call() -> Call:
    """eth_estimateGas of a plain call to an account made for it: no code, no value, no data.
    Its gas is the fork's base transaction cost, which tells the forks apart (schedule_of)."""
    return Call("eth_estimateGas", [{"to": "0x" + os.urandom(20).hex()}])


class Chain:
    def __init__(
        self,
        client: httpx.AsyncClient,
        chain_id: int,
        nodes: Sequence[str | Node],
        slot_seconds: int = SLOT_SECONDS,
    ) -> None:
        """`nodes`: each a URL, or a node with its batch cap; `slot_seconds`: its block time."""
        self._client = client
        self.chain_id = chain_id
        self._nodes = tuple(Node(n) if isinstance(n, str) else n for n in nodes)
        self.slot_seconds = slot_seconds
        # the highest block `latest` has read: a node behind the others (a load balancer's
        # backend, say) answering an older one doesn't lower it
        self.seen = 0

    def nodes(self) -> list[Node]:
        """The nodes in the order a request tries them: shuffled each time, so the load is
        spread across them, and a node that refuses (a public one rate-limiting, say) is
        passed over for the rest."""
        return random.sample(self._nodes, len(self._nodes))

    async def rpc(self, method: str, params: list[Any]) -> Any:
        """`method` at the first node that answers. A node's error answer (JsonRpcError, a
        revert included) is raised as it is: the next node would answer the same."""
        failures: list[str] = []
        for node in self.nodes():
            try:
                return await call(self._client, node.url, method, params)
            except (httpx.HTTPError, AnswerError) as e:
                failures.append(failure(node.url, e))
        raise ChainError(f"{method}: no node answered: {'; '.join(failures)}")

    async def batch(self, calls: Sequence[Call]) -> list[Answer]:
        """Each of `calls`, at the first node that answers: as batches of at most its
        `max_batch`, a node that fails leaving the rest to the next. The answers in the calls'
        order, a node's error answer to one call its own, not the batch's."""
        answers: list[Answer] = []
        failures: list[str] = []
        for node in self.nodes():
            try:
                while len(answers) < len(calls):
                    start = len(answers)
                    chunk = calls[start : start + node.max_batch]
                    answers += await batch(self._client, node.url, chunk)
                return answers
            except (httpx.HTTPError, AnswerError) as e:
                failures.append(failure(node.url, e))
        methods = ", ".join(sorted({c.method for c in calls[len(answers) :]}))
        raise ChainError(f"{methods}: no node answered: {'; '.join(failures)}")

    async def latest(self, op_stack: bool = False) -> Latest:
        """The latest block, the next one's base fee (eth_feeHistory's last) and the last
        TIP_BLOCKS blocks' median tips (its 50th percentile), in one request; on an OP-stack
        chain (`op_stack`, as measured) its data fee's inputs with them. A block without a
        reward (none of its transactions tipped) tips 0."""
        answers = await self.batch(
            [
                Call("eth_getBlockByNumber", ["latest", False]),
                Call("eth_feeHistory", [hex(TIP_BLOCKS), "latest", [50]]),
                *(datafee.reading() if op_stack else ()),
            ]
        )
        block, history = answers[0].value(), answers[1].value()
        number = int(block["number"], 16)
        self.seen = max(self.seen, number)
        return Latest(
            number,
            int(block["timestamp"], 16),
            int(block["baseFeePerGas"], 16),
            int(history["baseFeePerGas"][-1], 16),
            tuple(int(r[0], 16) if r else 0 for r in history.get("reward") or []),
            datafee.read(answers[2:]) if op_stack else None,
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

    async def op_stack(self) -> bool:
        """Whether it's an OP-stack chain: its fee oracle has code. The relay measures this
        (measurements.measure); the tools that send without measuring ask."""
        return await self.code(datafee.GAS_PRICE_ORACLE) not in ("0x", "0x0")

    async def head(self) -> int:
        return int(await self.rpc("eth_blockNumber", []), 16)

    async def finality(self) -> Finality:
        """The latest block and the finalized one, in one request."""
        latest, finalized = (
            a.value()
            for a in await self.batch(
                [
                    Call("eth_getBlockByNumber", ["latest", False]),
                    Call("eth_getBlockByNumber", ["finalized", False]),
                ]
            )
        )
        return Finality(
            int(latest["number"], 16), int(latest["timestamp"], 16), int(finalized["number"], 16)
        )

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
