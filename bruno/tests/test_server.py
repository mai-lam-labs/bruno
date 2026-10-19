"""The relay behind its HTTP interface, on anvil: prices read from Uniswap pools' own code, a
client quoting, submitting over REST and JSON-RPC, and its submission bundled through the
mock builder until it lands."""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import httpx
import pytest
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress, HexStr
from eth_utils.address import to_checksum_address
from jsonschema import Draft7Validator, Draft202012Validator
from litestar import Litestar
from litestar.testing import AsyncTestClient
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT7

from bruno.bundle import Mode
from bruno.chain import Chain
from bruno.config import Bundler, ChainConfig, Hop, PoolKind, Token
from bruno.evm import NATIVE_ASSET, EncodedCall
from bruno.forks import GasSchedule
from bruno.prices import PriceError, Prices
from bruno.relay import Relay, open_relay
from bruno.runner import UNDERPAID
from bruno.server.app import create_app
from bruno.server.wire import Code
from bruno.tests.calibur import calibur_batch, fresh_client
from bruno.tests.deployments import CLIENT_GAS, transfer
from bruno.tests.localchain import (
    CALIBUR,
    TEST_FLOAT,
    UNISWAP_V2_USDC_WETH,
    UNISWAP_V3_USDC_WETH,
    UNISWAP_V3_WETH_USDT,
    USDC,
    USDT,
    WETH,
    LocalChain,
)
from bruno.tests.mockbuilder import MockBuilder
from bruno.transactions import authorization_json

PAYEE_ACCOUNT = 8
BUILDER_URL = "http://builder.test/"
JSON = {"content-type": "application/json"}
MAX_DEADLINE_BLOCKS = 50
FLOAT_ETH = 10**18
# ETH at 2,500 USD: a USDC or USDT base unit (10**-6 USD) is 4 x 10**8 wei
WEI_A_USD_UNIT = 400_000_000
# USDC/WETH's sqrtPriceX96 at that price: token0 USDC in token1 WETH, sqrt(4 x 10**8) = 20,000
USDC_WETH_SQRT_PRICE_X96 = 20_000 * 2**96
# WETH/USDT's: token0 WETH in token1 USDT, sqrt(1 / (4 x 10**8)), rounded down
WETH_USDT_SQRT_PRICE_X96 = 2**96 // 20_000


async def set_pools(chain: LocalChain) -> None:
    """The v3 pools' slot0 at WEI_A_USD_UNIT, and the v2 pair's reserves at the same:
    1,000,000 USDC against 400 ETH."""
    await chain.store(UNISWAP_V3_USDC_WETH, 0, f"0x{USDC_WETH_SQRT_PRICE_X96:064x}")
    await chain.store(UNISWAP_V3_WETH_USDT, 0, f"0x{WETH_USDT_SQRT_PRICE_X96:064x}")
    reserve0, reserve1 = 10**12, 400 * 10**18  # reserve0 and reserve1, each uint112
    await chain.store(UNISWAP_V2_USDC_WETH, 8, f"0x{(reserve1 << 112) | reserve0:064x}")


async def test_prices_from_uniswap_pools(chain: LocalChain, schedule: GasSchedule) -> None:
    """A token's price in wei along its path: v3 and v2 from token0, v3 from token1, and a
    path that doesn't end at WETH."""
    await set_pools(chain)
    usdc, usdt = USDC.address, USDT.address
    v2 = Hop(PoolKind.UNISWAP_V2, UNISWAP_V2_USDC_WETH)
    v3_usdc = Hop(PoolKind.UNISWAP_V3, UNISWAP_V3_USDC_WETH)
    v3_usdt = Hop(PoolKind.UNISWAP_V3, UNISWAP_V3_WETH_USDT)
    async with httpx.AsyncClient() as client:
        live = Chain(client, await chain.chain_id(), (chain.url,), ((0, schedule),))
        by_v3 = Prices(live, WETH.address, {usdc: (v3_usdc,), usdt: (v3_usdt,)}, 0)
        by_v2 = Prices(live, WETH.address, {usdc: (v2,)}, 0)
        round_trip = Prices(live, WETH.address, {usdc: (v3_usdc, v2)}, 0)

        assert await by_v3.wei(usdc) == WEI_A_USD_UNIT
        assert await by_v2.wei(usdc) == WEI_A_USD_UNIT
        assert await by_v3.wei(usdt) == Fraction(2**192, WETH_USDT_SQRT_PRICE_X96**2)
        with pytest.raises(PriceError):
            await round_trip.wei(usdc)


@dataclass(frozen=True, slots=True)
class Served:
    """The relay on the local chain, the mock builder it sends to, and its payee."""

    relay: Relay
    builder: MockBuilder
    payee: ChecksumAddress


@dataclass(frozen=True, slots=True)
class Setup:
    """What a test asks of the relay: its builder's mode, its batch and queue sizes, and its
    funding's headroom."""

    mode: Mode = Mode.DROPPING
    accounts: int = 2
    max_pending: int = 4
    funding_multiplier: Decimal = Decimal(1)


@pytest.fixture
async def served(
    chain: LocalChain, schedule: GasSchedule, request: pytest.FixtureRequest
) -> AsyncIterator[Served]:
    """The relay, its float funded, its one builder the mock builder, set up as the test asks
    (Setup's defaults, unless it asks): USDC and USDT accepted, priced by the v3 pools, at a
    10% margin."""
    setup: Setup = getattr(request, "param", Setup())
    await set_pools(chain)
    await chain.set_eth_balance(TEST_FLOAT.address, FLOAT_ETH)
    builder = MockBuilder(chain)

    async def handle(request: httpx.Request) -> httpx.Response:
        """The builder's URL to the mock builder; everything else to anvil."""
        if str(request.url) == BUILDER_URL:
            body = json.loads(request.content)
            result = await builder.send_bundle(body["params"][0])
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})
        async with httpx.AsyncClient() as anvil:
            answer = await anvil.post(str(request.url), content=request.content, headers=JSON)
            return httpx.Response(answer.status_code, content=answer.content)

    payee = to_checksum_address(await chain.account(PAYEE_ACCOUNT))
    config = ChainConfig(
        rpc=(chain.url,),
        payee=payee,
        weth=WETH.address,
        bundlers=(Bundler(BUILDER_URL, setup.mode),),
        tokens=(
            Token(USDC.address, (Hop(PoolKind.UNISWAP_V3, UNISWAP_V3_USDC_WETH),)),
            Token(USDT.address, (Hop(PoolKind.UNISWAP_V3, UNISWAP_V3_WETH_USDT),)),
            Token(NATIVE_ASSET),
        ),
        accounts=setup.accounts,
        max_pending=setup.max_pending,
        max_deadline_blocks=MAX_DEADLINE_BLOCKS,
        funding_multiplier=setup.funding_multiplier,
        priority_fee=10**9,
        max_gas=CLIENT_GAS,
        max_call_bytes=10_000,
        margin_multiplier=Decimal("1.1"),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        chain_id = await chain.chain_id()
        relay = await open_relay(client, chain_id, config, TEST_FLOAT, ((0, schedule),))
        yield Served(relay, builder, payee)


async def signed_call(
    chain: LocalChain, client: LocalAccount, payee: str, paid: int
) -> tuple[dict[str, str], dict[str, str]]:
    """The client's call, paying `paid` USDC to `payee`, and its authorization to Calibur, as
    JSON."""
    chain_id = await chain.chain_id()
    authorization = Account.sign_authorization(
        {"chainId": chain_id, "address": CALIBUR, "nonce": 0}, client.key
    )
    call = calibur_batch(client, chain_id, [transfer(USDC, payee, paid)])
    return {"to": call.to, "data": call.data}, authorization_json(authorization)


async def quoted(http: AsyncTestClient[Litestar], chain_id: int, authority: str) -> int:
    """The USDC amount quoted for the client's call."""
    params = {"gas": hex(CLIENT_GAS), "callBytes": hex(2_000), "tokens": [USDC.address]}
    response = await http.get(f"/{chain_id}/v1/quote", params={**params, "authority": authority})
    assert response.status_code == 200, response.text
    return int(response.json()["quotes"][0]["amount"], 16)


def submission(
    call: dict[str, str], auth: dict[str, str], payee: str, amount: int, deadline: int
) -> dict[str, object]:
    return {
        "call": call,
        "authorizationList": [auth],
        "gas": hex(CLIENT_GAS),
        "payment": {"token": USDC.address, "payee": payee, "amount": hex(amount)},
        "maxBlockNumber": hex(deadline),
    }


@pytest.mark.parametrize(
    "served", [Setup(Mode.DROPPING), Setup(Mode.STANDALONE)], indirect=True, ids=str
)
async def test_quote_submit_and_land_over_rest(chain: LocalChain, served: Served) -> None:
    """A fresh client holding only USDC pays the quoted amount: its submission is taken once,
    its batch closed (its concurrency account funded from the float), bundled for the next
    block, lands, and the payee gains the amount."""
    relay, payee = served.relay, served.payee
    chain_id = relay.chain.chain_id
    client = fresh_client("a REST client")
    async with AsyncTestClient(app=create_app({chain_id: relay})) as http:
        terms = (await http.get(f"/{chain_id}/v1/config")).json()
        assert terms["payee"] == payee
        amount = await quoted(http, chain_id, client.address)
        await chain.mint(USDC, client.address, amount)
        call, auth = await signed_call(chain, client, payee, amount)
        head = await relay.chain.head()
        body = submission(call, auth, payee, amount, head + 10)
        before = await chain.balance_of(USDC, payee)

        taken = await http.post(f"/{chain_id}/v1/submissions", json=body)
        again = await http.post(f"/{chain_id}/v1/submissions", json=body)
        await relay.close_batch()
        await relay.on_block(await relay.chain.head())
        block = await served.builder.build_block()
        await relay.on_block(await relay.chain.head())
        landed = await http.get(taken.headers["Location"])

    assert taken.status_code == 202
    assert again.status_code == 200
    assert again.json()["id"] == taken.json()["id"]
    assert landed.json()["status"] == "included"
    assert bytes.fromhex(landed.json()["transactionHash"][2:]) in block.transactions
    assert await chain.balance_of(USDC, payee) - before == amount


async def rpc(
    http: AsyncTestClient[Litestar], chain_id: int, method: str, params: list[object]
) -> dict[str, Any]:
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    response = await http.post(f"/{chain_id}", json=body)
    assert response.status_code == 200
    return response.json()


async def test_refusals_over_json_rpc(chain: LocalChain, served: Served) -> None:
    """Over JSON-RPC: an amount under the price is underpaid, with the price; a call that
    pays less than it says, or can't pay, fails simulation with the runner's revert data."""
    relay, payee = served.relay, served.payee
    chain_id = relay.chain.chain_id
    client = fresh_client("a JSON-RPC client")
    async with AsyncTestClient(app=create_app({chain_id: relay})) as http:
        amount = await quoted(http, chain_id, client.address)
        deadline = await relay.chain.head() + 10
        await chain.mint(USDC, client.address, amount)
        call, auth = await signed_call(chain, client, payee, amount // 2)
        half = await rpc(
            http,
            chain_id,
            "postpaid_sendTransaction",
            [submission(call, auth, payee, amount // 2, deadline)],
        )
        short = await rpc(
            http,
            chain_id,
            "postpaid_sendTransaction",
            [submission(call, auth, payee, amount, deadline)],
        )
        broke = fresh_client("a client holding nothing")
        call, auth = await signed_call(chain, broke, payee, amount)
        unpaid = await rpc(
            http,
            chain_id,
            "postpaid_sendTransaction",
            [submission(call, auth, payee, amount, deadline)],
        )

    assert half["error"]["code"] == Code.UNDERPAID
    assert int(half["error"]["data"]["required"], 16) <= amount
    assert short["error"]["code"] == Code.SIMULATION_FAILED
    assert bytes.fromhex(short["error"]["data"]["revertData"][2:]) == UNDERPAID
    assert unpaid["error"]["code"] == Code.SIMULATION_FAILED
    assert unpaid["error"]["data"]["revertData"] != "0x"


# OpenRPC's meta-schema, as released: github.com/open-rpc/meta-schema/releases/tag/1.14.9,
# open-rpc-meta-schema.json (sha256 e5f534fb...71d0f82), the one that admits "1.3.2"; and the
# JSON Schema meta-schema it refers to, at https://meta.json-schema.tools/:
# github.com/json-schema-tools/meta-schema/releases/tag/1.8.3, schema.json (sha256
# 88ca4358...c4a780c8). Both vendored, so validating fetches nothing
FIXTURES = Path(__file__).parent / "fixtures"
OPENRPC_META_SCHEMA = FIXTURES / "open-rpc-meta-schema-1.14.9.json"
JSON_SCHEMA_TOOLS_META_SCHEMA = FIXTURES / "json-schema-tools-meta-schema-1.8.3.json"
JSON_SCHEMA_TOOLS = "https://meta.json-schema.tools"


def openrpc_validator() -> Draft7Validator:
    """OpenRPC's meta-schema, its one remote reference resolved from the fixture."""
    tools = Resource.from_contents(
        json.loads(JSON_SCHEMA_TOOLS_META_SCHEMA.read_text()), default_specification=DRAFT7
    )
    registry = Registry().with_resources(
        [(JSON_SCHEMA_TOOLS, tools), (JSON_SCHEMA_TOOLS + "/", tools)]
    )
    return Draft7Validator(json.loads(OPENRPC_META_SCHEMA.read_text()), registry=registry)


def method_param(document: dict[str, Any], name: str) -> dict[str, Any]:
    """The parameter schema of method `name`, with the components its references name."""
    method = next(m for m in document["methods"] if m["name"] == name)
    return {**method["params"][0]["schema"], "components": document["components"]}


async def test_openrpc_document(chain: LocalChain, served: Served) -> None:
    """The OpenRPC document is valid against OpenRPC's meta-schema, the same by
    rpc.discover, and its parameter schemas take a real submission and refuse one whose
    quantity isn't one."""
    relay, payee = served.relay, served.payee
    chain_id = relay.chain.chain_id
    client = fresh_client("an OpenRPC client")
    async with AsyncTestClient(app=create_app({chain_id: relay})) as http:
        document = (await http.get("/openrpc.json")).json()
        discovered = await rpc(http, chain_id, "rpc.discover", [])
    call, auth = await signed_call(chain, client, payee, 1)
    body = submission(call, auth, payee, 1, 1)
    send = Draft202012Validator(method_param(document, "postpaid_sendTransaction"))

    openrpc_validator().validate(document)
    assert discovered["result"] == document
    assert {m["name"] for m in document["methods"]} == {
        "postpaid_config",
        "postpaid_quote",
        "postpaid_sendTransaction",
        "postpaid_getStatus",
    }
    send.validate(body)
    assert not send.is_valid({**body, "gas": "0x01"})


async def land(served: Served) -> None:
    """A batch made, bundled for the next block, the block built, and settled."""
    relay = served.relay
    await relay.close_batch()
    await relay.on_block(await relay.chain.head())
    await served.builder.build_block()
    await relay.on_block(await relay.chain.head())


async def submit_paying(
    http: AsyncTestClient[Litestar], chain: LocalChain, served: Served, name: str, amount: int
) -> httpx.Response:
    """A fresh client, holding `amount` USDC, submitting a call that pays it all."""
    client = fresh_client(name)
    await chain.mint(USDC, client.address, amount)
    call, auth = await signed_call(chain, client, served.payee, amount)
    deadline = await served.relay.chain.head() + MAX_DEADLINE_BLOCKS
    body = submission(call, auth, served.payee, amount, deadline)
    return await http.post(f"/{served.relay.chain.chain_id}/v1/submissions", json=body)


@pytest.mark.parametrize("served", [Setup(accounts=1, max_pending=2)], indirect=True, ids=str)
async def test_queue_ranks_by_profit(chain: LocalChain, served: Served) -> None:
    """With room for two, a third more profitable submission evicts the least profitable,
    and a fourth less profitable than both is refused with what would get it in. A batch of
    one account takes the most profitable."""
    relay = served.relay
    chain_id = relay.chain.chain_id
    async with AsyncTestClient(app=create_app({chain_id: relay})) as http:
        price = await quoted(http, chain_id, fresh_client("anyone").address)
        low = await submit_paying(http, chain, served, "pays the price", price)
        mid = await submit_paying(http, chain, served, "pays twice", 2 * price)
        high = await submit_paying(http, chain, served, "pays three times", 3 * price)
        refused = await submit_paying(http, chain, served, "pays it again", price + 1)
        await land(served)
        statuses = {
            name: (await http.get(r.headers["Location"])).json()
            for name, r in (("low", low), ("mid", mid), ("high", high))
        }

    assert statuses["low"]["status"] == "rejected"
    assert statuses["low"]["reason"] == "outbid"
    assert refused.status_code == 503
    assert int(refused.json()["error"]["data"]["required"], 16) > price + 1
    assert statuses["high"]["status"] == "included"
    assert statuses["mid"]["status"] == "pending"


async def test_a_submission_that_stops_simulating_is_rejected(
    chain: LocalChain, served: Served
) -> None:
    """A client that spends its tokens after submitting: the batch's simulation rejects its
    submission, with the token's revert data, and nothing is sent for it."""
    relay = served.relay
    chain_id = relay.chain.chain_id
    async with AsyncTestClient(app=create_app({chain_id: relay})) as http:
        price = await quoted(http, chain_id, fresh_client("anyone").address)
        taken = await submit_paying(http, chain, served, "spends it elsewhere", price)
        client = fresh_client("spends it elsewhere")
        await chain.mint(USDC, client.address, -price)  # its balance written back to 0
        await land(served)
        status = (await http.get(taken.headers["Location"])).json()

    assert status["status"] == "rejected"
    assert status["reason"] == "simulation failed"
    assert status["revertData"] != "0x"
    assert status["attempts"] == "0x0"


@pytest.mark.parametrize(
    "served", [Setup(funding_multiplier=Decimal("2.5"))], indirect=True, ids=str
)
async def test_headroom_saves_a_funding_round(chain: LocalChain, served: Served) -> None:
    """Funded with headroom, an account covers the next batch's submission too: that batch
    sends no funding transaction (the float's nonce doesn't move), and it lands."""
    relay = served.relay
    chain_id = relay.chain.chain_id
    async with AsyncTestClient(app=create_app({chain_id: relay})) as http:
        price = await quoted(http, chain_id, fresh_client("anyone").address)
        first = await submit_paying(http, chain, served, "the first", price)
        await land(served)
        float_nonce = await chain.nonce(TEST_FLOAT.address)
        second = await submit_paying(http, chain, served, "the second", price)
        await land(served)
        statuses = [(await http.get(r.headers["Location"])).json() for r in (first, second)]

    assert [s["status"] for s in statuses] == ["included", "included"]
    assert await chain.nonce(TEST_FLOAT.address) == float_nonce


async def test_deadline_too_far_ahead_is_refused(chain: LocalChain, served: Served) -> None:
    relay, payee = served.relay, served.payee
    chain_id = relay.chain.chain_id
    client = fresh_client("plans ahead")
    call, auth = await signed_call(chain, client, payee, 1)
    deadline = await relay.chain.head() + MAX_DEADLINE_BLOCKS + 1
    async with AsyncTestClient(app=create_app({chain_id: relay})) as http:
        refused = await http.post(
            f"/{chain_id}/v1/submissions", json=submission(call, auth, payee, 10**12, deadline)
        )

    assert refused.status_code == 400
    assert refused.json()["error"]["data"] == "maxBlockNumber"


async def test_paying_in_eth_the_call_releases(chain: LocalChain, served: Served) -> None:
    """A fresh client holding only WETH: its call unwraps it and pays the payee the ETH,
    quoted and checked in wei. The client ends with no ETH, never having held any."""
    relay, payee = served.relay, served.payee
    chain_id = relay.chain.chain_id
    client = fresh_client("holds only WETH")
    async with AsyncTestClient(app=create_app({chain_id: relay})) as http:
        params = {"gas": hex(CLIENT_GAS), "callBytes": hex(2_000), "tokens": [NATIVE_ASSET]}
        quote = await http.get(
            f"/{chain_id}/v1/quote", params={**params, "authority": client.address}
        )
        amount = int(quote.json()["quotes"][0]["amount"], 16)
        await chain.mint(WETH, client.address, amount)
        authorization = Account.sign_authorization(
            {"chainId": chain_id, "address": CALIBUR, "nonce": 0}, client.key
        )
        unwrap = EncodedCall(
            WETH.address, LocalChain.calldata("withdraw(uint256)", ["uint256"], [amount])
        )
        pay = EncodedCall(payee, HexStr("0x"), amount)
        call = calibur_batch(client, chain_id, [unwrap, pay])
        body = submission(
            {"to": call.to, "data": call.data},
            authorization_json(authorization),
            payee,
            amount,
            await relay.chain.head() + MAX_DEADLINE_BLOCKS,
        )
        body["payment"] = {"token": NATIVE_ASSET, "payee": payee, "amount": hex(amount)}
        before = await chain.eth_balance(payee)
        taken = await http.post(f"/{chain_id}/v1/submissions", json=body)
        await land(served)
        status = (await http.get(taken.headers["Location"])).json()

    assert taken.status_code == 202, taken.text
    assert status["status"] == "included"
    assert await chain.eth_balance(payee) - before == amount
    assert await chain.eth_balance(client.address) == 0
