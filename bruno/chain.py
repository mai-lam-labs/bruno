"""A chain through its nodes: reads, fees, and sending a transaction and waiting for it.
Its calls are spread across the nodes, and what one fails is asked of another (Chain.batch)."""

import asyncio
import os
import random
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from math import ceil, floor

import httpx
import msgspec
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address

from . import datafee
from .common import Json
from .datafee import DataFee
from .forks import GasSchedule, schedule_of
from .jsonrpc import Answer, AnswerError, Call, JsonRpcError, Node, batch, call
from .transactions import Fees, SignedTransaction
from .units import Millis, Seconds

# the slot time on Ethereum and its testnets: the next block is a slot after the latest
SLOT = Seconds(12)
# how many times a slot something the chain hasn't done yet (a receipt, a deployment's code) is
# looked for: every 4 seconds on Ethereum
POLLS_PER_SLOT = 3


def poll_of(slot: Seconds) -> Millis:
    """How long between looks for something a chain of `slot` hasn't done yet."""
    return Millis(slot.millis // POLLS_PER_SLOT)


# Ethereum's: every 4 seconds
POLL = poll_of(SLOT)
# what of a slot passes, by default, before its block can be read at a chain's nodes: a second on
# Ethereum
SLOTS_A_BLOCK_DELAY = 12


def block_delay_of(slot: Seconds) -> Millis:
    """How long after a slot starts its block can be read, by default, on a chain of `slot`."""
    return Millis(slot.millis // SLOTS_A_BLOCK_DELAY)


# Ethereum's: a second
BLOCK_DELAY = block_delay_of(SLOT)


# the tip is read from the last TIP_BLOCKS blocks' median tips, and the last TIP_RECENT of
# them: enough history to be steady, and recent enough to rise within a few blocks
TIP_BLOCKS = 20
TIP_RECENT = 5
# EIP-1559: the base fee rises by at most an eighth a block
MAX_BASE_FEE_RISE = Fraction(9, 8)
# and the most it falls in one: by an eighth (EIP-1559)
MAX_BASE_FEE_FALL = Fraction(7, 8)


# how long a node that answers it's rate limited is left out, unless its Retry-After says
REST = Seconds(5)
# a Retry-After that's a number of seconds (it may be a date, which isn't read), of a length a
# node would mean
_RETRY_AFTER_SECONDS = re.compile(r"[0-9]{1,9}")
# how a node says a call is over its rate limit: EIP-1474's limit exceeded, or the HTTP status
# as the error's code (Alchemy's)
_RATE_LIMITED = frozenset({-32005, 429})
_TOO_MANY_REQUESTS = 429
# how a node says it hasn't a method
_METHOD_NOT_FOUND = -32601
# how a node says it hasn't a block it's asked at: EIP-1474's code, geth's message, and the
# start of anvil's (a block past its head, as after its snapshot's reverted)
_RESOURCE_NOT_FOUND = -32001
_HEADER_NOT_FOUND = "header not found"
_BLOCK_OUT_OF_RANGE = "BlockOutOfRangeError"
# the methods whose answer may be nothing (a transaction not mined, a block not built): any
# other answering nothing is its node without the block asked at
_MAY_BE_NOTHING = frozenset({"eth_getTransactionReceipt", "eth_getBlockByNumber"})


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


def unseen(answer: Answer) -> bool:
    """Whether a node answered a call at a block that it hasn't the block: EIP-1474's resource
    not found (-32001), geth's "header not found", anvil's BlockOutOfRangeError, or no result
    at all (Sepolia's nodes, 2026-10-26: publicnode and Tenderly the first, ethpandaops the
    last). A revert is another error (code 3)."""
    if answer.error is None:
        return answer.result is None
    message = answer.error.message
    return (
        answer.error.code == _RESOURCE_NOT_FOUND
        or message == _HEADER_NOT_FOUND
        or message.startswith(_BLOCK_OUT_OF_RANGE)
    )


def failure(url: str, e: Exception) -> str:
    """Why a node didn't answer, naming it by its host: an HTTP status, or the kind of failure
    (httpx's own message repeats the URL)."""
    if isinstance(e, httpx.HTTPStatusError):
        return f"{host(url)}: HTTP {e.response.status_code}"
    if isinstance(e, AnswerError):
        return f"{host(url)}: {e}"
    return f"{host(url)}: {type(e).__name__}"


@dataclass(frozen=True, slots=True)
class Block:
    """A block, as much of it as the relay reads: its number and its timestamp."""

    number: int
    timestamp: int

    @classmethod
    def of(cls, value: object, what: str = "a block") -> Block:
        """A block eth_getBlockByNumber answered, read: ChainError if it isn't one."""
        block = object_of(value, what)
        return cls(quantity(block.get("number")), quantity(block.get("timestamp")))


@dataclass(frozen=True, slots=True)
class FeeHistory:
    """eth_feeHistory's answer, as much of it as the relay reads: the base fee of the block
    after its last, and each of its blocks' tips at the percentile asked for, oldest first (0
    for a block without one: none of its transactions tipped)."""

    next_base_fee: int
    tips: tuple[int, ...]

    @classmethod
    def of(cls, value: object) -> FeeHistory:
        """A fee history eth_feeHistory answered, read: ChainError if it isn't one."""
        history = object_of(value, "eth_feeHistory")
        fees = list_of(history.get("baseFeePerGas"), "eth_feeHistory's baseFeePerGas")
        if not fees:
            raise ChainError("eth_feeHistory: no base fee")
        rewards = history.get("reward")
        tips = tuple(_tip(r) for r in ([] if rewards is None else list_of(rewards, "reward")))
        return cls(quantity(fees[-1]), tips)


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


def simulating_call() -> Call:
    """eth_simulateV1 of one plain call, at the latest block: a node that answers it has the
    method every simulation goes through."""
    call = {"to": "0x" + os.urandom(20).hex()}
    return Call("eth_simulateV1", [{"blockStateCalls": [{"calls": [call]}]}, "latest"])


@dataclass(frozen=True, slots=True)
class _Piece:
    """What one node is sent in one request: which of a batch's calls, by their indexes."""

    node: Node
    indexes: list[int]


class Chain:
    def __init__(
        self,
        client: httpx.AsyncClient,
        chain_id: int,
        nodes: Sequence[str | Node],
        slot: Seconds = SLOT,
        poll: Millis = POLL,
        block_delay: Millis = BLOCK_DELAY,
    ) -> None:
        """`nodes`: each a URL, or a node as configured; `slot`: its block time; `poll`:
        how long between looks for something it hasn't done yet (a receipt, a deployment's
        code), poll_of(slot) on a live chain; `block_delay`: how long after a slot starts its
        block can be read at these nodes."""
        self._client = client
        self.chain_id = chain_id
        self._nodes = tuple(Node(n) if isinstance(n, str) else n for n in nodes)
        self.slot = slot
        self.poll = poll
        self.block_delay = block_delay
        # the highest block `latest` has read: a node behind the others (a load balancer's
        # backend, say) answering an older one doesn't lower it
        self.seen = 0
        # the nodes left out for now, each until when (by the relay's clock): rate limited
        self._resting: dict[str, Millis] = {}

    def next_block_at(self, timestamp: int) -> Millis:
        """When the block after the one at `timestamp` can be read (by the wall clock, as block
        timestamps are): a slot on, and the chain's block delay."""
        return Millis(Seconds(timestamp + self.slot).millis + self.block_delay)

    @property
    def seen_tag(self) -> str:
        """The highest block seen, as a block tag: "latest" before any is."""
        return hex(self.seen) if self.seen else "latest"

    async def rpc(self, method: str, params: Sequence[object]) -> Json:
        """`method`'s result, asked as `batch` asks one call. A node's error answer
        (JsonRpcError, a revert included) is raised as it is."""
        (answer,) = await self.batch([Call(method, params)])
        return answer.value()

    async def batch(self, calls: Sequence[Call], *, atomic: bool = False) -> list[Answer]:
        """Each of `calls` answered, in the calls' order. They're spread across the nodes, in
        pieces of at most each node's `max_batch` sent at once, a call going only to a node
        that isn't configured without its method and isn't resting. What a node fails is asked
        of the nodes that haven't failed it: a piece whole (an HTTP error, a timeout, no JSON),
        or one call of it (rate limited, a method or a block the node hasn't). Any other error
        answer, a revert included, is the call's answer. A call no node is left to ask keeps
        the answer a node gave it, if one did; otherwise ChainError.

        `atomic`: every call at one node, for calls that are one view of the chain (the head
        and its fee history, a balance and the nonce beside it); what fails is asked again
        whole, of another node."""
        if atomic:
            return await self._together(calls)
        answers: list[Answer | None] = [None] * len(calls)
        failed: list[set[str]] = [set() for _ in calls]
        failures: list[str] = []
        pending = list(range(len(calls)))
        while pending:
            pieces = self._pieces(calls, pending, failed)
            asked = {i for piece in pieces for i in piece.indexes}
            unanswered = [calls[i] for i in pending if i not in asked and answers[i] is None]
            if unanswered:
                raise self._no_node(unanswered, failures)
            results = await asyncio.gather(
                *(self._ask(p.node, [calls[i] for i in p.indexes], failures) for p in pieces)
            )
            pending = []
            for piece, result in zip(pieces, results, strict=True):
                for n, i in enumerate(piece.indexes):
                    if result is not None:
                        answers[i] = result[n]
                    if result is None or self._failed(piece.node, calls[i], result[n]):
                        failed[i].add(piece.node.url)
                        pending.append(i)
        return [a for a in answers if a is not None]

    async def _together(self, calls: Sequence[Call]) -> list[Answer]:
        """`calls` at one node, in requests of at most its `max_batch`, one after another: the
        first node that fails none of them. If every node fails one, the last node's answers
        stand, if it answered them all; otherwise ChainError."""
        failures: list[str] = []
        last: list[Answer] | None = None
        for node in self._available({c.method for c in calls}):
            answers: list[Answer] = []
            for start in range(0, len(calls), node.max_batch):
                piece = await self._ask(node, calls[start : start + node.max_batch], failures)
                if piece is None:
                    break
                answers += piece
            else:
                faults = [self._failed(node, c, a) for c, a in zip(calls, answers, strict=True)]
                if not any(faults):
                    return answers
                last = answers
        if last is None:
            raise self._no_node(calls, failures)
        return last

    def _available(self, methods: Collection[str] = ()) -> list[Node]:
        """The nodes a request calling `methods` can go to, in the order it tries them: those
        not configured without one of them and not resting, shuffled each time, so the load
        is spread across them."""
        now = Millis.monotonic()
        nodes = [
            n
            for n in self._nodes
            if n.without.isdisjoint(methods) and self._resting.get(n.url, now) <= now
        ]
        return random.sample(nodes, len(nodes))

    def _pieces(
        self, calls: Sequence[Call], pending: Sequence[int], failed: Sequence[set[str]]
    ) -> list[_Piece]:
        """`pending` (indexes into `calls`) as pieces, each a node and the calls it's sent in
        one request: every available node in turn takes as many as its `max_batch` of the
        calls it can be asked (not one it's without, nor one it has failed), until none is
        left or no node can take what is."""
        left = list(pending)
        pieces: list[_Piece] = []
        nodes = self._available()
        taking = True
        while left and taking:
            taking = False
            for node in nodes:
                indexes = [
                    i
                    for i in left
                    if calls[i].method not in node.without and node.url not in failed[i]
                ][: node.max_batch]
                if indexes:
                    taking = True
                    pieces.append(_Piece(node, indexes))
                    left = [i for i in left if i not in indexes]
        return pieces

    async def _ask(
        self, node: Node, calls: Sequence[Call], failures: list[str]
    ) -> list[Answer] | None:
        """`calls` in one request to `node` (one call as a plain request, more as a batch):
        their answers, or None if the node failed the request, why added to `failures`. A node
        answering 429 rests."""
        try:
            if len(calls) == 1:
                only = calls[0]
                try:
                    return [Answer(await call(self._client, node.url, only.method, only.params))]
                except JsonRpcError as e:
                    return [Answer(None, e)]
            return await batch(self._client, node.url, calls)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == _TOO_MANY_REQUESTS:
                self._rest(node, e.response.headers.get("retry-after"))
            failures.append(failure(node.url, e))
            return None
        except (httpx.HTTPError, AnswerError) as e:
            failures.append(failure(node.url, e))
            return None

    def _failed(self, node: Node, asked: Call, answer: Answer) -> bool:
        """Whether `answer` is `node` failing the call and not the call's answer, so another
        node is asked: rate limited (the node rests), a method it hasn't, or a block it
        hasn't."""
        if answer.error is None:
            return answer.result is None and asked.method not in _MAY_BE_NOTHING
        if answer.error.code in _RATE_LIMITED:
            self._rest(node, None)
            return True
        return answer.error.code == _METHOD_NOT_FOUND or unseen(answer)

    def _rest(self, node: Node, retry_after: str | None) -> None:
        """`node` left out of every request for its Retry-After's seconds, or REST without
        one (or with a date, which isn't read)."""
        rest = REST.millis
        if retry_after is not None and _RETRY_AFTER_SECONDS.fullmatch(retry_after):
            rest = Millis.of_seconds(int(retry_after))
        self._resting[node.url] = Millis(Millis.monotonic() + rest)

    def _no_node(self, calls: Sequence[Call], failures: Sequence[str]) -> ChainError:
        """The error for `calls` no node answered: each node's failure, and the nodes resting."""
        now = Millis.monotonic()
        resting = [
            f"{host(n.url)}: resting" for n in self._nodes if self._resting.get(n.url, now) > now
        ]
        methods = ", ".join(dict.fromkeys(c.method for c in calls))
        return ChainError(f"{methods}: no node answered: {'; '.join([*failures, *resting])}")

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
            ],
            atomic=True,
        )
        block = Block.of(answers[0].value(), "the latest block")
        history = FeeHistory.of(answers[1].value())
        self.seen = max(self.seen, block.number)
        return Latest(
            block.number,
            block.timestamp,
            history.next_base_fee,
            history.tips,
            datafee.read([word_of(a.value()) for a in answers[2:]]) if op_stack else None,
        )

    async def check_chain(self) -> None:
        """Raise unless the nodes are the chain this was made for."""
        chain_id = quantity(await self.rpc("eth_chainId", []))
        if chain_id != self.chain_id:
            raise ChainError(f"the nodes are chain {chain_id}, not {self.chain_id}")

    async def balance(self, account: str) -> int:
        return quantity(await self._at_seen("eth_getBalance", account))

    async def nonce(self, account: str) -> int:
        return quantity(await self._at_seen("eth_getTransactionCount", account))

    async def code(self, account: str) -> bytes:
        return code_of(await self._at_seen("eth_getCode", account))

    async def _at_seen(self, method: str, account: str) -> Json:
        """`method` of `account` at the highest block seen (a head read, or the receipt of a
        transaction sent). Asked at "latest", a node behind answers from its own older head, and
        a nonce read there has the next transaction refused as too low; asked at a block it
        hasn't, it can only say so, and another node is asked. A block no node has is waited
        for, two slots: the chain reorganised below it, or a local chain un-mined it. After
        that the chain is taken as the nodes have it now, and read at its head."""
        for _ in range(2 * POLLS_PER_SLOT):
            (answer,) = await self.batch([Call(method, [account, self.seen_tag])])
            if not unseen(answer):
                return answer.value()
            await asyncio.sleep(self.poll.seconds)
        self.seen = quantity(await self.rpc("eth_blockNumber", []))
        return await self.rpc(method, [account, self.seen_tag])

    async def op_stack(self) -> bool:
        """Whether it's an OP-stack chain: its fee oracle has code. The relay measures this
        (measurements.measure); the tools that send without measuring ask."""
        return bool(await self.code(datafee.GAS_PRICE_ORACLE))

    async def head(self) -> int:
        """The latest block's number, as a node has it: seen."""
        head = quantity(await self.rpc("eth_blockNumber", []))
        self.seen = max(self.seen, head)
        return head

    async def finality(self) -> Finality:
        """The latest block and the finalized one, in one request."""
        latest, finalized = (
            Block.of(a.value())
            for a in await self.batch(
                [
                    Call("eth_getBlockByNumber", ["latest", False]),
                    Call("eth_getBlockByNumber", ["finalized", False]),
                ],
                atomic=True,
            )
        )
        return Finality(latest.number, latest.timestamp, finalized.number)

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
        self.seen = max(self.seen, quantity(block.get("number")))
        return quantity(block.get("baseFeePerGas"))

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

    async def receipt(self, tx_hash: bytes) -> dict[str, object] | None:
        """A transaction's receipt, whole, as its node answers it: None if it has none."""
        receipt = await self.rpc("eth_getTransactionReceipt", ["0x" + tx_hash.hex()])
        return None if receipt is None else object_of(receipt, "a receipt")

    async def send_raw(self, raw: bytes) -> None:
        """The signed transaction `raw` to the public mempool, not waited for."""
        await self.rpc("eth_sendRawTransaction", ["0x" + raw.hex()])

    async def send(self, tx: SignedTransaction, what: str, timeout: float) -> dict[str, object]:
        """Send `tx` to the public mempool and wait up to `timeout` seconds for its receipt,
        which must be a success. Its block is seen: what's read next is read after it."""
        await self.send_raw(tx.raw)
        for _ in range(int(timeout / self.poll.seconds) + 1):
            receipt = await self.receipt(tx.hash)
            if receipt is not None:
                self.seen = max(self.seen, quantity(receipt.get("blockNumber")))
                if Receipt.of(receipt).status != 1:
                    raise ChainError(f"{what} reverted: 0x{tx.hash.hex()}")
                return receipt
            await asyncio.sleep(self.poll.seconds)
        raise ChainError(f"{what}: no receipt for 0x{tx.hash.hex()} in {timeout}s")
