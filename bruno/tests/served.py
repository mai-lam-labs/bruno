"""The relay under test, whichever runs it: the Python relay in-process, or the Worker
under workerd. Its fixtures (`backend`, `served`) are the tests' through conftest.py, so a test
asking for `served` runs against both."""

import asyncio
import json
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from math import ceil

import httpx
import msgspec
import pytest
import uvicorn
from eth_account import Account
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address
from litestar import Litestar

from bruno.bundle import Mode
from bruno.chain import ChainError
from bruno.config import (
    BuilderConfig,
    ChainConfig,
    Hop,
    MinFee,
    PoolKind,
    Token,
    TreasuryConfig,
)
from bruno.evm import NATIVE_ASSET
from bruno.forks import GasSchedule
from bruno.jsonrpc import JsonRpcError, Node
from bruno.relay import ClockState, Relay, open_relay
from bruno.server.app import create_app
from bruno.server.operations import node_failed
from bruno.tests.deployments import MAX_GAS
from bruno.tests.localchain import (
    SWAP_ROUTER,
    TEST_FLOAT,
    UNISWAP_V2_USDC_WETH,
    UNISWAP_V3_USDC_USDT,
    UNISWAP_V3_USDC_WETH,
    USDC,
    USDC_DOMAIN,
    USDT,
    WETH,
    LocalChain,
)
from bruno.tests.mockbuilder import BuiltBlock, MockBuilder, Reply, serve_json
from bruno.treasury import Treasury

PAYEE_ACCOUNT = 8
# its key, anvil's: the treasury's
PAYEE_KEY = Account.from_key("0xdbda1821b80551c9d65939329250298aa3472ba22feea921c0cf5d620ea67b97")
JSON = {"content-type": "application/json"}
MAX_DEADLINE_BLOCKS = 50
# the blocks land() gives a funding that hasn't landed
FUNDING_BLOCKS = 3
# how long an x402 request is held at most, in blocks: the configuration's default
X402_DEADLINE_BLOCKS = 10
# the mock builder's port, served over HTTP to either relay (beside anvil's and workerd's), the
# port of the Worker's way to anvil, which records its requests, of a node that answers every
# request with a page, and of a second way to anvil, recorded too
BUILDER_PORT = 8548
NODE_PORT = 8549
PAGE_NODE_PORT = 8550
SECOND_NODE_PORT = 8551
BUILDER_URL = f"http://127.0.0.1:{BUILDER_PORT}/"
PAGE_NODE_URL = f"http://127.0.0.1:{PAGE_NODE_PORT}/"
SECOND_NODE_URL = f"http://127.0.0.1:{SECOND_NODE_PORT}/"
# how a node says a call is over its rate limit: EIP-1474's limit exceeded
LIMITED: dict[str, object] = {"code": -32005, "message": "rate limit exceeded"}
# what a node behind a failing proxy answers: a web page, not JSON-RPC
PAGE = b"<html><body>502 Bad Gateway</body></html>"
# how nodes say they haven't the block asked at: geth, and anvil (a block past its head)
GETH_UNSEEN: dict[str, object] = {"code": -32000, "message": "header not found"}
ANVIL_UNSEEN: dict[str, object] = {
    "code": -32602,
    "message": "BlockOutOfRangeError: block height is 20 but requested was 23",
}
# what one request to the Worker may take: a block's work, simulations and bundles included
WORKER_TIMEOUT_SECONDS = 60
# a fast chain, for the relay's own clock: a block every 2 seconds, a batch every second
FAST_SLOT_SECONDS = 2
FAST_BATCH_SECONDS = 1.0
# on the relay's own clock: the most slots a wait for it gives the relay to get there (a
# submission landing: its batch, its funding's block, its own, and the block reconciling it,
# with room to spare), so a relay that never does fails rather than hangs
CLOCK_SLOTS = 15
# how often a wait looks again, in a slot
LOOKS_PER_SLOT = 8
FLOAT_ETH = 10**18


@dataclass(frozen=True, slots=True)
class Setup:
    """What a test asks of the relay: its builder's mode, its batch and queue sizes, and its
    funding's headroom and patience."""

    mode: Mode = Mode.DROPPING
    accounts: int = 2
    max_pending: int = 4
    funding_multiplier: str = "1"
    batch_seconds: float = 12.0
    # a local chain: stepping as fast as the tests need (min_step_seconds' default is 5)
    min_step_seconds: float = 0.1
    slot_seconds: int = 12
    retention_seconds: float = 3600.0
    max_authorizations: int = 1
    measure_seconds: float = 3600.0
    # a second node, answering every request with a page
    page_node: bool = False
    # the chain's own node listed as rpc_extra, beside the page node as its rpc
    extra_node: bool = False
    # a second node that answers as the first does (Faults.second), configured without these
    # methods
    second_node: bool = False
    second_without: tuple[str, ...] = ()
    # ETH among the tokens taken
    eth: bool = True


@dataclass(slots=True)
class Faults:
    """What the node and the builder get wrong, as a test sets it: the node answering a web
    page to every request (`pages`), or once to each method in `page_once`; answering geth's
    "header not found" (a node behind the block asked at), or `unseen` instead, once to each
    method in `unseen_once`; refusing a call to a method in `refused_once`, unforwarded, each method once
    (as a node refuses a transaction); answering each call to a method in `garbled` with what it
    maps the call's params and true result to (a node's malformed answer), while it's there;
    answering each call to a method in `missing` that it has no such method (-32601), alone or in
    a batch, while it's there, and each to one in `limited` that it's over its rate limit
    (-32005); the builder holding each bundle unanswered until `released`. The second node
    (Setup.second_node) answers as the chain's does, each request's methods recorded in
    `second` and when it came (the monotonic clock) in `second_at`, or with `second_reply`
    while that's set."""

    pages: bool = False
    page_once: set[str] = field(default_factory=set[str])
    unseen_once: set[str] = field(default_factory=set[str])
    unseen_error: dict[str, object] = field(default_factory=lambda: dict(GETH_UNSEEN))
    refused_once: set[str] = field(default_factory=set[str])
    garbled: dict[str, Callable[[object, object], object]] = field(
        default_factory=dict[str, Callable[[object, object], object]]
    )
    missing: set[str] = field(default_factory=set[str])
    limited: set[str] = field(default_factory=set[str])
    second: list[list[str]] = field(default_factory=list[list[str]])
    second_at: list[float] = field(default_factory=list[float])
    second_reply: Reply | None = None
    builder_hangs: bool = False
    released: asyncio.Event = field(default_factory=asyncio.Event)
    # how many requests were answered with a page, the page node's included
    paged: int = 0

    def page(self, called: list[str]) -> bool:
        """Whether the node answers a request calling `called` with a page."""
        once = self.page_once.intersection(called)
        self.page_once -= once
        paged = self.pages or bool(once)
        self.paged += paged
        return paged

    def refusal(self, body: bytes) -> bytes | None:
        """The node's refusal of `body`, if it's one call to a method in `refused_once`: an
        error answer, the call never reaching the node."""
        request = json.loads(body)
        if isinstance(request, list) or request["method"] not in self.refused_once:
            return None
        self.refused_once.discard(request["method"])
        error = {"code": -32000, "message": "nonce too low"}
        return json.dumps({"jsonrpc": "2.0", "id": request["id"], "error": error}).encode()

    def unseen(self, body: bytes, answer: bytes) -> bytes:
        """The node's `answer` to `body`, its calls to a method in `unseen_once` answered with
        `unseen_error` instead, each method once."""
        request = json.loads(body)
        calls = request if isinstance(request, list) else [request]
        ids = {call["id"] for call in calls if call["method"] in self.unseen_once}
        if not ids:
            return answer
        self.unseen_once -= {call["method"] for call in calls}
        answered = json.loads(answer)
        for each in answered if isinstance(answered, list) else [answered]:
            if each.get("id") in ids:
                each.pop("result", None)
                each["error"] = self.unseen_error
        return json.dumps(answered).encode()

    def without(self, body: bytes, answer: bytes) -> bytes:
        """The node's `answer` to `body`, its calls to a method in `missing` answered as a node
        without the method answers: geth's error for it."""
        request = json.loads(body)
        calls = request if isinstance(request, list) else [request]
        refused = self.missing | self.limited
        methods = {call["id"]: call["method"] for call in calls if call["method"] in refused}
        if not methods:
            return answer
        answered = json.loads(answer)
        for each in answered if isinstance(answered, list) else [answered]:
            if (method := methods.get(each.get("id"))) is not None:
                each.pop("result", None)
                why = f"the method {method} does not exist/is not available"
                missing = {"code": -32601, "message": why}
                each["error"] = LIMITED if method in self.limited else missing
        return json.dumps(answered).encode()

    def rewritten(self, body: bytes, answer: bytes) -> bytes:
        """The node's `answer` to `body`, as the test has it go wrong: unseen, a method
        missing, or garbled."""
        answer = self.without(body, self.unseen(body, answer))
        request = json.loads(body)
        calls = request if isinstance(request, list) else [request]
        garbled = {call["id"]: call for call in calls if call["method"] in self.garbled}
        if not garbled:
            return answer
        answered = json.loads(answer)
        for each in answered if isinstance(answered, list) else [answered]:
            if (call := garbled.get(each.get("id"))) is not None and "result" in each:
                each["result"] = self.garbled[call["method"]](call.get("params"), each["result"])
        return json.dumps(answered).encode()

    async def page_node(self, _body: bytes) -> bytes:
        """The page node's answer to any request."""
        self.paged += 1
        return PAGE


@dataclass(slots=True)
class Pace:
    """The chain's pace in a test on the relay's own clock: a block a slot apart, built by the
    mock builder, so the chain's clock keeps to the wall clock (each test starts on it:
    conftest.py's chain); every wait in slots, looked at LOOKS_PER_SLOT times a slot, blocks
    built as they fall due."""

    builder: MockBuilder
    slot_seconds: float
    # how long the relay keeps a finished submission (its setup's)
    retention_seconds: float
    # when the last block was built, or the pace began
    last: float = field(default_factory=time.monotonic)

    @property
    def forgetting_slots(self) -> int:
        """The most slots a finished submission waits to be forgotten: its retention, and the
        relay's step after it."""
        return ceil(self.retention_seconds / self.slot_seconds) + 1

    async def block(self) -> BuiltBlock:
        """The next block, built once a slot has passed since the last."""
        await asyncio.sleep(max(0.0, self.last + self.slot_seconds - time.monotonic()))
        self.last = time.monotonic()
        return await self.builder.build_block()

    async def until[T](
        self, read: Callable[[], Awaitable[T]], done: Callable[[T], bool], slots: int
    ) -> T:
        """`read` looked at until `done`, blocks built as they fall due: its last value. Not
        within `slots` slots, an AssertionError saying what it last was."""
        deadline = time.monotonic() + slots * self.slot_seconds
        while True:
            value = await read()
            if done(value):
                return value
            if time.monotonic() >= deadline:
                raise AssertionError(f"not within {slots} slots: {value!r}")
            if time.monotonic() >= self.last + self.slot_seconds:
                await self.block()
            else:
                await asyncio.sleep(self.slot_seconds / LOOKS_PER_SLOT)


@dataclass(frozen=True, slots=True)
class Served:
    """The relay under test, whichever runs it (the Python app, or the Worker under workerd):
    its interface, its chain and payee, the mock builder it sends to, and its batches and
    blocks, driven as the test asks."""

    http: httpx.AsyncClient
    chain: LocalChain
    chain_id: int
    payee: ChecksumAddress
    builder: MockBuilder
    close_batch: Callable[[], Awaitable[None]]
    # the block's work, as the clock does it: whether a funding is still to land after it
    on_block: Callable[[int], Awaitable[bool]]
    # its own clock started: batches and blocks from here on are the relay's to drive
    start: Callable[[], Awaitable[None]]
    # each request the relay has made to its node: the methods it called, a batch's several
    node: list[list[str]]
    # a treasury run, now
    treasury: Callable[[], Awaitable[None]]
    # what its node and builder get wrong, from here on
    faults: Faults
    # a treasury run expected to fail: the error object it fails with
    treasury_refused: Callable[[], Awaitable[dict[str, object]]]
    # its own clock, as it stands
    clock: Callable[[], Awaitable[ClockState]]
    # the chain's pace, for a test on the relay's own clock
    pace: Pace

    @asynccontextmanager
    async def interface(self) -> AsyncGenerator[httpx.AsyncClient]:
        yield self.http

    async def head(self) -> int:
        return int(await self.chain.rpc("eth_blockNumber", []), 16)

    async def send(self) -> None:
        """The batch closed and its submissions bundled for the next block. A batch funding
        its accounts sends them a block later, once the funding's block is built; a funding
        that doesn't land is given FUNDING_BLOCKS."""
        await self.close_batch()
        funding = await self.on_block(await self.head())
        for _ in range(FUNDING_BLOCKS):
            if not funding:
                return
            await self.builder.build_block()
            funding = await self.on_block(await self.head())

    async def land(self) -> BuiltBlock:
        """The batch sent, the block built, and reconciled: the block."""
        await self.send()
        block = await self.builder.build_block()
        await self.on_block(await self.head())
        return block


def relay_config(setup: Setup, rpc: str, payee: ChecksumAddress, builder_url: str) -> ChainConfig:
    """USDC accepted, priced by a v3 pool, and over x402 too, and USDT, by a v3 pool to USDC
    and the v2 pair on to WETH; and ETH (unless the setup leaves it out) and WETH; at a 10%
    margin. Our fee at least 1 gwei a
    gas, and a cent of USDC. A treasury, through Uniswap's router."""
    second = Node(SECOND_NODE_URL, without=frozenset(setup.second_without))
    nodes = (
        (rpc, PAGE_NODE_URL) if setup.page_node else (rpc, second) if setup.second_node else (rpc,)
    )
    return ChainConfig(
        rpc=(PAGE_NODE_URL,) if setup.extra_node else nodes,
        rpc_extra=(rpc,) if setup.extra_node else (),
        payee=payee,
        weth=WETH.address,
        bundlers=(BuilderConfig(builder_url, setup.mode),),
        tokens=(
            Token(
                USDC.address,
                (Hop(PoolKind.UNISWAP_V3, UNISWAP_V3_USDC_WETH),),
                eip3009=USDC_DOMAIN,
            ),
            Token(
                USDT.address,
                (
                    Hop(PoolKind.UNISWAP_V3, UNISWAP_V3_USDC_USDT),
                    Hop(PoolKind.UNISWAP_V2, UNISWAP_V2_USDC_WETH),
                ),
            ),
            *((Token(NATIVE_ASSET),) if setup.eth else ()),
            Token(WETH.address),
        ),
        accounts=setup.accounts,
        max_pending=setup.max_pending,
        max_deadline_blocks=MAX_DEADLINE_BLOCKS,
        funding_multiplier=setup.funding_multiplier,
        batch_seconds=setup.batch_seconds,
        min_step_seconds=setup.min_step_seconds,
        slot_seconds=setup.slot_seconds,
        retention_seconds=setup.retention_seconds,
        max_authorizations=setup.max_authorizations,
        measure_seconds=setup.measure_seconds,
        max_gas=MAX_GAS,
        max_call_bytes=10_000,
        margin_multiplier="1.1",
        min_fee_per_gas=10**9,
        min_fee=MinFee(USDC.address, "0.01"),
        treasury=TreasuryConfig(SWAP_ROUTER),
    )


@pytest.fixture(params=["python", "worker"])
def backend(request: pytest.FixtureRequest) -> str:
    """Which relay a test runs against: the Python relay, or the Worker."""
    return str(request.param)


@pytest.fixture
async def served(
    chain: LocalChain, schedule: GasSchedule, backend: str, request: pytest.FixtureRequest
) -> AsyncIterator[Served]:
    """The relay, its float funded, its one builder the mock builder, set up as the test asks
    (Setup's defaults, unless it asks)."""
    setup: Setup = getattr(request, "param", Setup())
    await chain.set_eth_balance(TEST_FLOAT.address, FLOAT_ETH)
    builder = MockBuilder(chain, slot_seconds=setup.slot_seconds)
    payee = to_checksum_address(await chain.account(PAYEE_ACCOUNT))
    chain_id = await chain.chain_id()
    node: list[list[str]] = []
    faults = Faults()
    listening = [await serve_json(answering(builder, faults), "127.0.0.1", BUILDER_PORT)]
    try:
        if backend == "python":
            config = relay_config(setup, chain.url, payee, BUILDER_URL)
            async with python_relay(chain, schedule, config, node, faults) as relay:
                treasury = Treasury(relay, PAYEE_KEY, TreasuryConfig(SWAP_ROUTER))
                app = create_app({chain_id: relay}, {chain_id: treasury})
                async with (
                    uvicorn_serving(app) as url,
                    httpx.AsyncClient(base_url=url, timeout=WORKER_TIMEOUT_SECONDS) as http,
                ):

                    async def close_batch() -> None:
                        await relay.batches.close_batch(await relay.chain.latest())

                    async def on_block(_head: int) -> bool:  # the latest, as the clock reads it
                        await relay.batches.on_block(await relay.chain.latest())
                        return relay.batches.funding_in_flight

                    async def start() -> None:
                        relay.start()

                    async def run_treasury() -> None:
                        await treasury.run()

                    async def treasury_refused() -> dict[str, object]:
                        try:
                            await treasury.run()
                        except (ChainError, JsonRpcError, httpx.HTTPError) as e:
                            return msgspec.to_builtins(node_failed(e))
                        raise AssertionError("the treasury ran")

                    async def clock() -> ClockState:
                        return relay.clock()

                    try:
                        yield Served(
                            http,
                            chain,
                            chain_id,
                            payee,
                            builder,
                            close_batch,
                            on_block,
                            start,
                            node,
                            run_treasury,
                            faults,
                            treasury_refused,
                            clock,
                            Pace(builder, setup.slot_seconds, setup.retention_seconds),
                        )
                    finally:
                        await relay.stop()
            return
        proxied = recording(chain.url, node, faults)
        listening.append(await serve_json(proxied, "127.0.0.1", NODE_PORT))
        listening.append(await serve_json(faults.page_node, "127.0.0.1", PAGE_NODE_PORT))
        second = seconding(chain.url, faults)
        listening.append(await serve_json(second, "127.0.0.1", SECOND_NODE_PORT))
        url = str(request.getfixturevalue("workerd"))
        config = relay_config(setup, f"http://127.0.0.1:{NODE_PORT}/", payee, BUILDER_URL)
        async with httpx.AsyncClient(base_url=url, timeout=WORKER_TIMEOUT_SECONDS) as http:
            hooks = f"/__test/{chain_id}"
            configured = await http.post(
                f"{hooks}/configure",
                json={
                    "config": msgspec.to_builtins(config),
                    "floatKey": "0x" + TEST_FLOAT.key.hex(),
                    "payeeKey": "0x" + PAYEE_KEY.key.hex(),
                },
            )
            assert configured.status_code == 200, configured.text

            async def close_batch() -> None:
                closed = await http.post(f"{hooks}/close-batch")
                assert closed.status_code == 200, closed.text

            async def on_block(_head: int) -> bool:  # the Worker takes the latest, as its alarm
                sent = await http.post(f"{hooks}/on-block")
                assert sent.status_code == 200, sent.text
                funding: bool = sent.json()["fundingInFlight"]
                return funding

            async def start() -> None:
                started = await http.post(f"{hooks}/start")
                assert started.status_code == 200, started.text

            async def run_treasury() -> None:
                ran = await http.post(f"{hooks}/treasury")
                assert ran.status_code == 200, ran.text

            async def worker_clock() -> ClockState:
                answer = await http.get(f"{hooks}/clock")
                assert answer.status_code == 200, answer.text
                state = answer.json()
                return ClockState(state["busy"], state["waitingToForget"], state["set"])

            async def treasury_refused_() -> dict[str, object]:
                ran = await http.post(f"{hooks}/treasury")
                assert ran.status_code != 200, ran.text
                return ran.json()["error"]

            yield Served(
                http,
                chain,
                chain_id,
                payee,
                builder,
                close_batch,
                on_block,
                start,
                node,
                run_treasury,
                faults,
                treasury_refused_,
                worker_clock,
                Pace(builder, setup.slot_seconds, setup.retention_seconds),
            )
    finally:
        faults.released.set()
        for server in listening:
            server.close()
            await server.wait_closed()


def answering(builder: MockBuilder, faults: Faults) -> Callable[[bytes], Awaitable[bytes]]:
    """`builder` over HTTP: eth_sendBundle as a JSON-RPC POST, answered with its bundle hash,
    or held unanswered while `faults` says it hangs."""

    async def answer(body: bytes) -> bytes:
        request = json.loads(body)
        if faults.builder_hangs:
            await faults.released.wait()
        if request.get("method") == "eth_sendBundle":
            result = await builder.send_bundle(request["params"][0])
            reply = {"jsonrpc": "2.0", "id": request.get("id"), "result": result}
        else:
            error = {"code": -32601, "message": f"method not found: {request.get('method')}"}
            reply = {"jsonrpc": "2.0", "id": request.get("id"), "error": error}
        return json.dumps(reply).encode()

    return answer


def methods(body: bytes) -> list[str]:
    """The methods a JSON-RPC request calls: one, or a batch's."""
    request = json.loads(body)
    return [str(call["method"]) for call in (request if isinstance(request, list) else [request])]


def recording(
    url: str, node: list[list[str]], faults: Faults
) -> Callable[[bytes], Awaitable[bytes]]:
    """Each request passed to the node at `url`, its methods recorded in `node`; or answered
    with a page, as `faults` says."""

    async def forward(body: bytes) -> bytes:
        called = methods(body)
        node.append(called)
        if faults.page(called):
            return PAGE
        if (refused := faults.refusal(body)) is not None:
            return refused
        async with httpx.AsyncClient() as anvil:
            answer = (await anvil.post(url, content=body, headers=JSON)).content
        return faults.rewritten(body, answer)

    return forward


def seconding(url: str, faults: Faults) -> Callable[[bytes], Awaitable[bytes | Reply]]:
    """The second node: each request recorded in `faults.second`, and passed to the node at
    `url` unless `faults.second_reply` answers it."""

    async def forward(body: bytes) -> bytes | Reply:
        faults.second.append(methods(body))
        faults.second_at.append(time.monotonic())
        if faults.second_reply is not None:
            return faults.second_reply
        async with httpx.AsyncClient() as anvil:
            return (await anvil.post(url, content=body, headers=JSON)).content

    return forward


@asynccontextmanager
async def uvicorn_serving(app: Litestar) -> AsyncGenerator[str]:
    """`app` served by uvicorn in the test's own loop, as the server runs it, on a port the
    system picks: its base URL. (Litestar's test client runs the app in a thread and loop of
    its own, so a relay would be driven from two loops at once.)"""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    serving = asyncio.create_task(server.serve())
    while not server.started:
        if serving.done():
            serving.result()  # it failed to start: its error
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await serving


@asynccontextmanager
async def python_relay(
    chain: LocalChain,
    schedule: GasSchedule,
    config: ChainConfig,
    node: list[list[str]],
    faults: Faults,
) -> AsyncGenerator[Relay]:
    """The Python relay, its requests to the node recorded in `node` (or answered with a page,
    as `faults` says), its builder reached over HTTP, its own timeout applying."""
    over_http = httpx.AsyncHTTPTransport()
    second = seconding(chain.url, faults)

    async def handle(request: httpx.Request) -> httpx.Response:
        if str(request.url) == BUILDER_URL:
            response = await over_http.handle_async_request(request)
            await response.aread()
            return response
        if str(request.url) == PAGE_NODE_URL:
            return httpx.Response(200, content=await faults.page_node(request.content))
        if str(request.url) == SECOND_NODE_URL:
            reply = await second(request.content)
            if isinstance(reply, Reply):
                headers = dict(reply.headers)
                return httpx.Response(reply.status, content=reply.body, headers=headers)
            return httpx.Response(200, content=reply, headers=JSON)
        called = methods(request.content)
        node.append(called)
        if faults.page(called):
            return httpx.Response(200, content=PAGE, headers=JSON)
        if (refused := faults.refusal(request.content)) is not None:
            return httpx.Response(200, content=refused, headers=JSON)
        async with httpx.AsyncClient() as anvil:
            answer = await anvil.post(str(request.url), content=request.content, headers=JSON)
        content = faults.rewritten(request.content, answer.content)
        return httpx.Response(answer.status_code, content=content)

    async with over_http, httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        chain_id = await chain.chain_id()
        yield open_relay(client, chain_id, config, TEST_FLOAT)
