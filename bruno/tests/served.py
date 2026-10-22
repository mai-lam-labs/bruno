"""The relay under test, whichever runs it: the Python reference in-process, or the Worker
under workerd. Its fixtures (`backend`, `served`) are the tests' through conftest.py, so a test
asking for `served` runs against both."""

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal

import httpx
import msgspec
import pytest
import uvicorn
from eth_account import Account
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address
from litestar import Litestar

from bruno.bundle import Mode
from bruno.config import (
    Bundler,
    ChainConfig,
    Eip712Domain,
    Hop,
    MinFee,
    PoolKind,
    Token,
    TreasuryConfig,
)
from bruno.evm import NATIVE_ASSET
from bruno.forks import GasSchedule
from bruno.relay import Relay, open_relay
from bruno.server.app import create_app
from bruno.tests.deployments import CLIENT_GAS
from bruno.tests.localchain import (
    SWAP_ROUTER,
    TEST_FLOAT,
    UNISWAP_V2_USDC_WETH,
    UNISWAP_V3_USDC_USDT,
    UNISWAP_V3_USDC_WETH,
    USDC,
    USDT,
    WETH,
    LocalChain,
)
from bruno.tests.mockbuilder import MockBuilder, serve, serve_json
from bruno.treasury import Treasury

PAYEE_ACCOUNT = 8
# its key, anvil's: the treasury's
PAYEE_KEY = Account.from_key("0xdbda1821b80551c9d65939329250298aa3472ba22feea921c0cf5d620ea67b97")
BUILDER_URL = "http://builder.test/"
JSON = {"content-type": "application/json"}
MAX_DEADLINE_BLOCKS = 50
# how long an x402 request is held at most, in blocks: the configuration's default
X402_DEADLINE_BLOCKS = 10
# the mock builder's port when served over HTTP, for the Worker (beside anvil's and workerd's),
# and the port of the Worker's way to anvil, which records its requests
BUILDER_PORT = 8548
NODE_PORT = 8549
# what one request to the Worker may take: a block's work, simulations and bundles included
WORKER_TIMEOUT_SECONDS = 60
# a fast chain, for the relay's own clock: a block every 2 seconds, a batch every second; a
# submission is waited for this many slots
FAST_SLOT_SECONDS = 2
FAST_BATCH_SECONDS = 1.0
FAST_SLOTS = 15
FLOAT_ETH = 10**18
# the local USDC's EIP-712 domain, as FiatTokenV2_2 is initialised (localchain)
USDC_DOMAIN = Eip712Domain("USD Coin", "2")


@dataclass(frozen=True, slots=True)
class Setup:
    """What a test asks of the relay: its builder's mode, its batch and queue sizes, and its
    funding's headroom and patience."""

    mode: Mode = Mode.DROPPING
    accounts: int = 2
    max_pending: int = 4
    funding_multiplier: Decimal = Decimal(1)
    batch_seconds: float = 12.0
    slot_seconds: int = 12
    retention_seconds: float = 3600.0
    max_authorizations: int = 1
    funding_timeout_seconds: float = 120.0
    measure_seconds: float = 3600.0


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
    on_block: Callable[[int], Awaitable[None]]
    # its own clock started: batches and blocks from here on are the relay's to drive
    start: Callable[[], Awaitable[None]]
    # each request the relay has made to its node: the methods it called, a batch's several
    node: list[list[str]]
    # a treasury run, now
    treasury: Callable[[], Awaitable[None]]

    @asynccontextmanager
    async def interface(self) -> AsyncGenerator[httpx.AsyncClient]:
        yield self.http

    async def head(self) -> int:
        return int(await self.chain.rpc("eth_blockNumber", []), 16)


def relay_config(setup: Setup, rpc: str, payee: ChecksumAddress, builder_url: str) -> ChainConfig:
    """USDC accepted, priced by a v3 pool, and over x402 too, and USDT, by a v3 pool to USDC
    and the v2 pair on to WETH; and ETH and WETH; at a 10% margin. Our fee at least 1 gwei a
    gas, and a cent of USDC. A treasury, through Uniswap's router."""
    return ChainConfig(
        rpc=(rpc,),
        payee=payee,
        weth=WETH.address,
        bundlers=(Bundler(builder_url, setup.mode),),
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
            Token(NATIVE_ASSET),
            Token(WETH.address),
        ),
        accounts=setup.accounts,
        max_pending=setup.max_pending,
        max_deadline_blocks=MAX_DEADLINE_BLOCKS,
        funding_multiplier=setup.funding_multiplier,
        funding_timeout_seconds=setup.funding_timeout_seconds,
        batch_seconds=setup.batch_seconds,
        slot_seconds=setup.slot_seconds,
        retention_seconds=setup.retention_seconds,
        max_authorizations=setup.max_authorizations,
        measure_seconds=setup.measure_seconds,
        max_gas=CLIENT_GAS,
        max_call_bytes=10_000,
        margin_multiplier=Decimal("1.1"),
        min_fee_per_gas=10**9,
        min_fee=MinFee(USDC.address, Decimal("0.01")),
        treasury=TreasuryConfig(SWAP_ROUTER),
    )


@pytest.fixture(params=["python", "worker"])
def backend(request: pytest.FixtureRequest) -> str:
    """Which relay a test runs against: the Python reference, or the Worker port."""
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
    if backend == "python":
        config = relay_config(setup, chain.url, payee, BUILDER_URL)
        async with python_relay(chain, schedule, builder, config, node) as relay:
            treasury = Treasury(relay, PAYEE_KEY, TreasuryConfig(SWAP_ROUTER))
            app = create_app({chain_id: relay}, {chain_id: treasury})
            async with (
                uvicorn_serving(app) as url,
                httpx.AsyncClient(base_url=url, timeout=WORKER_TIMEOUT_SECONDS) as http,
            ):

                async def close_batch() -> None:
                    await relay.close_batch(await relay.chain.latest())

                async def on_block(_head: int) -> None:  # the latest block, as the clock reads it
                    await relay.on_block(await relay.chain.latest())

                async def start() -> None:
                    relay.start()

                async def run_treasury() -> None:
                    await treasury.run()

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
                    )
                finally:
                    await relay.stop()
        return
    server = await serve(builder, "127.0.0.1", BUILDER_PORT)
    proxy = await serve_json(recording(chain.url, node), "127.0.0.1", NODE_PORT)
    try:
        url = str(request.getfixturevalue("workerd"))
        config = relay_config(
            setup, f"http://127.0.0.1:{NODE_PORT}/", payee, f"http://127.0.0.1:{BUILDER_PORT}/"
        )
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

            async def on_block(_head: int) -> None:  # the Worker takes the latest, as its alarm
                sent = await http.post(f"{hooks}/on-block")
                assert sent.status_code == 200, sent.text

            async def start() -> None:
                started = await http.post(f"{hooks}/start")
                assert started.status_code == 200, started.text

            async def run_treasury() -> None:
                ran = await http.post(f"{hooks}/treasury")
                assert ran.status_code == 200, ran.text

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
            )
    finally:
        for listening in (server, proxy):
            listening.close()
            await listening.wait_closed()


def methods(body: bytes) -> list[str]:
    """The methods a JSON-RPC request calls: one, or a batch's."""
    request = json.loads(body)
    return [str(call["method"]) for call in (request if isinstance(request, list) else [request])]


def recording(url: str, node: list[list[str]]) -> Callable[[bytes], Awaitable[bytes]]:
    """Each request passed to the node at `url`, its methods recorded in `node`."""

    async def forward(body: bytes) -> bytes:
        node.append(methods(body))
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
    builder: MockBuilder,
    config: ChainConfig,
    node: list[list[str]],
) -> AsyncGenerator[Relay]:
    """The Python relay, its builder's URL routed to the mock builder in-process, its requests to
    the node recorded in `node`."""

    async def handle(request: httpx.Request) -> httpx.Response:
        if str(request.url) == BUILDER_URL:
            body = json.loads(request.content)
            result = await builder.send_bundle(body["params"][0])
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})
        node.append(methods(request.content))
        async with httpx.AsyncClient() as anvil:
            answer = await anvil.post(str(request.url), content=request.content, headers=JSON)
            return httpx.Response(answer.status_code, content=answer.content)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        chain_id = await chain.chain_id()
        yield open_relay(client, chain_id, config, TEST_FLOAT)
