"""The relay behind its HTTP interface, on anvil: prices read from Uniswap pools' own code, a
client quoting, submitting over REST and JSON-RPC, and its submission bundled through the
mock builder until it lands."""

import asyncio
import json
from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
from math import ceil
from pathlib import Path
from typing import Any, override

import httpx
import msgspec
import pytest
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_typing import HexStr
from eth_utils.address import to_checksum_address
from jsonschema import Draft7Validator, Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT7

from bruno.accounts import derive
from bruno.bundle import Mode
from bruno.chain import Chain, lasting_fees, market_tip
from bruno.config import Bundler, Hop, PoolKind
from bruno.evm import NATIVE_ASSET, EncodedCall
from bruno.forks import GasSchedule
from bruno.net.jsonrpc import Answer, Call
from bruno.prices import PriceError, Prices
from bruno.relay import fee_ceiling
from bruno.router import Step, swap_calldata
from bruno.runner import UNDERPAID
from bruno.server.app import MAX_BODY_BYTES
from bruno.server.front import page, qr, svg
from bruno.server.wire import Code, IndexResult
from bruno.tests.calibur import calibur_batch, fresh_client
from bruno.tests.deployments import CLIENT_GAS, transfer
from bruno.tests.localchain import (
    CALIBUR,
    SWAP_ROUTER,
    TEST_FLOAT,
    UNISWAP_V2_USDC_WETH,
    UNISWAP_V3_USDC_WETH,
    UNISWAP_V3_WETH_USDT,
    USDC,
    USDT,
    WEI_A_USD_UNIT,
    WETH,
    WETH_USDT_SQRT_PRICE_X96,
    LocalChain,
)
from bruno.tests.served import (
    BUILDER_URL,
    FAST_BATCH_SECONDS,
    FAST_SLOT_SECONDS,
    FAST_SLOTS,
    JSON,
    MAX_DEADLINE_BLOCKS,
    PAYEE_ACCOUNT,
    Served,
    Setup,
    relay_config,
)
from bruno.transactions import authorization_json
from bruno.treasury import BUNDLE_BLOCKS, GAS_HEADROOM

# what a payee holds of a stablecoin in the treasury's tests: 200 USD
STABLES = 200 * 10**6


async def test_prices_from_uniswap_pools(chain: LocalChain, schedule: GasSchedule) -> None:
    """A token's price in wei along its path: v3 and v2 from token0, v3 from token1, and a
    path that doesn't end at WETH."""
    usdc, usdt = USDC.address, USDT.address
    v2 = Hop(PoolKind.UNISWAP_V2, UNISWAP_V2_USDC_WETH)
    v3_usdc = Hop(PoolKind.UNISWAP_V3, UNISWAP_V3_USDC_WETH)
    v3_usdt = Hop(PoolKind.UNISWAP_V3, UNISWAP_V3_WETH_USDT)
    async with httpx.AsyncClient() as client:
        live = Chain(client, await chain.chain_id(), (chain.url,))
        by_v3 = Prices(live, WETH.address, {usdc: (v3_usdc,), usdt: (v3_usdt,)}, 0)
        by_v2 = Prices(live, WETH.address, {usdc: (v2,)}, 0)
        round_trip = Prices(live, WETH.address, {usdc: (v3_usdc, v2)}, 0)

        assert await by_v3.wei(usdc) == WEI_A_USD_UNIT
        assert await by_v2.wei(usdc) == WEI_A_USD_UNIT
        assert await by_v3.wei(usdt) == Fraction(2**192, WETH_USDT_SQRT_PRICE_X96**2)
        with pytest.raises(PriceError):
            await round_trip.wei(usdc)


class CountingChain(Chain):
    """The chain, counting the reads made of it: what a price costs the nodes."""

    reads = 0

    @override
    async def batch(self, calls: Sequence[Call]) -> list[Answer]:
        self.reads += 1
        return await super().batch(calls)


async def test_concurrent_price_requests_share_one_read(
    chain: LocalChain, schedule: GasSchedule
) -> None:
    """Twenty requests for a token's price at once read its pool once (token0, token1, slot0,
    in one batch), however short the cache; and a price is read again once its time is up."""
    hop = Hop(PoolKind.UNISWAP_V3, UNISWAP_V3_USDC_WETH)
    async with httpx.AsyncClient() as client:
        live = CountingChain(client, await chain.chain_id(), (chain.url,))
        prices = Prices(live, WETH.address, {USDC.address: (hop,)}, 0)
        burst = await asyncio.gather(*(prices.wei(USDC.address) for _ in range(20)))
        after_burst = live.reads
        await prices.wei(USDC.address)

    assert set(burst) == {WEI_A_USD_UNIT}
    assert after_burst == 1
    assert live.reads == 2


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


async def quoted(http: httpx.AsyncClient, chain_id: int, authority: str) -> int:
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
    payee = served.payee
    chain_id = served.chain_id
    client = fresh_client("a REST client")
    async with served.interface() as http:
        terms = (await http.get(f"/{chain_id}/v1/config")).json()
        assert terms["payee"] == payee
        amount = await quoted(http, chain_id, client.address)
        await chain.mint(USDC, client.address, amount)
        call, auth = await signed_call(chain, client, payee, amount)
        head = await served.head()
        body = submission(call, auth, payee, amount, head + 10)
        before = await chain.balance_of(USDC, payee)

        taken = await http.post(f"/{chain_id}/v1/submissions", json=body)
        again = await http.post(f"/{chain_id}/v1/submissions", json=body)
        await served.close_batch()
        await served.on_block(await served.head())
        block = await served.builder.build_block()
        await served.on_block(await served.head())
        landed = await http.get(taken.headers["Location"])

    assert taken.status_code == 202
    assert again.status_code == 200
    assert again.json()["id"] == taken.json()["id"]
    assert landed.json()["status"] == "included"
    assert bytes.fromhex(landed.json()["transactionHash"][2:]) in block.transactions
    assert await chain.balance_of(USDC, payee) - before == amount


async def rpc(
    http: httpx.AsyncClient, chain_id: int, method: str, params: list[object]
) -> dict[str, Any]:
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    response = await http.post(f"/{chain_id}", json=body)
    assert response.status_code == 200
    return response.json()


async def test_refusals_over_json_rpc(chain: LocalChain, served: Served) -> None:
    """Over JSON-RPC: an amount under the price is underpaid, with the price; a call that
    pays less than it says, or can't pay, fails simulation with the runner's revert data."""
    payee = served.payee
    chain_id = served.chain_id
    client = fresh_client("a JSON-RPC client")
    async with served.interface() as http:
        amount = await quoted(http, chain_id, client.address)
        deadline = await served.head() + 10
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
    """The OpenRPC document is valid against OpenRPC's meta-schema; its servers are the
    chains served, and rpc.discover answers it with the one chain's; and its parameter schemas
    take a real submission and refuse one whose quantity isn't one."""
    payee = served.payee
    chain_id = served.chain_id
    client = fresh_client("an OpenRPC client")
    async with served.interface() as http:
        document = (await http.get("/openrpc.json")).json()
        discovered = await rpc(http, chain_id, "rpc.discover", [])
    call, auth = await signed_call(chain, client, payee, 1)
    body = submission(call, auth, payee, 1, 1)
    send = Draft202012Validator(method_param(document, "postpaid_sendTransaction"))

    endpoint = {"name": "anvil", "url": f"{origin(served)}/anvil"}

    openrpc_validator().validate(document)
    openrpc_validator().validate(discovered["result"])
    assert document["servers"] == [endpoint]
    assert discovered["result"] == {**document, "servers": [endpoint]}
    assert {m["name"] for m in document["methods"]} == {
        "postpaid_config",
        "postpaid_quote",
        "postpaid_sendTransaction",
        "postpaid_getStatus",
    }
    send.validate(body)
    assert not send.is_valid({**body, "gas": "0x01"})


async def test_paying_in_weth(chain: LocalChain, served: Served) -> None:
    """WETH needs no pools: it's priced one to one with ETH, and a call paying in it lands, the
    payee's WETH up by the amount."""
    async with served.interface() as http:
        client = fresh_client("pays in WETH")
        params = {"gas": hex(CLIENT_GAS), "callBytes": hex(2_000), "tokens": [WETH.address]}
        quote = await http.get(
            f"/{served.chain_id}/v1/quote", params={**params, "authority": client.address}
        )
        assert quote.status_code == 200, quote.text
        price = int(quote.json()["quotes"][0]["amount"], 16)
        await chain.mint(WETH, client.address, price)
        authorization = Account.sign_authorization(
            {"chainId": served.chain_id, "address": CALIBUR, "nonce": 0}, client.key
        )
        call = calibur_batch(client, served.chain_id, [transfer(WETH, served.payee, price)])
        body = {
            "call": {"to": call.to, "data": call.data},
            "authorizationList": [authorization_json(authorization)],
            "gas": hex(CLIENT_GAS),
            "payment": {"token": WETH.address, "payee": served.payee, "amount": hex(price)},
            "maxBlockNumber": hex(await served.head() + MAX_DEADLINE_BLOCKS),
        }
        before = await chain.balance_of(WETH, served.payee)
        taken = await http.post(f"/{served.chain_id}/v1/submissions", json=body)
        assert taken.status_code == 202, taken.text
        await land(served)
        status = (await http.get(taken.headers["Location"])).json()

    assert status["status"] == "included", status
    assert await chain.balance_of(WETH, served.payee) - before == price


def origin(served: Served) -> str:
    """Where the relay's interface is: its URLs in what it answers begin so."""
    return str(served.http.base_url).rstrip("/")


async def test_the_root_lists_the_chains_and_documents(served: Served) -> None:
    """The root says what the relay is, where its documents are and which chains it serves,
    each by its network's name, with its payee: JSON, or a page for a browser, the Python
    reference's page from the same JSON, a QR code of the payee's address among it. Any origin may
    call it."""
    async with served.interface() as http:
        root = await http.get("/")
        browsed = await http.get("/", headers={"accept": "text/html"})
        openapi = (await http.get("/openapi.json")).json()
        preflight = await http.options(
            "/anvil",
            headers={
                "origin": "https://dapp.example",
                "access-control-request-method": "POST",
                "access-control-request-headers": "content-type",
            },
        )
        config = await http.get("/anvil/v1/config", headers={"origin": "https://dapp.example"})
        nowhere = await http.get("/nowhere/v1/config")

    at = origin(served)
    assert root.status_code == 200, root.text
    assert root.json()["chains"] == [
        {
            "chainId": 31337,
            "name": "anvil",
            "jsonRpc": f"{at}/anvil",
            "rest": f"{at}/anvil/v1",
            "payee": served.payee,
            "floatAccount": TEST_FLOAT.address,
            "concurrencyAccounts": [derive(TEST_FLOAT, i).address for i in range(2)],
            "x402": f"{at}/anvil/v1/x402",
            "x402Tokens": [USDC.address],
        }
    ]
    assert "<h2>Accounts</h2>" in browsed.text
    assert f"<code>{TEST_FLOAT.address[:6]}…{TEST_FLOAT.address[-4:]}</code>" in browsed.text
    assert root.json()["openrpc"] == f"{at}/openrpc.json"
    assert root.json()["openapi"] == f"{at}/openapi.json"
    assert root.json()["documentation"] == "https://docs.mailam.app/bruno/"
    assert browsed.headers["content-type"].startswith("text/html")
    assert browsed.text == page(at, msgspec.convert(root.json(), IndexResult))
    assert svg(qr(served.payee)) in browsed.text
    assert "/{chain}/v1/quote" in openapi["paths"]
    assert openapi["servers"] == [{"url": at}]
    assert preflight.status_code in (200, 204), preflight.text
    assert preflight.headers["access-control-allow-origin"] == "*"
    assert config.headers["access-control-allow-origin"] == "*"
    assert nowhere.status_code == 404
    assert nowhere.json()["error"]["message"].endswith(": see /"), nowhere.text


async def test_a_chain_by_name_is_the_chain_by_id(chain: LocalChain, served: Served) -> None:
    """A chain's network name and its chain id reach the same chain: a submission taken by
    one is found by the other, at the path the relay answers with, by name."""
    async with served.interface() as http:
        price = await quoted(http, served.chain_id, fresh_client("anyone").address)
        client = fresh_client("by name")
        await chain.mint(USDC, client.address, price)
        call, auth = await signed_call(chain, client, served.payee, price)
        body = submission(call, auth, served.payee, price, await served.head() + 10)
        taken = await http.post("/anvil/v1/submissions", json=body)
        again = await http.post(f"/{served.chain_id}/v1/submissions", json=body)
        by_id = await http.get(taken.headers["Location"].replace("/anvil/", f"/{served.chain_id}/"))

    assert taken.status_code == 202, taken.text
    assert taken.headers["Location"].startswith("/anvil/v1/submissions/0x")
    assert again.status_code == 200, again.text
    assert again.json()["id"] == taken.json()["id"]
    assert by_id.json()["status"] == "pending", by_id.text


async def land(served: Served) -> None:
    """A batch made, bundled for the next block, the block built, and settled."""
    await served.close_batch()
    await served.on_block(await served.head())
    await served.builder.build_block()
    await served.on_block(await served.head())


async def submit_paying(
    http: httpx.AsyncClient, chain: LocalChain, served: Served, name: str, amount: int
) -> httpx.Response:
    """A fresh client, holding `amount` USDC, submitting a call that pays it all."""
    client = fresh_client(name)
    await chain.mint(USDC, client.address, amount)
    call, auth = await signed_call(chain, client, served.payee, amount)
    deadline = await served.head() + MAX_DEADLINE_BLOCKS
    body = submission(call, auth, served.payee, amount, deadline)
    return await http.post(f"/{served.chain_id}/v1/submissions", json=body)


@pytest.mark.parametrize("served", [Setup(accounts=1, max_pending=2)], indirect=True, ids=str)
async def test_queue_ranks_by_profit(chain: LocalChain, served: Served) -> None:
    """With room for two, a third more profitable submission evicts the least profitable,
    and a fourth less profitable than both is refused with what would get it in. A batch of
    one account takes the most profitable."""
    chain_id = served.chain_id
    async with served.interface() as http:
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
    chain_id = served.chain_id
    async with served.interface() as http:
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
    chain_id = served.chain_id
    async with served.interface() as http:
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
    payee = served.payee
    chain_id = served.chain_id
    client = fresh_client("plans ahead")
    call, auth = await signed_call(chain, client, payee, 1)
    deadline = await served.head() + MAX_DEADLINE_BLOCKS + 1
    async with served.interface() as http:
        refused = await http.post(
            f"/{chain_id}/v1/submissions", json=submission(call, auth, payee, 10**12, deadline)
        )

    assert refused.status_code == 400
    assert refused.json()["error"]["data"] == "maxBlockNumber"


async def test_paying_in_eth_the_call_releases(chain: LocalChain, served: Served) -> None:
    """A fresh client holding only WETH: its call unwraps it and pays the payee the ETH,
    quoted and checked in wei. The client ends with no ETH, never having held any."""
    payee = served.payee
    chain_id = served.chain_id
    client = fresh_client("holds only WETH")
    async with served.interface() as http:
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
            await served.head() + MAX_DEADLINE_BLOCKS,
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


@pytest.mark.parametrize(
    "served",
    [Setup(batch_seconds=FAST_BATCH_SECONDS, slot_seconds=FAST_SLOT_SECONDS)],
    indirect=True,
    ids=str,
)
async def test_the_relay_s_own_clock_lands_a_submission(chain: LocalChain, served: Served) -> None:
    """Nothing driven but the chain: a block built every slot, as a builder would. The relay's
    own clock (the Worker's alarms, the Python relay's loops) makes the batch, funds it, and
    bundles it for a block, which lands it."""
    async with served.interface() as http:
        price = await quoted(http, served.chain_id, fresh_client("anyone").address)
        taken = await submit_paying(http, chain, served, "left to the clock", price)
        before = await chain.balance_of(USDC, served.payee)
        await served.start()
        status: dict[str, Any] = {}
        for _ in range(FAST_SLOTS):
            await asyncio.sleep(FAST_SLOT_SECONDS)
            await served.builder.build_block()
            status = (await http.get(taken.headers["Location"])).json()
            if status["status"] != "pending":
                break

    assert status["status"] == "included", status
    assert await chain.balance_of(USDC, served.payee) - before == price


@pytest.mark.parametrize("backend", ["worker"], indirect=True)
@pytest.mark.parametrize(
    "served",
    [
        Setup(
            batch_seconds=FAST_BATCH_SECONDS,
            slot_seconds=FAST_SLOT_SECONDS,
            # kept long enough to be seen landed, polled a slot apart
            retention_seconds=3 * FAST_SLOT_SECONDS,
        )
    ],
    indirect=True,
    ids=str,
)
async def test_an_idle_worker_sets_no_alarm(chain: LocalChain, served: Served) -> None:
    """The Worker's alarms run only while it has work, so an idle object costs nothing: with
    nothing queued its alarm stops, and a quote doesn't arm it. A submission does: nothing else
    drives the relay here, so its landing is the alarms' doing. Once it's forgotten there's no
    alarm again."""
    hooks = f"/__test/{served.chain_id}"
    async with served.interface() as http:

        async def alarm() -> int | None:
            answer = await http.get(f"{hooks}/alarm")
            assert answer.status_code == 200, answer.text
            return answer.json()["alarm"]

        async def until_idle() -> None:
            for _ in range(FAST_SLOTS):
                if await alarm() is None:
                    return
                await asyncio.sleep(FAST_SLOT_SECONDS)

        await served.start()
        await until_idle()
        idle = await alarm()
        price = await quoted(http, served.chain_id, fresh_client("anyone").address)
        quoting = await alarm()
        taken = await submit_paying(http, chain, served, "wakes the relay", price)
        status: dict[str, Any] = {}
        for _ in range(FAST_SLOTS):
            await asyncio.sleep(FAST_SLOT_SECONDS)
            await served.builder.build_block()
            status = (await http.get(taken.headers["Location"])).json()
            if status["status"] != "pending":
                break
        await until_idle()
        asleep = await alarm()
        forgotten = await http.get(taken.headers["Location"])

    assert idle is None
    assert quoting is None
    assert status["status"] == "included", status
    assert asleep is None
    assert forgotten.status_code == 404, forgotten.text


@pytest.mark.parametrize(
    "served",
    [Setup(batch_seconds=FAST_BATCH_SECONDS, slot_seconds=FAST_SLOT_SECONDS)],
    indirect=True,
    ids=str,
)
async def test_a_submission_wakes_a_relay_waiting_to_forget(
    chain: LocalChain, served: Served
) -> None:
    """Once a submission has landed, the relay waits only to forget it, an hour on; a second
    submission wakes it at once, and lands within a few slots, not after the hour."""
    async with served.interface() as http:
        price = await quoted(http, served.chain_id, fresh_client("anyone").address)
        await served.start()
        statuses: list[str] = []
        for name in ("lands first", "lands after"):
            taken = await submit_paying(http, chain, served, name, price)
            status: dict[str, Any] = {}
            for _ in range(FAST_SLOTS):
                await asyncio.sleep(FAST_SLOT_SECONDS)
                await served.builder.build_block()
                status = (await http.get(taken.headers["Location"])).json()
                if status["status"] != "pending":
                    break
            statuses.append(status["status"])
            # the relay idle again: nothing pending, only the forgetting left
            for _ in range(3):
                await asyncio.sleep(FAST_SLOT_SECONDS)
                await served.builder.build_block()

    assert statuses == ["included", "included"]


def calling(requests: list[list[str]], method: str) -> list[int]:
    """How many times each request that calls `method` calls it."""
    return [r.count(method) for r in requests if method in r]


@pytest.mark.parametrize("backend", ["worker"], indirect=True)
@pytest.mark.parametrize("served", [Setup(accounts=3)], indirect=True, ids=str)
async def test_the_worker_reads_a_queue_in_batches(chain: LocalChain, served: Served) -> None:
    """Three submissions in flight cost the node one request where they need the same read:
    closing the batch simulates all three in one; the block's bundles are simulated in one;
    and their receipts are looked for in one. A Durable Object's work for a slot is a few
    requests, whatever the queue."""
    async with served.interface() as http:
        price = await quoted(http, served.chain_id, fresh_client("anyone").address)
        taken = [await submit_paying(http, chain, served, f"batched {i}", price) for i in range(3)]
        before = len(served.node)
        await served.close_batch()
        closing = served.node[before:]
        before = len(served.node)
        await served.on_block(await served.head())
        sending = served.node[before:]
        await served.builder.build_block()
        before = len(served.node)
        await served.on_block(await served.head())
        settling = served.node[before:]
        statuses = [(await http.get(t.headers["Location"])).json()["status"] for t in taken]

    # the simulations: the requests calling eth_call that aren't the accounts' survey (which
    # reads the payee's tokens with eth_call too, in its own one request)
    simulating = [r for r in closing if "eth_getBalance" not in r]
    assert calling(simulating, "eth_call") == [3]
    assert calling(sending, "eth_call") == [3]
    assert calling(settling, "eth_getTransactionReceipt") == [3]
    assert statuses == ["included"] * 3


# secp256k1's order: an s over half of it is EIP-7702's "high s"
SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


async def test_routes_only_canonical_chain_ids(workerd: str) -> None:
    """The Worker routes a chain id only as its canonical decimal: a padded one is refused by
    the Worker itself ("not found: see /"), never reaching an object, so no request makes a second
    object for a chain. (A chain the configuration doesn't name is refused there too, outside
    tests; here the hooks configure chains, so that check is off.)"""
    async with httpx.AsyncClient(base_url=workerd) as http:
        padded = await http.get("/031337/v1/config")

    assert padded.status_code == 404
    assert padded.json()["error"]["message"] == "not found: see /", padded.text


async def test_a_key_not_bound_is_a_configuration_error(
    chain: LocalChain, schedule: GasSchedule, workerd: str
) -> None:
    """A bundler's identity key named but not bound, or a float key that isn't one, is the
    configuration's mistake: refused, naming the binding and never showing its value, before
    the Worker reads the chain."""
    payee = to_checksum_address(await chain.account(PAYEE_ACCOUNT))
    chain_id = await chain.chain_id()
    identified = Bundler(BUILDER_URL, Mode.STANDALONE, identity_key="UNBOUND_IDENTITY")
    config = replace(relay_config(Setup(), chain.url, payee, BUILDER_URL), bundlers=(identified,))
    test = {"config": msgspec.to_builtins(config)}
    hooks = f"/__test/{chain_id}"
    async with httpx.AsyncClient(base_url=workerd) as http:
        unbound = await http.post(
            f"{hooks}/configure", json={**test, "floatKey": "0x" + TEST_FLOAT.key.hex()}
        )
        no_float = await http.post(f"{hooks}/configure", json={**test, "floatKey": "0x"})

    assert unbound.status_code == 404, unbound.text
    assert unbound.json()["error"]["message"].startswith("UNBOUND_IDENTITY: no private key")
    assert no_float.status_code == 404, no_float.text
    assert no_float.json()["error"]["message"].startswith("FLOAT_KEY: no private key")


async def test_malformed_requests_are_refused_not_failed(chain: LocalChain, served: Served) -> None:
    """A body that isn't JSON, or is too large, a quantity past any range, an amount past a
    uint256's and a token that isn't an address are the client's mistakes: 400, never a 500. A
    token named twice is quoted once."""
    payee = served.payee
    client = fresh_client("sends nonsense")
    call, auth = await signed_call(chain, client, payee, 1)
    huge = submission(call, auth, payee, 1, await served.head() + 5)
    huge["gas"] = "0x" + "f" * 80
    past = submission(call, auth, payee, 2**256, await served.head() + 5)
    large = submission(call, auth, payee, 1, await served.head() + 5)
    large["call"] = {**call, "data": "0x" + "00" * MAX_BODY_BYTES}
    params = {"gas": hex(CLIENT_GAS), "callBytes": hex(100)}
    submissions = f"/{served.chain_id}/v1/submissions"
    async with served.interface() as http:
        garbled = await http.post(submissions, content=b"{not json", headers=JSON)
        overflowing = await http.post(submissions, json=huge)
        too_much = await http.post(submissions, json=past)
        too_large = await http.post(submissions, json=large)
        not_a_token = await http.get(
            f"/{served.chain_id}/v1/quote", params={**params, "tokens": ["0x1234"]}
        )
        twice = await http.get(
            f"/{served.chain_id}/v1/quote", params={**params, "tokens": [USDC.address] * 2}
        )

    for refused in (garbled, overflowing, too_much, too_large, not_a_token):
        assert refused.status_code == 400, refused.text
        assert refused.json()["error"]["code"] == -32602, refused.text
    assert [q["token"] for q in twice.json()["quotes"]] == [USDC.address], twice.text


async def test_authorizations_out_of_range_are_refused(chain: LocalChain, served: Served) -> None:
    """More authorizations than allowed, a yParity that isn't 0 or 1, a high s, and a nonce past
    a uint64's: each refused before any work, as EIP-7702 would skip or reject them."""
    payee = served.payee
    client = fresh_client("signs oddly")
    call, auth = await signed_call(chain, client, payee, 1)
    deadline = await served.head() + 5
    two = submission(call, auth, payee, 1, deadline)
    two["authorizationList"] = [auth, auth]
    parity = submission(call, {**auth, "yParity": "0x2"}, payee, 1, deadline)
    flipped = {
        **auth,
        "s": hex(SECP256K1_N - int(auth["s"], 16)),
        "yParity": hex(1 - int(auth["yParity"], 16)),
    }
    high_s = submission(call, flipped, payee, 1, deadline)
    nonce = submission(call, {**auth, "nonce": hex(2**64)}, payee, 1, deadline)
    async with served.interface() as http:
        answers = [
            await http.post(f"/{served.chain_id}/v1/submissions", json=body)
            for body in (two, parity, high_s, nonce)
        ]

    for answer in answers:
        assert answer.status_code == 400, answer.text
        assert answer.json()["error"]["data"].startswith("authorizationList"), answer.text


@pytest.mark.parametrize("served", [Setup(accounts=1, max_pending=1)], indirect=True, ids=str)
async def test_a_submission_already_sent_isnt_evicted(chain: LocalChain, served: Served) -> None:
    """Once its transaction is out, a submission may land: a more profitable one is refused
    rather than evicting it, and the one sent lands."""
    async with served.interface() as http:
        price = await quoted(http, served.chain_id, fresh_client("anyone").address)
        sent = await submit_paying(http, chain, served, "already sent", price)
        await served.close_batch()
        await served.on_block(await served.head())
        richer = await submit_paying(http, chain, served, "pays more, later", 3 * price)
        await served.builder.build_block()
        await served.on_block(await served.head())
        status = (await http.get(sent.headers["Location"])).json()

    assert sent.status_code == 202
    assert richer.status_code == 503, richer.text
    assert status["status"] == "included"


@pytest.mark.parametrize("served", [Setup(retention_seconds=0)], indirect=True, ids=str)
async def test_finished_submissions_are_forgotten(chain: LocalChain, served: Served) -> None:
    """A submission's status is kept `retention_seconds` after it finishes, then forgotten:
    nothing stored grows without bound."""
    async with served.interface() as http:
        price = await quoted(http, served.chain_id, fresh_client("anyone").address)
        taken = await submit_paying(http, chain, served, "lands, then is forgotten", price)
        await land(served)
        landed = (await http.get(taken.headers["Location"])).json()
        await served.on_block(await served.head())
        forgotten = await http.get(taken.headers["Location"])

    assert landed["status"] == "included"
    assert forgotten.status_code == 404, forgotten.text


@pytest.mark.parametrize("served", [Setup(accounts=1, max_pending=2)], indirect=True, ids=str)
async def test_concurrent_submissions_are_queued_once(chain: LocalChain, served: Served) -> None:
    """The same submission sent twice at once is taken once; three sent at once to a queue of
    two never leave more than two pending."""
    async with served.interface() as http:
        price = await quoted(http, served.chain_id, fresh_client("anyone").address)
        client = fresh_client("sends it twice")
        await chain.mint(USDC, client.address, price)
        call, auth = await signed_call(chain, client, served.payee, price)
        body = submission(call, auth, served.payee, price, await served.head() + 20)
        url = f"/{served.chain_id}/v1/submissions"
        twice = await asyncio.gather(http.post(url, json=body), http.post(url, json=body))
        crowd = await asyncio.gather(
            *(
                submit_paying(http, chain, served, f"one of a crowd {i}", (2 + i) * price)
                for i in range(3)
            )
        )
        taken = [r for r in (*twice[:1], *crowd) if r.status_code in (200, 202)]
        statuses = [(await http.get(r.headers["Location"])).json()["status"] for r in taken]

    assert sorted(r.status_code for r in twice) == [200, 202], [r.text for r in twice]
    assert twice[0].json()["id"] == twice[1].json()["id"]
    assert statuses.count("pending") <= 2, statuses


async def treasury_entry(served: Served) -> dict[str, Any]:
    """The chain's treasury on the root, as its last run left it."""
    root = (await served.http.get("/")).json()
    entry: dict[str, Any] = root["chains"][0]["treasury"]
    return entry


async def probe(chain: LocalChain, served: Served) -> tuple[int, int]:
    """The float's target and the payee's reserve as the treasury finds them now, its gas
    measured: from a run with the payee holding no ETH, so it can send nothing. The float and
    its accounts aren't changed after, so the next run measures no spend."""
    await chain.set_eth_balance(served.payee, 0)
    await served.treasury()
    entry = await treasury_entry(served)
    assert entry["outcome"].startswith("nothing sent"), entry["outcome"]
    return int(entry["target"], 16), int(entry["reserve"], 16)


async def a_transaction(chain: LocalChain, served: Served, schedule: GasSchedule) -> int:
    """What a transaction of the largest gas costs at the fee ceiling: the least a float that
    isn't running dry holds."""
    async with httpx.AsyncClient() as client:
        live = Chain(client, served.chain_id, (chain.url,))
        return CLIENT_GAS * fee_ceiling(await live.latest(), 1).max_fee_per_gas


async def weth_dust(chain: LocalChain, payee: str, forwarded_to: str | None = None) -> int:
    """WETH for the payee worth ten times its unwrap's gas at the base fee, and the forward's
    to `forwarded_to` if given, the gas measured: more than they cost, but too little to be
    worth it, at under 50 times."""
    await chain.mint(WETH, payee, 1)
    withdraw = chain.calldata("withdraw(uint256)", ["uint256"], [1])
    calls: list[dict[str, str]] = [{"from": payee, "to": WETH.address, "data": withdraw}]
    if forwarded_to is not None:
        calls.append({"from": payee, "to": forwarded_to, "value": "0x1"})
    gas = sum([int(await chain.rpc("eth_estimateGas", [c]), 16) for c in calls])
    dust = 10 * gas * await chain.base_fee()
    await chain.mint(WETH, payee, dust - 1)
    return dust


async def approve_router(chain: LocalChain, payee: str) -> None:
    """The router may spend the payee's USDC and USDT, as the treasury approves it ahead of
    its first swaps: for the tests that aren't about that."""
    for token in (USDC, USDT):
        await chain.send(
            payee,
            token.address,
            "approve(address,uint256)",
            ["address", "uint256"],
            [SWAP_ROUTER, 2**256 - 1],
        )


async def a_gwei_base_fee(chain: LocalChain) -> None:
    """The latest block's base fee 1 gwei: a treasury's costs, not anvil's decayed fee."""
    await chain.set_base_fee(10**9)
    await chain.rpc("evm_mine", [])


async def test_treasury_forwards_the_payees_eth(chain: LocalChain, served: Served) -> None:
    """An empty float, a payee holding plenty of ETH: one run tops the float up to its target
    (every account funded once, as nothing's spent yet), the payee keeping its reserve, and
    the root shows the run."""
    await approve_router(chain, served.payee)
    await a_gwei_base_fee(chain)
    await chain.set_eth_balance(TEST_FLOAT.address, 0)
    await served.treasury()
    entry = await treasury_entry(served)
    await served.builder.build_block()
    assert entry["outcome"].startswith("sent: forwarding"), entry["outcome"]
    assert await chain.eth_balance(TEST_FLOAT.address) == int(entry["target"], 16) > 0
    assert await chain.eth_balance(served.payee) >= int(entry["reserve"], 16)
    root = (await served.http.get("/")).json()
    browsed = (await served.http.get("/", headers={"accept": "text/html"})).text
    assert "<h2>Treasury</h2>" in browsed
    # the treasury's read is the accounts' latest: the root shows it
    status = root["chains"][0]["accountsStatus"]
    assert status["floatBalance"] == "0x0"
    assert status["block"] == entry["block"]
    assert len(status["balances"]) == len(status["nonces"]) == 2
    # each token by its own symbol(), as measured
    assert [h["symbol"] for h in status["payeeTokens"]] == ["USDC", "USDT", "WETH"]
    assert browsed == page(origin(served), msgspec.convert(root, IndexResult))


async def test_the_root_shows_every_balance_as_last_read(chain: LocalChain, served: Served) -> None:
    """Issue #5: the payee is read with the float and its accounts, its tokens too, so every
    balance on the root is as of one read; the treasury shows only its run's own figures. A
    run that forwards, then a read after it lands: the payee shows what it holds now."""
    await approve_router(chain, served.payee)
    await a_gwei_base_fee(chain)
    await chain.set_eth_balance(TEST_FLOAT.address, 0)
    await chain.mint(USDC, served.payee, 1)
    await served.treasury()  # forwards to the float
    await served.builder.build_block()
    await served.treasury()  # reads again: the float at its target, nothing to send
    root = (await served.http.get("/")).json()
    browsed = (await served.http.get("/", headers={"accept": "text/html"})).text

    (entry,) = root["chains"]
    status = entry["accountsStatus"]
    assert int(status["payeeBalance"], 16) == await chain.eth_balance(served.payee)
    assert int(status["payeeNonce"], 16) == await chain.nonce(served.payee)
    usdc = next(h for h in status["payeeTokens"] if h["symbol"] == "USDC")
    assert int(usdc["amount"], 16) == await chain.balance_of(USDC, served.payee)
    assert int(status["floatBalance"], 16) == await chain.eth_balance(TEST_FLOAT.address)
    # the run's own figures (spendADay once a run has measured spend), none of the payee's
    assert {"at", "block", "target", "reserve", "covers", "outcome"} <= set(entry["treasury"])
    assert not any(key.startswith("payee") for key in entry["treasury"])
    assert "<td>payee</td>" in browsed


async def test_treasury_unwraps_the_payees_weth(chain: LocalChain, served: Served) -> None:
    """A payee at its reserve, holding WETH: the WETH the float needs is unwrapped and the
    float topped up to its target, in one bundle, the payee keeping its reserve."""
    await approve_router(chain, served.payee)
    await a_gwei_base_fee(chain)
    await chain.set_eth_balance(TEST_FLOAT.address, 0)
    target, _ = await probe(chain, served)
    await chain.mint(WETH, served.payee, 2 * target)
    _, reserve = await probe(chain, served)
    await chain.set_eth_balance(served.payee, reserve)
    await served.treasury()
    entry = await treasury_entry(served)
    await served.builder.build_block()
    assert entry["outcome"].startswith("sent: converting"), entry["outcome"]
    assert await chain.eth_balance(TEST_FLOAT.address) == int(entry["target"], 16)
    assert await chain.eth_balance(served.payee) >= int(entry["reserve"], 16)
    assert 0 < await chain.balance_of(WETH, served.payee) < 2 * target


async def test_treasury_skips_a_costly_conversion(
    chain: LocalChain, served: Served, schedule: GasSchedule
) -> None:
    """WETH worth ten times its unwrap's gas isn't converted while the float isn't running dry
    (it covers a transaction, and nothing's been measured to run out), however short of its
    target: the payee at its reserve, nothing is sent."""
    await approve_router(chain, served.payee)
    await a_gwei_base_fee(chain)
    dust = await weth_dust(chain, served.payee)
    held = await a_transaction(chain, served, schedule) + 1
    await chain.set_eth_balance(TEST_FLOAT.address, held)
    _, reserve = await probe(chain, served)
    await chain.set_eth_balance(served.payee, reserve)
    await served.treasury()
    entry = await treasury_entry(served)
    await served.builder.build_block()
    assert entry["outcome"].startswith("nothing sent"), entry["outcome"]
    assert f"{WETH.address}: converting {dust} costs too much" in entry["outcome"]
    assert await chain.eth_balance(TEST_FLOAT.address) == held
    assert await chain.balance_of(WETH, served.payee) == dust


async def test_treasury_converts_anyway_for_an_empty_float(
    chain: LocalChain, served: Served
) -> None:
    """Costly WETH (ten times its unwrap's and the forward's gas) is converted when the float
    can't cover a transaction: running dry, the cost doesn't count. All of it is unwrapped, and
    forwarded above the reserve."""
    await approve_router(chain, served.payee)
    await a_gwei_base_fee(chain)
    await chain.set_eth_balance(TEST_FLOAT.address, 0)
    await weth_dust(chain, served.payee, forwarded_to=TEST_FLOAT.address)
    _, reserve = await probe(chain, served)
    await chain.set_eth_balance(served.payee, reserve)
    await served.treasury()
    entry = await treasury_entry(served)
    await served.builder.build_block()
    assert entry["outcome"].startswith("sent: converting"), entry["outcome"]
    assert await chain.balance_of(WETH, served.payee) == 0
    assert 0 < await chain.eth_balance(TEST_FLOAT.address) <= int(entry["target"], 16)
    assert await chain.eth_balance(served.payee) >= int(entry["reserve"], 16)


async def test_treasury_swaps_usdc_for_an_empty_float(chain: LocalChain, served: Served) -> None:
    """A payee holding USDC, the float empty. The first run approves the router for each
    token, and does nothing else; the next, the payee at its reserve, swaps the USDC needed
    along its v3 pool through Uniswap's router and forwards the ETH: the float at its target,
    the payee keeping its reserve."""
    await chain.mint(USDC, served.payee, STABLES)
    await a_gwei_base_fee(chain)
    await chain.set_eth_balance(TEST_FLOAT.address, 0)
    await served.treasury()
    approving = await treasury_entry(served)
    built = await served.builder.build_block()
    assert approving["outcome"].startswith("sent: approving"), approving["outcome"]
    assert USDC.address in approving["outcome"] and USDT.address in approving["outcome"]
    assert len(built.transactions) == 2
    assert await chain.eth_balance(TEST_FLOAT.address) == 0
    _, reserve = await probe(chain, served)
    await chain.set_eth_balance(served.payee, reserve)
    await served.treasury()
    entry = await treasury_entry(served)
    built = await served.builder.build_block()
    assert entry["outcome"].startswith("sent: converting"), entry["outcome"]
    assert USDC.address in entry["outcome"]
    assert len(built.transactions) == 2  # the swap, the forward
    assert await chain.eth_balance(TEST_FLOAT.address) == int(entry["target"], 16)
    assert await chain.eth_balance(served.payee) >= int(entry["reserve"], 16)
    assert 0 < await chain.balance_of(USDC, served.payee) < STABLES


async def test_treasury_swaps_usdt_along_two_legs(
    chain: LocalChain, served: Served, schedule: GasSchedule
) -> None:
    """A payee at its reserve, holding USDT, the float covering a transaction (short of its
    target, but not dry): the USDT swaps along a v3 pool to USDC and on through the v2 pair to
    WETH (the second leg swapping the router's own balance), enough that its gas is within
    the cost allowed, and the float is topped up to its target."""
    await chain.mint(USDT, served.payee, STABLES)
    await approve_router(chain, served.payee)
    await a_gwei_base_fee(chain)
    await chain.set_eth_balance(
        TEST_FLOAT.address, await a_transaction(chain, served, schedule) + 1
    )
    _, reserve = await probe(chain, served)
    await chain.set_eth_balance(served.payee, reserve)
    await served.treasury()
    entry = await treasury_entry(served)
    built = await served.builder.build_block()
    assert entry["outcome"].startswith("sent: converting"), entry["outcome"]
    assert USDT.address in entry["outcome"]
    assert len(built.transactions) == 2
    assert await chain.eth_balance(TEST_FLOAT.address) == int(entry["target"], 16)
    assert await chain.eth_balance(served.payee) >= int(entry["reserve"], 16)
    assert 0 < await chain.balance_of(USDT, served.payee) < STABLES


async def measured(chain: LocalChain, payee: str, to: str, data: bytes | str) -> int:
    """A call's gas from the payee, as the treasury measures it: eth_estimateGas, with its
    headroom."""
    hex_data = data if isinstance(data, str) else "0x" + data.hex()
    estimate = await chain.rpc("eth_estimateGas", [{"from": payee, "to": to, "data": hex_data}])
    return ceil(int(estimate, 16) * GAS_HEADROOM)


async def test_treasury_converts_what_it_can_afford(
    chain: LocalChain, served: Served, schedule: GasSchedule
) -> None:
    """A payee holding USDC and WETH, its ETH enough for the WETH's unwrap at the max fee but
    not the USDC's swap, the float empty: the swap it can't afford is passed over, not the
    run: the WETH is unwrapped and forwarded."""
    await approve_router(chain, served.payee)
    await a_gwei_base_fee(chain)
    await chain.mint(USDC, served.payee, STABLES)
    await chain.mint(WETH, served.payee, 10**18)
    await chain.set_eth_balance(TEST_FLOAT.address, 0)
    unwrap = await measured(
        chain,
        served.payee,
        WETH.address,
        chain.calldata("withdraw(uint256)", ["uint256"], [10**18]),
    )
    swap = await measured(
        chain,
        served.payee,
        SWAP_ROUTER,
        swap_calldata(
            [Step(PoolKind.UNISWAP_V3, USDC.address, WETH.address, 500)],
            STABLES,
            0,
            served.payee,
        ),
    )
    async with httpx.AsyncClient() as client:
        live = Chain(client, served.chain_id, (chain.url,))
        latest = await live.latest()
    max_fee = lasting_fees(latest, market_tip(latest.tips), BUNDLE_BLOCKS).max_fee_per_gas
    assert unwrap < swap
    await chain.set_eth_balance(served.payee, (unwrap + swap) // 2 * max_fee)
    await served.treasury()
    entry = await treasury_entry(served)
    await served.builder.build_block()
    assert entry["outcome"].startswith("sent: converting"), entry["outcome"]
    assert f"{USDC.address}: the payee holds too little ETH for its gas" in entry["outcome"]
    assert await chain.balance_of(WETH, served.payee) < 10**18
    assert await chain.balance_of(USDC, served.payee) == STABLES
    assert await chain.eth_balance(TEST_FLOAT.address) > 0


async def test_treasury_reserves_for_a_token_spent(chain: LocalChain, served: Served) -> None:
    """The payee's reserve counts a token's conversion once measured, after the payee holds
    none of it: the gas is there when the token comes back."""
    await approve_router(chain, served.payee)
    await a_gwei_base_fee(chain)
    await chain.mint(USDC, served.payee, STABLES)
    _, holding = await probe(chain, served)
    await chain.mint(USDC, served.payee, -STABLES)
    _, spent = await probe(chain, served)
    assert spent == holding
