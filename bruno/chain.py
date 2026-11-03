"""A chain through its nodes: reads, fees, and sending a transaction and waiting for it.
Each call goes to the first node that answers."""

import asyncio
import os
import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from math import ceil, floor
from typing import Any

import httpx
import msgspec
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address

from . import datafee
from .datafee import DataFee
from .forks import GasSchedule, schedule_of
from .jsonrpc import Answer, AnswerError, Call, JsonRpcError, Node, batch, call
from .transactions import Fees, SignedTransaction
from .units import Seconds

# how often a sent transaction's receipt is looked for: an engineering bound, under a slot
RECEIPT_POLL_SECONDS = 2
# the slot time on Ethereum and its testnets: the next block is a slot after the latest
SLOT = Seconds(12)
# the tip is read from the last TIP_BLOCKS blocks' median tips, and the last TIP_RECENT of
# them: enough history to be steady, and recent enough to rise within a few blocks
TIP_BLOCKS = 20
TIP_RECENT = 5
# EIP-1559: the base fee rises by at most an eighth a block
MAX_BASE_FEE_RISE = Fraction(9, 8)
# and the most it falls in one: by an eighth (EIP-1559)
MAX_BASE_FEE_FALL = Fraction(7, 8)


class ChainError(Exception):
    """The nodes failed, or a transaction did: unexpected, not a client's doing, so not a
    BrunoError."""


# what a node's quantity is: hex digits after 0x (leading zeros let through, as nodes differ);
# and a 32-byte word, as an eth_call returning one value answers
_QUANTITY = re.compile(r"0x[0-9a-fA-F]+")
_WORD = re.compile(r"0x[0-9a-fA-F]{64}")
# data: whole bytes after 0x
_DATA = re.compile(r"0x(?:[0-9a-fA-F]{2})*")


def quantity(value: object) -> int:
    """A node's quantity: ChainError, the node's failure, if it isn't one."""
    if not isinstance(value, str) or _QUANTITY.fullmatch(value) is None:
        raise ChainError(f"not a quantity: {value!r}")
    return int(value, 16)


def object_of(value: object, what: str) -> dict[str, object]:
    """A node's object (a block, a fee history): ChainError if it isn't one."""
    try:
        return msgspec.convert(value, dict[str, object])
    except msgspec.ValidationError as e:
        raise ChainError(f"{what}: not an object: {value!r}") from e


def list_of(value: object, what: str) -> list[object]:
    """A node's list: ChainError if it isn't one."""
    try:
        return msgspec.convert(value, list[object])
    except msgspec.ValidationError as e:
        raise ChainError(f"{what}: not a list: {value!r}") from e


def _tip(reward: object) -> int:
    """A block's median tip, eth_feeHistory's reward for it: 0 if it has none (none of its
    transactions tipped)."""
    rewards = list_of(reward, "reward")
    return quantity(rewards[0]) if rewards else 0


def hex_of(value: object) -> str:
    """A node's data, as hex: ChainError if it isn't whole bytes after 0x."""
    if not isinstance(value, str) or _DATA.fullmatch(value) is None:
        raise ChainError(f"not hex: {value!r}")
    return value


def data_of(value: object) -> bytes:
    """A node's data."""
    return bytes.fromhex(hex_of(value)[2:])


def code_of(value: object) -> bytes:
    """An account's code, as eth_getCode answers it: nodes answer one without code "0x", some
    "0x0"."""
    return b"" if value == "0x0" else data_of(value)


def has_code(value: object) -> bool:
    """Whether eth_getCode's answer is any code."""
    return bool(code_of(value))


@dataclass(frozen=True, slots=True)
class Receipt:
    """A transaction's receipt, as much of it as the relay reads: its status (1 a success) and
    its block."""

    status: int
    block_number: int

    @classmethod
    def of(cls, receipt: Mapping[str, object]) -> Receipt:
        """A receipt eth_getTransactionReceipt answered, read: ChainError if it isn't one."""
        return cls(quantity(receipt.get("status")), quantity(receipt.get("blockNumber")))


def receipt_of(value: object) -> Receipt | None:
    """A transaction's receipt, as eth_getTransactionReceipt answers it: None if there's none,
    ChainError if it isn't one."""
    return None if value is None else Receipt.of(object_of(value, "a receipt"))


def revert_data(error: JsonRpcError) -> bytes:
    """A revert's data, as a node's error answer carries it (EIP-1474's execution error): none
    if it gives none; ChainError if what it gives isn't data."""
    return b"" if not isinstance(error.data, str) else data_of(error.data)


def word_bytes(value: object) -> bytes:
    """A node's 32-byte word, as an eth_call returning one value answers: ChainError if it
    isn't one."""
    if not isinstance(value, str) or _WORD.fullmatch(value) is None:
        raise ChainError(f"not a 32-byte word: {value!r}")
    return bytes.fromhex(value[2:])


def word_of(value: object) -> int:
    """A node's 32-byte word, as a uint256."""
    return int.from_bytes(word_bytes(value))


def address_of(word: bytes) -> ChecksumAddress:
    """The address a 32-byte ABI word holds: ChainError if it isn't 32 bytes."""
    if len(word) != 32:
        raise ChainError(f"not a 32-byte word: {len(word)} bytes")
    return to_checksum_address(word[12:])


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


def risen_base_fee(base_fee: int, blocks: int) -> int:
    """The most `base_fee` can be `blocks` blocks later: risen by an eighth each block,
    rounded up."""
    return ceil(base_fee * MAX_BASE_FEE_RISE**blocks)


def fallen_base_fee(base_fee: int, blocks: int) -> int:
    """The least `base_fee` can be `blocks` blocks later: fallen by an eighth each block,
    rounded down."""
    return floor(base_fee * MAX_BASE_FEE_FALL**blocks)


def lasting_fees(latest: Latest, priority_fee: int, blocks: int) -> Fees:
    """Fees valid for `blocks` blocks however the base fee rises: the next base fee risen for
    them, and `priority_fee`; what's paid beyond gas, its L1 fees risen for them too. A
    transaction pays only the base fee of its block, and the tip."""
    data_fee = None if latest.data_fee is None else latest.data_fee.risen(blocks)
    return Fees(risen_base_fee(latest.next_base_fee, blocks) + priority_fee, priority_fee, data_fee)


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
        slot: Seconds = SLOT,
    ) -> None:
        """`nodes`: each a URL, or a node with its batch cap; `slot`: its block time."""
        self._client = client
        self.chain_id = chain_id
        self._nodes = tuple(Node(n) if isinstance(n, str) else n for n in nodes)
        self.slot = slot
        # the highest block `latest` has read: a node behind the others (a load balancer's
        # backend, say) answering an older one doesn't lower it
        self.seen = 0

    @property
    def seen_tag(self) -> str:
        """The highest block seen, as a block tag: "latest" before any is."""
        return hex(self.seen) if self.seen else "latest"

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
        methods = ", ".join(dict.fromkeys(c.method for c in calls[len(answers) :]))
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
        block = object_of(answers[0].value(), "the latest block")
        history = object_of(answers[1].value(), "eth_feeHistory")
        fees = list_of(history.get("baseFeePerGas"), "eth_feeHistory's baseFeePerGas")
        if not fees:
            raise ChainError("eth_feeHistory: no base fee")
        rewards = history.get("reward")
        number = quantity(block.get("number"))
        self.seen = max(self.seen, number)
        return Latest(
            number,
            quantity(block.get("timestamp")),
            quantity(fees[-1]),
            tuple(_tip(r) for r in ([] if rewards is None else list_of(rewards, "reward"))),
            datafee.read([word_of(a.value()) for a in answers[2:]]) if op_stack else None,
        )

    async def check_chain(self) -> None:
        """Raise unless the nodes are the chain this was made for."""
        chain_id = quantity(await self.rpc("eth_chainId", []))
        if chain_id != self.chain_id:
            raise ChainError(f"the nodes are chain {chain_id}, not {self.chain_id}")

    async def balance(self, account: str) -> int:
        return quantity(await self.rpc("eth_getBalance", [account, "latest"]))

    async def nonce(self, account: str) -> int:
        return quantity(await self.rpc("eth_getTransactionCount", [account, "latest"]))

    async def code(self, account: str) -> str:
        return await self.rpc("eth_getCode", [account, "latest"])

    async def op_stack(self) -> bool:
        """Whether it's an OP-stack chain: its fee oracle has code. The relay measures this
        (measurements.measure); the tools that send without measuring ask."""
        return has_code(await self.code(datafee.GAS_PRICE_ORACLE))

    async def head(self) -> int:
        return quantity(await self.rpc("eth_blockNumber", []))

    async def finality(self) -> Finality:
        """The latest block and the finalized one, in one request."""
        latest, finalized = (
            object_of(a.value(), "a block")
            for a in await self.batch(
                [
                    Call("eth_getBlockByNumber", ["latest", False]),
                    Call("eth_getBlockByNumber", ["finalized", False]),
                ]
            )
        )
        return Finality(
            quantity(latest.get("number")),
            quantity(latest.get("timestamp")),
            quantity(finalized.get("number")),
        )

    async def schedule(self) -> GasSchedule:
        """The gas schedule in force, measured: a plain call to an account made for it."""
        call = empty_call()
        gas = quantity(await self.rpc(call.method, call.params))
        schedule = schedule_of(gas)
        if schedule is None:
            raise ChainError(f"a fork no schedule knows: a plain call costs {gas}")
        return schedule

    async def base_fee(self) -> int:
        block = object_of(await self.rpc("eth_getBlockByNumber", ["latest", False]), "a block")
        return quantity(block.get("baseFeePerGas"))

    async def next_base_fee(self) -> int:
        """The next block's base fee: eth_feeHistory's last, which follows from the latest
        block's header."""
        history = object_of(await self.rpc("eth_feeHistory", [hex(1), "latest", []]), "fees")
        fees = list_of(history.get("baseFeePerGas"), "eth_feeHistory's baseFeePerGas")
        if not fees:
            raise ChainError("eth_feeHistory: no base fee")
        return quantity(fees[-1])

    async def call(self, to: str, data: bytes) -> bytes:
        """eth_call of `data` to `to`, at the latest block."""
        return data_of(
            await self.rpc("eth_call", [{"to": to, "data": "0x" + data.hex()}, "latest"])
        )

    async def fees(self, priority_fee: int) -> Fees:
        """Fees that stay valid through several blocks of rising base fee: twice the base fee,
        and `priority_fee`."""
        base = await self.base_fee()
        return Fees(max_fee_per_gas=2 * base + priority_fee, max_priority_fee_per_gas=priority_fee)

    async def receipt(self, tx_hash: bytes) -> dict[str, Any] | None:
        return await self.rpc("eth_getTransactionReceipt", ["0x" + tx_hash.hex()])

    async def send_raw(self, raw: bytes) -> None:
        """The signed transaction `raw` to the public mempool, not waited for."""
        await self.rpc("eth_sendRawTransaction", ["0x" + raw.hex()])

    async def send(self, tx: SignedTransaction, what: str, timeout: float) -> dict[str, Any]:
        """Send `tx` to the public mempool and wait up to `timeout` seconds for its receipt,
        which must be a success."""
        await self.send_raw(tx.raw)
        for _ in range(int(timeout / RECEIPT_POLL_SECONDS) + 1):
            receipt = await self.receipt(tx.hash)
            if receipt is not None:
                if Receipt.of(receipt).status != 1:
                    raise ChainError(f"{what} reverted: 0x{tx.hash.hex()}")
                return receipt
            await asyncio.sleep(RECEIPT_POLL_SECONDS)
        raise ChainError(f"{what}: no receipt for 0x{tx.hash.hex()} in {timeout}s")
