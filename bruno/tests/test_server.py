"""The relay behind its HTTP interface, on anvil: prices read from Uniswap pools' own code, a
client quoting, submitting over REST and JSON-RPC, and its submission bundled through the
mock builder until it lands."""

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
from typing import Any, override

import httpx
import msgspec
import pytest
from eth_account import Account
from eth_typing import HexStr
from eth_utils.address import to_checksum_address

from bruno.bundle import Mode
from bruno.chain import Chain
from bruno.config import Bundler, Hop, PoolKind
from bruno.evm import NATIVE_ASSET, SECP256K1_N, EncodedCall
from bruno.forks import GasSchedule
from bruno.net.jsonrpc import Answer, Call
from bruno.prices import PriceError, Prices
from bruno.runner import UNDERPAID
from bruno.server.app import MAX_BODY_BYTES
from bruno.server.wire import Code
from bruno.tests.calibur import calibur_batch, fresh_client
from bruno.tests.deployments import CLIENT_GAS, transfer
from bruno.tests.interface import quote, rpc, signed_call, submission, submit_paying
from bruno.tests.localchain import (
    CALIBUR,
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
        amount = await quote(http, chain_id, authority=client.address)
        await chain.mint(USDC, client.address, amount)
        call, auth = await signed_call(chain, client, payee, amount)
        head = await served.head()
        body = submission(call, auth, payee, amount, head + 10)
        before = await chain.balance_of(USDC, payee)

        taken = await http.post(f"/{chain_id}/v1/submissions", json=body)
        again = await http.post(f"/{chain_id}/v1/submissions", json=body)
        block = await served.land()
        landed = await http.get(taken.headers["Location"])

    assert taken.status_code == 202
    assert again.status_code == 200
    assert again.json()["id"] == taken.json()["id"]
    assert landed.json()["status"] == "included"
    assert bytes.fromhex(landed.json()["transactionHash"][2:]) in block.transactions
    assert await chain.balance_of(USDC, payee) - before == amount


async def test_refusals_over_json_rpc(chain: LocalChain, served: Served) -> None:
    """Over JSON-RPC: an amount under the price is underpaid, with the price; a call that
    pays less than it says, or can't pay, fails simulation with the runner's revert data."""
    payee = served.payee
    chain_id = served.chain_id
    client = fresh_client("a JSON-RPC client")
    async with served.interface() as http:
        amount = await quote(http, chain_id, authority=client.address)
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


async def test_paying_in_weth(chain: LocalChain, served: Served) -> None:
    """WETH needs no pools: it's priced one to one with ETH, and a call paying in it lands, the
    payee's WETH up by the amount."""
    async with served.interface() as http:
        client = fresh_client("pays in WETH")
        price = await quote(http, served.chain_id, WETH.address, authority=client.address)
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
        await served.land()
        status = (await http.get(taken.headers["Location"])).json()

    assert status["status"] == "included", status
    assert await chain.balance_of(WETH, served.payee) - before == price


@pytest.mark.parametrize("served", [Setup(accounts=1, max_pending=2)], indirect=True, ids=str)
async def test_queue_ranks_by_profit(chain: LocalChain, served: Served) -> None:
    """With room for two, a third more profitable submission evicts the least profitable,
    and a fourth less profitable than both is refused with what would get it in. A batch of
    one account takes the most profitable."""
    chain_id = served.chain_id
    async with served.interface() as http:
        price = await quote(http, chain_id, authority=fresh_client("anyone").address)
        low = await submit_paying(http, chain, served, "pays the price", price)
        mid = await submit_paying(http, chain, served, "pays twice", 2 * price)
        high = await submit_paying(http, chain, served, "pays three times", 3 * price)
        refused = await submit_paying(http, chain, served, "pays it again", price + 1)
        await served.land()
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
        price = await quote(http, chain_id, authority=fresh_client("anyone").address)
        taken = await submit_paying(http, chain, served, "spends it elsewhere", price)
        client = fresh_client("spends it elsewhere")
        await chain.mint(USDC, client.address, -price)  # its balance written back to 0
        await served.land()
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
        price = await quote(http, chain_id, authority=fresh_client("anyone").address)
        first = await submit_paying(http, chain, served, "the first", price)
        await served.land()
        float_nonce = await chain.nonce(TEST_FLOAT.address)
        second = await submit_paying(http, chain, served, "the second", price)
        await served.land()
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
        amount = await quote(http, chain_id, NATIVE_ASSET, authority=client.address)
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
        await served.land()
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
        price = await quote(http, served.chain_id, authority=fresh_client("anyone").address)
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
        price = await quote(http, served.chain_id, authority=fresh_client("anyone").address)
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
        price = await quote(http, served.chain_id, authority=fresh_client("anyone").address)
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
        price = await quote(http, served.chain_id, authority=fresh_client("anyone").address)
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
    uint256's, an authorization list that's null and a token that isn't an address are the
    client's mistakes: 400, never a 500. A token named twice is quoted once."""
    payee = served.payee
    client = fresh_client("sends nonsense")
    call, auth = await signed_call(chain, client, payee, 1)
    huge = submission(call, auth, payee, 1, await served.head() + 5)
    huge["gas"] = "0x" + "f" * 80
    past = submission(call, auth, payee, 2**256, await served.head() + 5)
    large = submission(call, auth, payee, 1, await served.head() + 5)
    large["call"] = {**call, "data": "0x" + "00" * MAX_BODY_BYTES}
    null_list = submission(call, auth, payee, 1, await served.head() + 5)
    null_list["authorizationList"] = None
    params = {"gas": hex(CLIENT_GAS), "callBytes": hex(100)}
    submissions = f"/{served.chain_id}/v1/submissions"
    async with served.interface() as http:
        garbled = await http.post(submissions, content=b"{not json", headers=JSON)
        overflowing = await http.post(submissions, json=huge)
        too_much = await http.post(submissions, json=past)
        too_large = await http.post(submissions, json=large)
        no_list = await http.post(submissions, json=null_list)
        not_a_token = await http.get(
            f"/{served.chain_id}/v1/quote", params={**params, "tokens": ["0x1234"]}
        )
        twice = await http.get(
            f"/{served.chain_id}/v1/quote", params={**params, "tokens": [USDC.address] * 2}
        )

    for refused in (garbled, overflowing, too_much, too_large, no_list, not_a_token):
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
        price = await quote(http, served.chain_id, authority=fresh_client("anyone").address)
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
        price = await quote(http, served.chain_id, authority=fresh_client("anyone").address)
        taken = await submit_paying(http, chain, served, "lands, then is forgotten", price)
        await served.land()
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
        price = await quote(http, served.chain_id, authority=fresh_client("anyone").address)
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
