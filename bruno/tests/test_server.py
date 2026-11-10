"""The relay behind its HTTP interface, on anvil: prices read from Uniswap pools' own code, a
client quoting, submitting over REST and JSON-RPC, and its submission bundled through the
mock builder until it lands."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import replace
from fractions import Fraction
from typing import Any, override

import httpx
import msgspec
import pytest
from eth_account import Account
from eth_typing import HexStr
from eth_utils.address import to_checksum_address

from bruno.bundle import Mode
from bruno.chain import Chain, list_of, object_of
from bruno.config import BuilderConfig, ChainConfig, ConfigError, Hop, Misconfiguration, PoolKind
from bruno.evm import NATIVE_ASSET, SECP256K1_N, EncodedCall
from bruno.forks import GasSchedule
from bruno.jsonrpc import Answer, Call
from bruno.prices import PriceError, Prices
from bruno.runner import UNDERPAID
from bruno.server.app import MAX_BODY_BYTES
from bruno.server.wire import Code
from bruno.tests.calibur import calibur_batch, fresh_client
from bruno.tests.deployments import CLIENT_GAS, MAX_GAS, transfer
from bruno.tests.interface import quote, quoting, rpc, signed_call, submission, submit_paying
from bruno.tests.localchain import (
    CALIBUR,
    ENTRY_POINT_0_7,
    LOCAL_POLL,
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
    CLOCK_SLOTS,
    FAST_BATCH_SECONDS,
    FAST_SLOT_SECONDS,
    JSON,
    MAX_DEADLINE_BLOCKS,
    PAYEE_ACCOUNT,
    Served,
    Setup,
    relay_config,
)
from bruno.transactions import authorization_json
from bruno.units import Seconds


async def test_prices_from_uniswap_pools(chain: LocalChain, schedule: GasSchedule) -> None:
    """A token's price in wei along its path: v3 and v2 from token0, v3 from token1, and a
    path that doesn't end at WETH."""
    usdc, usdt = USDC.address, USDT.address
    v2 = Hop(PoolKind.UNISWAP_V2, UNISWAP_V2_USDC_WETH)
    v3_usdc = Hop(PoolKind.UNISWAP_V3, UNISWAP_V3_USDC_WETH)
    v3_usdt = Hop(PoolKind.UNISWAP_V3, UNISWAP_V3_WETH_USDT)
    async with httpx.AsyncClient() as client:
        live = await chain.chain(client)
        by_v3 = Prices(live, WETH.address, {usdc: (v3_usdc,), usdt: (v3_usdt,)}, Seconds(0))
        by_v2 = Prices(live, WETH.address, {usdc: (v2,)}, Seconds(0))
        round_trip = Prices(live, WETH.address, {usdc: (v3_usdc, v2)}, Seconds(0))

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
        live = CountingChain(client, await chain.chain_id(), (chain.url,), poll=LOCAL_POLL)
        prices = Prices(live, WETH.address, {USDC.address: (hop,)}, Seconds(0))
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


async def test_a_submission_unlanded_by_its_deadline_expires(
    chain: LocalChain, served: Served
) -> None:
    """Sent, but no block built with it before its deadline passes: it expires, unpaid."""
    client = fresh_client("waits too long")
    async with served.interface() as http:
        price = await quote(http, served.chain_id, authority=client.address)
        await chain.mint(USDC, client.address, price)
        call, auth = await signed_call(chain, client, served.payee, price)
        deadline = await served.head() + 2
        taken = await http.post(
            f"/{served.chain_id}/v1/submissions",
            json=submission(call, auth, served.payee, price, deadline),
        )
        assert taken.status_code == 202, taken.text
        await served.send()
        while await served.head() <= deadline:  # blocks built without it
            await chain.rpc("evm_mine", [])
        await served.on_block(await served.head())
        status = (await http.get(taken.headers["Location"])).json()

    assert status["status"] == "expired"
    assert status["attempts"] == "0x1"
    assert await chain.balance_of(USDC, client.address) == price


async def test_a_float_short_of_the_funding_sends_none_until_topped_up(
    chain: LocalChain, served: Served
) -> None:
    """The float can't pay for the funding transaction: none is sent, and the submission
    waits; topped up, the next batch funds it, and it lands."""
    async with served.interface() as http:
        price = await quote(http, served.chain_id, authority=fresh_client("anyone").address)
        await chain.set_eth_balance(TEST_FLOAT.address, 1)
        taken = await submit_paying(http, chain, served, "waits for the float", price)
        await served.land()
        waiting = (await http.get(taken.headers["Location"])).json()
        float_nonce = await chain.nonce(TEST_FLOAT.address)
        await chain.set_eth_balance(TEST_FLOAT.address, 10**18)
        await served.land()
        landed = (await http.get(taken.headers["Location"])).json()

    assert waiting["status"] == "pending"
    assert waiting["attempts"] == "0x0"
    assert landed["status"] == "included"
    assert await chain.nonce(TEST_FLOAT.address) == float_nonce + 1


async def test_a_funding_whose_nonce_the_float_used_elsewhere_is_funded_again(
    chain: LocalChain, served: Served
) -> None:
    """The float's nonce used by another transaction before the funding lands: the funding is
    dropped, the next batch funds again at the float's next nonce, and the submission lands."""
    async with served.interface() as http:
        price = await quote(http, served.chain_id, authority=fresh_client("anyone").address)
        float_nonce = await chain.nonce(TEST_FLOAT.address)
        taken = await submit_paying(http, chain, served, "funded again", price)
        await served.close_batch()
        await chain.rpc("anvil_setNonce", [TEST_FLOAT.address, hex(float_nonce + 1)])
        await chain.rpc("evm_mine", [])
        funding_left = await served.on_block(await served.head())
        await served.land()
        landed = (await http.get(taken.headers["Location"])).json()

    assert not funding_left
    assert landed["status"] == "included"
    assert await chain.nonce(TEST_FLOAT.address) == float_nonce + 2


@pytest.mark.parametrize("served", [Setup(funding_multiplier="2.5")], indirect=True, ids=str)
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
    # the latest block it could have been: MAX_DEADLINE_BLOCKS ahead of the relay's head
    assert refused.json()["error"]["data"] == {"field": "maxBlockNumber", "most": hex(deadline - 1)}


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


def status_of(
    http: httpx.AsyncClient, taken: httpx.Response
) -> Callable[[], Awaitable[dict[str, Any]]]:
    """What reads the status of the submission `taken`, for a wait."""

    async def read() -> dict[str, Any]:
        return (await http.get(taken.headers["Location"])).json()

    return read


def finished(status: dict[str, Any]) -> bool:
    """Whether a submission, as its status reads, is no longer pending."""
    return status["status"] != "pending"


@pytest.mark.parametrize(
    "served",
    [Setup(batch_seconds=FAST_BATCH_SECONDS, slot_seconds=FAST_SLOT_SECONDS)],
    indirect=True,
    ids=str,
)
async def test_the_relay_s_own_clock_runs_only_while_there_s_work(
    chain: LocalChain, served: Served
) -> None:
    """The relay's own clock (the Worker's alarms, the Python relay's loop), nothing driven but
    the chain, a block a slot apart. With nothing queued it stops, so an idle relay costs
    nothing, and a quote doesn't start it. A submission does: the clock alone makes the batch,
    funds it and bundles it for a block, which lands it. Then it waits only to forget, an hour
    on; a second submission wakes it at once, and lands within a few slots, not after the hour.
    The clock's state is the relay's own, read at once: a Worker alarm that's running reads no
    alarm, the next set as it ends."""
    async with served.interface() as http:
        await served.start()
        await served.pace.until(served.clock, lambda clock: clock.stopped, CLOCK_SLOTS)
        price = await quote(http, served.chain_id, authority=fresh_client("anyone").address)
        quoting = await served.clock()
        before = await chain.balance_of(USDC, served.payee)
        statuses: list[str] = []
        for name in ("lands first", "lands after"):
            taken = await submit_paying(http, chain, served, name, price)
            status = await served.pace.until(status_of(http, taken), finished, CLOCK_SLOTS)
            statuses.append(status["status"])
            await served.pace.until(
                served.clock, lambda clock: clock.idle and clock.waiting_to_forget, CLOCK_SLOTS
            )

    assert quoting.stopped, quoting
    assert statuses == ["included", "included"]
    assert await chain.balance_of(USDC, served.payee) - before == 2 * price


@pytest.mark.parametrize(
    "served",
    [
        Setup(
            batch_seconds=FAST_BATCH_SECONDS,
            slot_seconds=FAST_SLOT_SECONDS,
            retention_seconds=2 * FAST_SLOT_SECONDS,
        )
    ],
    indirect=True,
    ids=str,
)
async def test_the_relay_s_own_clock_stops_once_it_s_forgotten_all(
    chain: LocalChain, served: Served
) -> None:
    """A submission finished (here expired: its deadline a block on, too soon to be funded and
    sent) is forgotten once its retention is past, and the relay's own clock then stops: no
    alarm, nothing to forget."""
    async with served.interface() as http:
        price = await quote(http, served.chain_id, authority=fresh_client("anyone").address)
        taken = await submit_paying(http, chain, served, "expires", price, blocks_ahead=1)
        await served.start()
        status = await served.pace.until(status_of(http, taken), finished, CLOCK_SLOTS)
        stopped = await served.pace.until(
            served.clock, lambda clock: clock.stopped, served.pace.forgetting_slots + CLOCK_SLOTS
        )
        forgotten = await http.get(taken.headers["Location"])

    assert status["status"] == "expired", status
    assert stopped.stopped, stopped
    assert forgotten.status_code == 404, forgotten.text


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
        await served.on_block(await served.head())
        await served.builder.build_block()  # the accounts' funding
        before = len(served.node)
        await served.on_block(await served.head())
        sending = served.node[before:]
        await served.builder.build_block()
        before = len(served.node)
        await served.on_block(await served.head())
        reconciling = served.node[before:]
        statuses = [(await http.get(t.headers["Location"])).json()["status"] for t in taken]

    # the simulations: one eth_simulateV1 each, the three in one request
    assert calling(closing, "eth_simulateV1") == [3]
    assert calling(sending, "eth_simulateV1") == [3]
    assert calling(reconciling, "eth_getTransactionReceipt") == [3]
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
    assert padded.json()["error"]["code"] == -32601, padded.text


async def test_a_key_not_bound_is_a_configuration_error(
    chain: LocalChain, schedule: GasSchedule, workerd: str
) -> None:
    """A bundler's identity key named but not bound, or a float key that isn't one, is the
    configuration's mistake: an internal error (500, never retried), naming the binding and
    never showing a key, before the Worker reads the chain."""
    payee = to_checksum_address(await chain.account(PAYEE_ACCOUNT))
    chain_id = await chain.chain_id()
    identified = BuilderConfig(BUILDER_URL, Mode.STANDALONE, identity_key="UNBOUND_IDENTITY")
    config = replace(relay_config(Setup(), chain.url, payee, BUILDER_URL), bundlers=(identified,))
    test = {"config": msgspec.to_builtins(config)}
    hooks = f"/__test/{chain_id}"
    async with httpx.AsyncClient(base_url=workerd) as http:
        unbound = await http.post(
            f"{hooks}/configure", json={**test, "floatKey": "0x" + TEST_FLOAT.key.hex()}
        )
        no_float = await http.post(f"{hooks}/configure", json={**test, "floatKey": "0x"})

    for answer, field in ((unbound, "UNBOUND_IDENTITY"), (no_float, "FLOAT_KEY")):
        assert answer.status_code == 500, answer.text
        error = answer.json()["error"]
        assert error["code"] == -32603, answer.text
        assert error["data"] == {"reason": "unbound-key", "field": field}, answer.text
    assert TEST_FLOAT.key.hex() not in unbound.text


@pytest.mark.parametrize("written", [1.1, "1e0", "Infinity", " 1.1"], ids=repr)
async def test_a_decimal_is_written_exactly(
    chain: LocalChain, workerd: str, written: object
) -> None:
    """A multiplier written other than as a decimal string (a JSON number, an exponent,
    Infinity, a space around it) is the configuration's mistake, for both relays alike, naming
    it."""
    payee = to_checksum_address(await chain.account(PAYEE_ACCOUNT))
    raw = msgspec.to_builtins(relay_config(Setup(), chain.url, payee, BUILDER_URL))
    raw["tokens"][0]["margin_multiplier"] = written
    with pytest.raises(msgspec.ValidationError) as refused_here:
        msgspec.convert(raw, ChainConfig)
    error = ConfigError.of(refused_here.value, "chain")
    async with httpx.AsyncClient(base_url=workerd) as http:
        refused = await http.post(
            f"/__test/{await chain.chain_id()}/configure",
            json={"config": raw, "floatKey": "0x" + TEST_FLOAT.key.hex()},
        )

    field = "tokens[0].margin_multiplier"
    assert (error.reason, error.field) == (Misconfiguration.INVALID, field)
    assert refused.status_code == 500, refused.text
    assert refused.json()["error"]["data"] == {"reason": "invalid-config", "field": field}


def _token_entries(raw: dict[str, object]) -> list[dict[str, object]]:
    """The configuration's tokens, as written."""
    return [object_of(token, "a token") for token in list_of(raw["tokens"], "tokens")]


def _eth_given_pools(raw: dict[str, object]) -> str:
    tokens = _token_entries(raw)
    eth = next(i for i, t in enumerate(tokens) if t["address"] == NATIVE_ASSET)
    tokens[eth]["price"] = tokens[0]["price"]
    raw["tokens"] = tokens
    return f"tokens[{eth}].price"


def _token_without_pools(raw: dict[str, object]) -> str:
    tokens = _token_entries(raw)
    no_pools: list[object] = []
    tokens[0]["price"] = no_pools
    raw["tokens"] = tokens
    return "tokens[0].price"


def _min_fee_in_another_token(raw: dict[str, object]) -> str:
    raw["min_fee"] = {"token": UNISWAP_V3_USDC_WETH, "amount": "0.01"}
    return "min_fee.token"


@pytest.mark.parametrize(
    "mistaken", [_eth_given_pools, _token_without_pools, _min_fee_in_another_token]
)
async def test_tokens_are_checked_as_the_configuration_is_read(
    chain: LocalChain, workerd: str, mistaken: Callable[[dict[str, object]], str]
) -> None:
    """ETH given pools, a token given none, and a minimum fee in a token the chain doesn't
    accept are the configuration's mistakes as it's read, before any relay is opened (so
    preflight refuses them), for both relays alike, naming the field."""
    payee = to_checksum_address(await chain.account(PAYEE_ACCOUNT))
    built = msgspec.to_builtins(relay_config(Setup(), chain.url, payee, BUILDER_URL))
    raw = object_of(built, "a chain's configuration")
    field = mistaken(raw)
    with pytest.raises(ConfigError) as refused_here:
        msgspec.convert(raw, ChainConfig)
    async with httpx.AsyncClient(base_url=workerd) as http:
        refused = await http.post(
            f"/__test/{await chain.chain_id()}/configure",
            json={"config": raw, "floatKey": "0x" + TEST_FLOAT.key.hex()},
        )

    error = refused_here.value
    assert (error.reason, error.field) == (Misconfiguration.INVALID, field)
    assert refused.status_code == 500, refused.text
    assert refused.json()["error"]["data"] == {"reason": "invalid-config", "field": field}


async def _chunks(body: bytes) -> AsyncIterator[bytes]:
    """`body` streamed: sent chunked, with no content-length."""
    yield body


async def test_malformed_requests_are_refused_not_failed(chain: LocalChain, served: Served) -> None:
    """A body that isn't JSON, or is too large, a quantity past any range, an amount past a
    uint256's, an authorization list that's null, a token that isn't an address and hex with
    a newline after it are the client's mistakes: 400, never a 500. A token named twice is
    quoted once."""
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
    newline = submission(call, auth, payee + "\n", 1, await served.head() + 5)
    params = {"gas": hex(CLIENT_GAS), "callDataLength": hex(100)}
    submissions = f"/{served.chain_id}/v1/submissions"
    async with served.interface() as http:
        garbled = await http.post(submissions, content=b"{not json", headers=JSON)
        overflowing = await http.post(submissions, json=huge)
        too_much = await http.post(submissions, json=past)
        too_large = await http.post(submissions, json=large)
        # no content-length, and fewer characters than the limit has bytes: it's the bytes
        too_large_unannounced = await http.post(
            submissions,
            content=_chunks(b'{"pad": "' + "é".encode() * (MAX_BODY_BYTES // 2) + b'"}'),
            headers=JSON,
        )
        no_list = await http.post(submissions, json=null_list)
        trailing = await http.post(submissions, json=newline)
        trailing_token = await http.post(
            f"/{served.chain_id}/v1/quote", json={**params, "tokens": [USDC.address + "\n"]}
        )
        not_a_token = await http.post(
            f"/{served.chain_id}/v1/quote", json={**params, "tokens": ["0x1234"]}
        )
        twice = await http.post(
            f"/{served.chain_id}/v1/quote", json={**params, "tokens": [USDC.address] * 2}
        )

    for refused in (
        garbled,
        overflowing,
        too_much,
        too_large,
        no_list,
        not_a_token,
        trailing,
        trailing_token,
    ):
        assert refused.status_code == 400, refused.text
        assert refused.json()["error"]["code"] == -32602, refused.text
    for refused in (too_large, too_large_unannounced):
        assert refused.status_code == 400, refused.text
        assert refused.json()["error"]["data"] == {"field": "body"}, refused.text
    assert [q["token"] for q in twice.json()["quotes"]] == [USDC.address], twice.text


async def test_failures_are_answered_alike(served: Served) -> None:
    """Both relays answer a request they refuse with the same status, code and data, which is
    what a client tells refusals apart by, never the message: a chain not served refused
    before its request is read, its data the chain (no such path has none); a path that doesn't take the method, 405 naming the one it
    does; invalid params and an Invalid Request naming the field, as a path into the request's
    parameter, or a REST parameter as the OpenAPI document names it; JSON-RPC 2.0's
    "jsonrpc": "2.0" required."""
    c = served.chain_id
    op: dict[str, str | None] = {
        "sender": USDC.address,
        "nonce": "0x0",
        "callData": "0x",
        "signature": "0x",
        "maxFeePerGas": None,
    }
    entry_point = ENTRY_POINT_0_7
    query = {"gas": hex(CLIENT_GAS), "callDataLength": "0x0", "tokens": [USDC.address]}
    call = {"to": USDC.address, "data": "0x"}
    # a submission's shape, each case below wrong in one field alone
    auth = {"chainId": "0x1", "address": USDC.address, "nonce": "0x0", "yParity": "0x0"}
    sent_auth = {**auth, "r": "0x1", "s": "0x1"}
    sent = submission(call, sent_auth, USDC.address, 1, 1)

    def rpc_body(request: object) -> dict[str, Any]:
        return {"content": msgspec.json.encode(request), "headers": JSON}

    def method(name: str, params: list[object]) -> dict[str, Any]:
        return rpc_body({"jsonrpc": "2.0", "id": 1, "method": name, "params": params})

    rest: list[tuple[str, str, dict[str, Any], int, dict[str, object] | None]] = [
        ("GET", "/nosuchchain/v1/config", {}, 404, {"chain": "nosuchchain"}),
        ("POST", "/nosuchchain/v1/quote", {"json": query}, 404, {"chain": "nosuchchain"}),
        ("POST", "/nosuchchain/v1/config", {}, 404, {"chain": "nosuchchain"}),
        ("GET", "/99999999/v1/config", {}, 404, {"chain": "99999999"}),
        ("GET", f"/{c}/v1/nosuchpath", {}, 404, None),
        (
            "POST",
            f"/{c}/v1/quote",
            {"json": {"gas": "0x1", "tokens": [USDC.address]}},
            400,
            {"field": "call"},
        ),
        (
            "POST",
            f"/{c}/v1/quote",
            {"json": {"callDataLength": "0x0", "tokens": [USDC.address]}},
            400,
            {"field": "gas"},
        ),
        (
            "POST",
            f"/{c}/v1/quote",
            {"json": {**query, "callDataLength": "zz"}},
            400,
            {"field": "callDataLength"},
        ),
        (
            "POST",
            f"/{c}/v1/quote",
            {"json": {**query, "call": {**call, "data": "0x00"}}},
            400,
            {"field": "callDataLength"},
        ),
        (
            "POST",
            f"/{c}/v1/quote",
            {"json": {**query, "authority": USDC.address, "authorizationList": [sent_auth]}},
            400,
            {"field": "authority"},
        ),
        (
            "POST",
            f"/{c}/v1/quote",
            {"json": {**query, "deployment": {"account": USDC.address}}},
            400,
            {"field": "deployment.factory"},
        ),
        ("GET", f"/{c}/v1/submissions/0xzz", {}, 400, {"field": "id"}),
        ("GET", f"/{c}/v1/submissions/0x12", {}, 400, {"field": "id"}),
        ("GET", f"/{c}/v1/user-operations/0xzz", {}, 400, {"field": "userOpHash"}),
        (
            "POST",
            f"/{c}/v1/submissions",
            {"content": b"{x", "headers": JSON},
            400,
            {"field": "body"},
        ),
        ("POST", f"/{c}/v1/submissions", {"json": [1]}, 400, {"field": "body"}),
        (
            "POST",
            f"/{c}/v1/submissions",
            {"json": {**sent, "call": {"data": "0x"}}},
            400,
            {"field": "call.to"},
        ),
        (
            "POST",
            f"/{c}/v1/submissions",
            {"json": {**sent, "authorizationList": [5]}},
            400,
            {"field": "authorizationList[0]"},
        ),
        (
            "POST",
            f"/{c}/v1/user-operations/gas",
            {"json": {"userOperation": {**op, "sender": 5}, "entryPoint": entry_point}},
            400,
            {"field": "sender"},
        ),
        (
            "POST",
            f"/{c}/v1/user-operations/gas",
            {"json": {"userOperation": op, "entryPoint": entry_point}},
            400,
            {"field": "maxFeePerGas"},
        ),
    ]
    not_allowed = [
        ("GET", f"/{c}", "POST"),
        ("POST", f"/{c}/v1/config", "GET"),
        ("GET", f"/{c}/v1/quote", "POST"),
        ("PUT", f"/{c}/v1/submissions", "POST"),
        ("POST", "/", "GET"),
    ]
    json_rpc: list[tuple[dict[str, Any], int, dict[str, object]]] = [
        (
            rpc_body({"jsonrpc": "2.0", "id": 1, "method": 5}),
            Code.INVALID_REQUEST,
            {"field": "method"},
        ),
        (rpc_body({"jsonrpc": "2.0", "id": 1}), Code.INVALID_REQUEST, {"field": "method"}),
        (
            rpc_body({"jsonrpc": "2.0", "id": {}, "method": "eth_chainId"}),
            Code.INVALID_REQUEST,
            {"field": "id"},
        ),
        (rpc_body({"id": 1, "method": "eth_chainId"}), Code.INVALID_REQUEST, {"field": "jsonrpc"}),
        (
            rpc_body({"jsonrpc": "1.0", "id": 1, "method": "eth_chainId"}),
            Code.INVALID_REQUEST,
            {"field": "jsonrpc"},
        ),
        (
            rpc_body({"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": "x"}),
            Code.INVALID_REQUEST,
            {"field": "params"},
        ),
        (
            rpc_body([{"jsonrpc": "2.0", "id": 1, "method": "eth_chainId"}]),
            Code.INVALID_REQUEST,
            {"field": "request"},
        ),
        (method("postpaid_getStatus", [5]), Code.INVALID_PARAMS, {"field": "id"}),
        # a hash in a list isn't one, though it's written the same as text
        (method("postpaid_getStatus", [["0x" + "ab" * 32]]), Code.INVALID_PARAMS, {"field": "id"}),
        (
            method("eth_getUserOperationReceipt", [["0x" + "ab" * 32]]),
            Code.INVALID_PARAMS,
            {"field": "userOpHash"},
        ),
        (method("postpaid_getStatus", []), Code.INVALID_PARAMS, {"field": "params"}),
        (method("postpaid_sendTransaction", [5]), Code.INVALID_PARAMS, {"field": "submission"}),
        (method("postpaid_quote", [5]), Code.INVALID_PARAMS, {"field": "quote"}),
        (method("eth_getUserOperationReceipt", [5]), Code.INVALID_PARAMS, {"field": "userOpHash"}),
        (
            method("postpaid_quote", [{**query, "callDataLength": 5}]),
            Code.INVALID_PARAMS,
            {"field": "callDataLength"},
        ),
        (
            method("eth_estimateUserOperationGas", [op, entry_point]),
            Code.INVALID_PARAMS,
            {"field": "maxFeePerGas"},
        ),
        (
            method("eth_estimateUserOperationGas", [{**op, "maxFeePerGas": "0x0"}, 5]),
            Code.INVALID_PARAMS,
            {"field": "entryPoint"},
        ),
    ]
    async with served.interface() as http:
        rest_answers = [await http.request(m, path, **kw) for m, path, kw, _, _ in rest]
        allow_answers = [await http.request(m, path) for m, path, _ in not_allowed]
        rpc_answers = [await http.post(f"/{c}", **kw) for kw, _, _ in json_rpc]

    for (m, path, _, status, data), answer in zip(rest, rest_answers, strict=True):
        error = answer.json()["error"]
        assert answer.status_code == status, (m, path, answer.text)
        assert error["code"] == (Code.METHOD_NOT_FOUND if status == 404 else Code.INVALID_PARAMS)
        assert error.get("data") == data, (m, path, answer.text)
    for (m, path, allow), answer in zip(not_allowed, allow_answers, strict=True):
        assert answer.status_code == 405, (m, path, answer.text)
        assert answer.json()["error"]["code"] == Code.METHOD_NOT_FOUND, answer.text
        assert answer.headers["allow"].split(", ") == [allow, "OPTIONS"], (m, path)
    for (kw, code, data), answer in zip(json_rpc, rpc_answers, strict=True):
        body = answer.json()
        assert answer.status_code == 200, answer.text
        assert body["error"]["code"] == code, (kw["content"], answer.text)
        assert body["error"]["data"] == data, (kw["content"], answer.text)
        if code == Code.INVALID_REQUEST:
            assert body["id"] is None, answer.text


async def test_quantities_past_a_safe_integer_are_over_their_bound(
    chain: LocalChain, served: Served
) -> None:
    """A gas, a call's length or a deadline past the largest integer a float64 holds exactly
    (2**53) is past the relay's limit like any other too large: each relay says so as the other
    does, naming the limit. JSON-RPC params by name (an object) are a request without the
    method's parameters, which are by position; params neither a list nor an object, no request
    at all (JSON-RPC 2.0): each said alike."""
    payee = served.payee
    client = fresh_client("asks past a safe integer")
    call, auth = await signed_call(chain, client, payee, 1)
    past = hex(2**53)
    body = submission(call, auth, payee, 1, await served.head() + 5)
    late = {**body, "maxBlockNumber": past}
    body["gas"] = past
    async with served.interface() as http:
        terms = (await http.get(f"/{served.chain_id}/v1/config")).json()
        gas = await http.post(
            f"/{served.chain_id}/v1/quote",
            json={"gas": past, "callDataLength": "0x64", "tokens": [USDC.address]},
        )
        length = await http.post(
            f"/{served.chain_id}/v1/quote",
            json={"gas": "0x186a0", "callDataLength": past, "tokens": [USDC.address]},
        )
        submitted = await http.post(f"/{served.chain_id}/v1/submissions", json=body)
        too_late = await http.post(f"/{served.chain_id}/v1/submissions", json=late)
        not_a_list = (
            await http.post(
                f"/{served.chain_id}",
                json={"jsonrpc": "2.0", "id": 1, "method": "postpaid_getStatus", "params": {}},
            )
        ).json()
        not_structured = (
            await http.post(
                f"/{served.chain_id}",
                json={"jsonrpc": "2.0", "id": 1, "method": "postpaid_getStatus", "params": "0x"},
            )
        ).json()

    most, most_bytes = int(terms["maxGas"], 16), int(terms["maxCallDataLength"], 16)
    for answer, data in (
        (gas, {"field": "gas", "most": hex(most)}),
        (length, {"field": "callDataLength", "most": hex(most_bytes)}),
        (submitted, {"field": "gas", "most": hex(most)}),
    ):
        assert answer.json()["error"]["code"] == -32602, answer.text
        assert answer.json()["error"]["data"] == data, answer.text
    late_error = too_late.json()["error"]
    assert late_error["code"] == -32602, too_late.text
    assert set(late_error["data"]) == {"field", "most"}, too_late.text
    assert late_error["data"]["field"] == "maxBlockNumber", too_late.text
    assert (not_structured["id"], not_structured["error"]["code"]) == (None, -32600), not_structured
    assert not_a_list["id"] == 1, not_a_list
    assert not_a_list["error"]["code"] == -32602, not_a_list
    assert not_a_list["error"]["data"] == {"field": "params"}, not_a_list


async def test_the_most_gas_bounds_the_whole_transaction(chain: LocalChain, served: Served) -> None:
    """maxGas is the most gas the whole transaction may use, the runner's own included: a call
    given all of it fits on its own but not in its transaction, so its quote, its x402
    challenge and its submission are each refused, saying what the transaction needs."""
    payee = served.payee
    client = fresh_client("asks for all the gas")
    call, auth = await signed_call(chain, client, payee, 1)
    body = submission(call, auth, payee, 1, await served.head() + 5)
    body["gas"] = hex(MAX_GAS)
    async with served.interface() as http:
        most = int((await http.get(f"/{served.chain_id}/v1/config")).json()["maxGas"], 16)
        quoted = await quoting(http, served.chain_id, gas=most, call_data_length=100)
        challenged_ = await http.post(
            f"/{served.chain_id}/v1/x402", json={"call": call, "gas": hex(most)}
        )
        submitted = await http.post(f"/{served.chain_id}/v1/submissions", json=body)

    assert most == MAX_GAS
    for answer in (quoted, challenged_, submitted):
        assert answer.status_code == 400, answer.text
        error = answer.json()["error"]
        assert error["code"] == -32602, error
        assert set(error["data"]) == {"field", "most", "needs"}, error
        assert (error["data"]["field"], error["data"]["most"]) == ("gas", hex(most)), error
        assert int(error["data"]["needs"], 16) > most, error


async def test_authorizations_out_of_range_are_refused(chain: LocalChain, served: Served) -> None:
    """More authorizations than allowed, a yParity that isn't 0 or 1, a high s, and a nonce past
    the largest integer a float64 holds exactly (2**53 - 1: no account's nonce gets there):
    each refused before any work."""
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
    nonce = submission(call, {**auth, "nonce": hex(2**53)}, payee, 1, deadline)
    async with served.interface() as http:
        answers = [
            await http.post(f"/{served.chain_id}/v1/submissions", json=body)
            for body in (two, parity, high_s, nonce)
        ]

    for answer in answers:
        assert answer.status_code == 400, answer.text
        assert answer.json()["error"]["data"]["field"] == "authorizationList", answer.text
    # more than allowed: the most named
    assert answers[0].json()["error"]["data"]["most"] == "0x1", answers[0].text


@pytest.mark.parametrize("served", [Setup(accounts=1, max_pending=1)], indirect=True, ids=str)
async def test_a_submission_already_sent_isnt_evicted(chain: LocalChain, served: Served) -> None:
    """Once its transaction is out, a submission may land: a more profitable one is refused
    rather than evicting it, and the one sent lands."""
    async with served.interface() as http:
        price = await quote(http, served.chain_id, authority=fresh_client("anyone").address)
        sent = await submit_paying(http, chain, served, "already sent", price)
        await served.send()
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
