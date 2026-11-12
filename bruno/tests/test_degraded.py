"""A relay left alone degrades, never dies: what it measures of the chain (bruno.measurements)
taken again once stale, and anything that fails taking out only what depends on it. On anvil,
against both relays."""

import asyncio
import time
from itertools import pairwise

import httpx
import msgspec
import pytest

from bruno.builders import BUILDER_TIMEOUT
from bruno.chain import REST, Chain, list_of, object_of
from bruno.config import ChainConfig
from bruno.measurements import UnavailableError
from bruno.preflight import check
from bruno.pricing import QuotedCall
from bruno.relay import open_relay
from bruno.server.front import chain_path
from bruno.tests.calibur import fresh_client
from bruno.tests.deployments import CLIENT_GAS
from bruno.tests.interface import quote, quoting, submit_paying
from bruno.tests.localchain import (
    ENTRY_POINT_0_7,
    REVERT_ALWAYS,
    TEST_FLOAT,
    USDC,
    USDT,
    LocalChain,
    without_deployer,
)
from bruno.tests.mockbuilder import Reply
from bruno.tests.served import (
    ANVIL_UNSEEN,
    BUILDER_URL,
    GETH_UNSEEN,
    PAGE,
    PAYEE_ACCOUNT,
    Served,
    Setup,
    relay_config,
)
from bruno.transactions import Fees, sign
from bruno.units import Millis

# the call quoted: its size
CALL_BYTES = 100
QUOTE = {"gas": hex(CLIENT_GAS), "callDataLength": hex(CALL_BYTES)}
# quotes enough that a node chosen at random for each request is asked at least once
QUOTES = 20
# how far the relay's clock and the test's may disagree about when a node was asked
CLOCK_SLACK_SECONDS = 0.05


async def problems(http: httpx.AsyncClient) -> list[str]:
    root = (await http.get("/")).json()
    found: list[str] = root["chains"][0].get("problems", [])
    return found


async def test_without_its_deployer_a_chain_is_unavailable(
    chain: LocalChain, served: Served
) -> None:
    """The deployer missing: nothing is quoted or taken (503, why), and the root says why."""
    await without_deployer(chain)
    async with served.interface() as http:
        refused = await quoting(
            http, chain_path(served.chain_id), USDC.address, call_data_length=CALL_BYTES
        )
        x402 = await http.post(f"/{chain_path(served.chain_id)}/v1/x402")
        listed = await problems(http)

    assert refused.status_code == 503, refused.text
    assert refused.json()["error"]["code"] == -32015
    assert refused.json()["error"]["data"] == {"category": "operator", "reason": "no-deployer"}
    assert x402.status_code == 503, x402.text
    assert any("deployer" in p for p in listed), listed


async def test_nodes_without_eth_simulatev1_leave_a_chain_unavailable(
    chain: LocalChain, served: Served
) -> None:
    """The nodes not having eth_simulateV1, which every simulation goes through: nothing is
    quoted or taken (503, the operator's to fix), and the root says why."""
    served.faults.missing.add("eth_simulateV1")
    async with served.interface() as http:
        refused = await quoting(
            http, chain_path(served.chain_id), USDC.address, call_data_length=CALL_BYTES
        )
        listed = await problems(http)

    assert refused.status_code == 503, refused.text
    assert refused.json()["error"]["code"] == -32015
    assert refused.json()["error"]["data"] == {"category": "operator", "reason": "no-simulation"}
    assert any("eth_simulateV1" in p for p in listed), listed


async def test_a_token_that_cant_be_measured_is_suspended(
    chain: LocalChain, served: Served
) -> None:
    """USDT's code refusing every call: it's suspended (refused, why), USDC still quoted."""
    await chain.rpc("anvil_setCode", [USDT.address, REVERT_ALWAYS])
    async with served.interface() as http:
        suspended = await quoting(
            http, chain_path(served.chain_id), USDT.address, call_data_length=CALL_BYTES
        )
        still = await quoting(
            http, chain_path(served.chain_id), USDC.address, call_data_length=CALL_BYTES
        )
        listed = await problems(http)

    assert suspended.status_code == 422, suspended.text
    assert suspended.json()["error"]["code"] == -32012, suspended.text
    assert suspended.json()["error"]["data"] == {"category": "retry", "reason": "suspended"}
    assert still.status_code == 200, still.text
    assert any(USDT.address in p and "suspended" in p for p in listed), listed


@pytest.mark.parametrize("served", [Setup(measure_seconds=1)], indirect=True, ids=str)
async def test_measurements_are_taken_again_once_stale(chain: LocalChain, served: Served) -> None:
    """Measured, then the deployer gone: once the measurements are stale, the chain is found
    unavailable, without a restart."""
    async with served.interface() as http:
        before = await quoting(
            http, chain_path(served.chain_id), USDC.address, call_data_length=CALL_BYTES
        )
        await without_deployer(chain)
        await asyncio.sleep(1.5)
        after = await quoting(
            http, chain_path(served.chain_id), USDC.address, call_data_length=CALL_BYTES
        )

    assert before.status_code == 200, before.text
    assert after.status_code == 503, after.text


async def test_nodes_not_answering_leave_the_relay_unavailable(chain: LocalChain) -> None:
    """The relay opens whatever its nodes do: they not answering, it's unavailable, not dead,
    and says so without naming them."""
    payee = await chain.account(PAYEE_ACCOUNT)
    config: ChainConfig = relay_config(Setup(), "http://127.0.0.1:9/", payee, BUILDER_URL)
    async with httpx.AsyncClient() as client:
        relay = open_relay(client, await chain.chain_id(), config, TEST_FLOAT)
        with pytest.raises(UnavailableError, match="not measured") as refused:
            await relay.quote(QuotedCall(None, 100, CLIENT_GAS), [USDC.address])
        with pytest.raises(UnavailableError, match="not measured"):  # not asked again yet
            await relay.quote(QuotedCall(None, 100, CLIENT_GAS), [USDC.address])

    assert "127.0.0.1" not in str(refused.value)  # a node's URL stays in the log


async def test_preflight_finds_what_isnt_ready(chain: LocalChain) -> None:
    """Preflight, against the local chain: the deployer missing is reported, read-only (a chain
    that's unavailable has nothing more checked)."""
    await without_deployer(chain)
    payee = await chain.account(PAYEE_ACCOUNT)
    config = relay_config(Setup(), chain.url, payee, "http://127.0.0.1:9/")
    async with httpx.AsyncClient() as client:
        problems = await check(client, await chain.chain_id(), config, payee)

    assert any("deployer" in p for p in problems), problems


async def test_preflight_finds_an_entry_point_missing(chain: LocalChain) -> None:
    """Preflight, against the local chain: an EntryPoint without code is reported, the op paying
    by its fees sent to it having nothing to pay the payee."""
    await chain.rpc("anvil_setCode", [ENTRY_POINT_0_7, "0x"])
    payee = await chain.account(PAYEE_ACCOUNT)
    config = relay_config(Setup(), chain.url, payee, "http://127.0.0.1:9/")
    async with httpx.AsyncClient() as client:
        problems = await check(client, await chain.chain_id(), config, payee)

    assert f"EntryPoint v0.7 isn't at {ENTRY_POINT_0_7}" in problems, problems
    assert not any("v0.8" in p for p in problems), problems


@pytest.mark.parametrize("served", [Setup(page_node=True)], indirect=True, ids=str)
async def test_a_node_answering_a_web_page_is_passed_over(
    chain: LocalChain, served: Served
) -> None:
    """Two nodes, one answering every request with a web page: each request it gets goes on to
    the other, and the relay answers as if it had the one."""
    async with served.interface() as http:
        quotes = [
            await quoting(
                http, chain_path(served.chain_id), USDC.address, call_data_length=CALL_BYTES
            )
            for _ in range(QUOTES)
        ]

    assert all(q.status_code == 200 for q in quotes), [q.text for q in quotes]
    assert served.faults.paged > 0, served.node


@pytest.mark.parametrize(
    "served",
    [Setup(second_node=True, second_without=("eth_simulateV1", "eth_feeHistory"))],
    indirect=True,
    ids=str,
)
async def test_a_node_is_never_asked_a_method_its_configured_without(
    chain: LocalChain, served: Served
) -> None:
    """Two nodes, the second configured without eth_simulateV1 and eth_feeHistory: it's asked
    for other methods and never for those, which go to the first (the head read whole, its
    fee history being one of its two calls)."""
    async with served.interface() as http:
        quotes = [
            await quoting(
                http, chain_path(served.chain_id), USDC.address, call_data_length=CALL_BYTES
            )
            for _ in range(QUOTES)
        ]

    asked = {method for request in served.faults.second for method in request}
    assert all(q.status_code == 200 for q in quotes), [q.text for q in quotes]
    assert asked, served.node
    assert not asked & {"eth_simulateV1", "eth_feeHistory"}, asked


@pytest.mark.parametrize("served", [Setup(second_node=True)], indirect=True, ids=str)
@pytest.mark.parametrize("fault", ["missing", "limited"])
async def test_a_call_one_node_cant_answer_is_asked_of_another(
    chain: LocalChain, served: Served, fault: str
) -> None:
    """Two nodes, the first answering one method of a request that it hasn't it (-32601) or
    that it's over its rate limit (-32005), the rest as usual: that's the node's failure, not
    the call's answer, so the other node is asked and every quote is answered."""
    faulty: set[str] = getattr(served.faults, fault)
    faulty.add("eth_feeHistory")
    async with served.interface() as http:
        quotes = [
            await quoting(
                http, chain_path(served.chain_id), USDC.address, call_data_length=CALL_BYTES
            )
            for _ in range(QUOTES)
        ]

    assert all(q.status_code == 200 for q in quotes), [q.text for q in quotes]


@pytest.mark.parametrize("served", [Setup(second_node=True)], indirect=True, ids=str)
@pytest.mark.parametrize("retry_after", ["1", None], ids=["retry-after", "no header"])
async def test_a_rate_limited_node_rests(
    chain: LocalChain, served: Served, retry_after: str | None
) -> None:
    """Two nodes, the second answering 429: after each request it's left out for its
    Retry-After (a second here), or REST without one, the first answering every quote
    meanwhile; and it's asked again once that's passed. However long the quotes take, no two
    requests reach it closer together than its rest."""
    headers = () if retry_after is None else (("Retry-After", retry_after),)
    rest = REST.millis / 1000 if retry_after is None else float(retry_after)
    served.faults.second_reply = Reply(429, headers)
    before = len(served.faults.second_at)
    async with served.interface() as http:
        quotes = [
            await quoting(
                http, chain_path(served.chain_id), USDC.address, call_data_length=CALL_BYTES
            )
            for _ in range(QUOTES)
        ]
        refused = served.faults.second_at[before:]
        again: list[httpx.Response] = []
        if retry_after is not None:
            await asyncio.sleep(rest + 0.2)
            served.faults.second_reply = None
            again = [
                await quoting(
                    http, chain_path(served.chain_id), USDC.address, call_data_length=CALL_BYTES
                )
                for _ in range(QUOTES)
            ]

    assert all(q.status_code == 200 for q in quotes + again), [q.text for q in quotes + again]
    gaps = [later - earlier for earlier, later in pairwise(refused)]
    assert refused, served.node
    assert all(gap >= rest - CLOCK_SLACK_SECONDS for gap in gaps), gaps
    if retry_after is not None:
        assert len(served.faults.second_at) > before + len(refused), served.faults.second


@pytest.mark.parametrize("served", [Setup(extra_node=True)], indirect=True, ids=str)
async def test_a_chains_extra_nodes_are_asked_beside_its_own(
    chain: LocalChain, served: Served
) -> None:
    """A chain whose `rpc` is a node answering web pages and whose `rpc_extra` is its real one
    (as a local configuration adds nodes to the default's): both are asked, and the relay
    answers from the one that can."""
    async with served.interface() as http:
        quotes = [
            await quoting(
                http, chain_path(served.chain_id), USDC.address, call_data_length=CALL_BYTES
            )
            for _ in range(QUOTES)
        ]

    assert all(q.status_code == 200 for q in quotes), [q.text for q in quotes]
    assert served.faults.paged > 0, served.node


@pytest.mark.parametrize("error", [GETH_UNSEEN, ANVIL_UNSEEN], ids=["geth", "anvil"])
async def test_a_node_behind_the_block_is_unavailable_not_a_revert(
    chain: LocalChain, served: Served, error: dict[str, object]
) -> None:
    """A node answering that it hasn't the block asked at to a submission's simulation, as geth
    or anvil says it: the relay is unavailable for now (503, to try again), not the call
    reverting."""
    async with served.interface() as http:
        price = await quote(http, served.chain_id, authority=fresh_client("anyone").address)
        served.faults.unseen_once.add("eth_simulateV1")
        served.faults.unseen_error = error
        answer = await submit_paying(http, chain, served, "a node behind", price)

    assert answer.status_code == 503, answer.text
    assert not served.faults.unseen_once, "no eth_simulateV1 was made"


# what a node answers eth_simulateV1 with validation on for a transaction a block wouldn't take
# (execution-apis' simulate errors): its sender short of its gas and value
INVALID_AS_SIGNED: dict[str, object] = {
    "code": -38014,
    "message": "insufficient funds for gas * price + value",
}


async def test_a_transaction_a_block_wouldnt_take_as_signed_sits_out(
    chain: LocalChain, served: Served
) -> None:
    """A node answering a block's simulation, as its transaction will be signed, that a block
    wouldn't take it (eth_simulateV1's validation): nothing is held against the submission. It
    isn't rejected, and nothing is sent for that block; the next block's simulation, answered,
    sends it, and it lands."""
    async with served.interface() as http:
        price = await quote(http, served.chain_id, authority=fresh_client("anyone").address)
        taken = await submit_paying(http, chain, served, "invalid as signed once", price)
        await served.close_batch()
        assert await served.on_block(await served.head()), "a funding sent"
        await served.builder.build_block()  # the funding's
        served.faults.unseen_once.add("eth_simulateV1")
        served.faults.unseen_error = INVALID_AS_SIGNED
        await served.on_block(await served.head())
        assert not served.faults.unseen_once, "no simulation as signed was made"
        skipped = await served.builder.build_block()
        await served.on_block(await served.head())
        held = (await http.get(taken.headers["Location"])).json()
        await served.land()
        status = (await http.get(taken.headers["Location"])).json()

    assert skipped.transactions == (), skipped
    assert held["status"] == "pending", held
    assert status["status"] == "included", status


def padded_status(receipt: object) -> object:
    """A receipt as a node may answer it, its status with a leading zero ("0x01")."""
    if receipt is None:
        return None
    raw = msgspec.convert(receipt, dict[str, object])
    return {**raw, "status": "0x0" + str(raw["status"])[2:]}


async def test_a_status_with_a_leading_zero_is_a_success(chain: LocalChain, served: Served) -> None:
    """A node answering a receipt's status as "0x01" (a quantity, its leading zero let through):
    the submission is included, its transaction a success."""
    async with served.interface() as http:
        price = await quote(http, served.chain_id, authority=fresh_client("padded").address)
        taken = await submit_paying(http, chain, served, "a padded status", price)
        assert taken.status_code == 202, taken.text
        served.faults.garbled["eth_getTransactionReceipt"] = lambda _, r: padded_status(r)
        await served.land()
        status = (await http.get(taken.headers["Location"])).json()
        del served.faults.garbled["eth_getTransactionReceipt"]

    assert status["status"] == "included", status


async def test_a_null_code_is_the_nodes_failure(chain: LocalChain, served: Served) -> None:
    """A node answering eth_getCode with null, as no account's code is: the submission is refused
    as the node's failure (503, to try again), never read as code; taken once the node answers
    properly."""
    async with served.interface() as http:
        price = await quote(http, served.chain_id, authority=fresh_client("null code").address)
        served.faults.garbled["eth_getCode"] = lambda _params, _result: None
        refused = await submit_paying(http, chain, served, "a null code", price)
        del served.faults.garbled["eth_getCode"]
        taken = await submit_paying(http, chain, served, "a null code, then code", price)

    assert refused.status_code == 503, refused.text
    assert refused.json()["error"]["data"] == {"category": "retry", "reason": "node-failed"}
    assert taken.status_code == 202, taken.text


async def test_a_malformed_answer_for_one_holds_up_only_it(
    chain: LocalChain, served: Served
) -> None:
    """Two submissions taken, the node answering one's code with null as their batch is planned:
    that one sits out, still pending, never rejected; the other lands."""
    async with served.interface() as http:
        price = await quote(http, served.chain_id, authority=fresh_client("anyone").address)
        held = await submit_paying(http, chain, served, "held up by its node", price)
        lands = await submit_paying(http, chain, served, "lands beside it", price)
        assert held.status_code == lands.status_code == 202, (held.text, lands.text)
        account = fresh_client("held up by its node").address.lower()

        def null_code(params: object, result: object) -> object:
            is_its = isinstance(params, list) and str(params[0]).lower() == account
            return None if is_its else result

        served.faults.garbled["eth_getCode"] = null_code
        await served.land()
        del served.faults.garbled["eth_getCode"]
        statuses = [(await http.get(a.headers["Location"])).json() for a in (held, lands)]

    assert statuses[0]["status"] == "pending", statuses
    assert statuses[1]["status"] == "included", statuses


async def test_a_short_pool_answer_is_the_nodes_failure(chain: LocalChain, served: Served) -> None:
    """A node answering a token's pools with a byte ("0x00", no token0 nor price in it): its
    quote is the node's failure (503, to try again), not the pool read as empty."""
    async with served.interface() as http:
        warm = await quoting(
            http, chain_path(served.chain_id), USDC.address, call_data_length=CALL_BYTES
        )
        assert warm.status_code == 200, warm.text
        served.faults.garbled["eth_call"] = lambda _params, _result: "0x00"
        refused = await quoting(
            http, chain_path(served.chain_id), USDT.address, call_data_length=CALL_BYTES
        )
        del served.faults.garbled["eth_call"]

    assert refused.status_code == 503, refused.text
    assert refused.json()["error"]["data"] == {"category": "retry", "reason": "node-failed"}


async def test_a_node_answering_a_web_page_has_failed(chain: LocalChain, served: Served) -> None:
    """A node answering a web page (a proxy's error, say) has failed, as one not answering
    has: a quote is unavailable for now (503, to try again), over REST and JSON-RPC alike,
    never the page."""
    request = {"jsonrpc": "2.0", "id": 1, "method": "postpaid_quote"}
    async with served.interface() as http:
        before = await quoting(
            http, chain_path(served.chain_id), USDC.address, call_data_length=CALL_BYTES
        )
        served.faults.pages = True
        rest = await quoting(
            http, chain_path(served.chain_id), USDC.address, call_data_length=CALL_BYTES
        )
        rpc = await http.post(
            f"/{chain_path(served.chain_id)}",
            json={**request, "params": [{**QUOTE, "tokens": [USDC.address]}]},
        )

    assert before.status_code == 200, before.text
    assert rest.status_code == 503, rest.text
    failed = {"category": "retry", "reason": "node-failed"}
    for error in (rest.json()["error"], rpc.json()["error"]):
        assert (error["code"], error["data"]) == (-32015, failed), error
    assert PAGE.decode() not in rest.text + rpc.text


async def test_a_funding_whose_bundle_misses_blocks_lands_at_the_float_s_one_nonce(
    chain: LocalChain, served: Served
) -> None:
    """The funding is a bundle of its own for the next block. Blocks built without it (by
    another builder): it's signed again for each next block, and lands once one takes it,
    then the submission; the float's nonce used once."""
    async with served.interface() as http:
        price = await quote(http, served.chain_id, authority=fresh_client("anyone").address)
        float_nonce = await chain.nonce(TEST_FLOAT.address)
        taken = await submit_paying(http, chain, served, "funded at the third block", price)
        await served.close_batch()
        missed: list[bool] = []
        for _ in range(2):
            await chain.rpc("evm_mine", [])
            missed.append(await served.on_block(await served.head()))
        waiting = (await http.get(taken.headers["Location"])).json()
        await served.builder.build_block()
        funding_left = await served.on_block(await served.head())
        await served.builder.build_block()
        await served.on_block(await served.head())
        landed = (await http.get(taken.headers["Location"])).json()

    assert missed == [True, True]
    assert waiting["status"] == "pending"
    assert waiting["attempts"] == "0x0"
    assert not funding_left
    assert landed["status"] == "included"
    assert await chain.nonce(TEST_FLOAT.address) == float_nonce + 1


async def test_a_builder_that_hangs_holds_a_block_only_its_timeout(
    chain: LocalChain, served: Served
) -> None:
    """A builder that takes a bundle and never answers: the block's work waits for it no longer
    than its timeout, the bundle counted as sent."""
    async with served.interface() as http:
        price = await quote(http, served.chain_id, authority=fresh_client("anyone").address)
        taken = await submit_paying(http, chain, served, "bundled to a hung builder", price)
        await served.close_batch()
        await served.on_block(await served.head())
        await served.builder.build_block()  # its account's funding
        served.faults.builder_hangs = True
        started = time.monotonic()
        await served.on_block(await served.head())
        took = time.monotonic() - started
        status = (await http.get(taken.headers["Location"])).json()

    assert BUILDER_TIMEOUT.seconds <= took < 2 * BUILDER_TIMEOUT.seconds, took
    assert status["attempts"] == "0x1", status


# the reads a node behind answers from its own head, their block the second parameter
_STATE_READS = ("eth_getTransactionCount", "eth_getBalance", "eth_getCode")


def _behind(call: dict[str, object], head: int) -> dict[str, object] | None:
    """What a node whose head is `head` answers `call` without reading the chain: its head, no
    receipt for a later block's transaction, and that it hasn't a block past its head. None
    where it reads the chain, `call` changed to read it as of its head."""
    answer: dict[str, object] = {"jsonrpc": "2.0", "id": call["id"]}
    params = list_of(call["params"], "params")
    if call["method"] == "eth_blockNumber":
        return {**answer, "result": hex(head)}
    if call["method"] == "eth_getTransactionReceipt":
        return {**answer, "result": None}
    if call["method"] in _STATE_READS:
        block = params[1]
        if block != "latest" and int(str(block), 16) > head:
            return {**answer, "error": GETH_UNSEEN}
        call["params"] = [params[0], hex(head)]
    return None


def two_nodes(chain: LocalChain, head: int) -> httpx.MockTransport:
    """Two nodes of the local chain: fresh.test, which is it, and behind.test, which is it as
    of block `head` (`_behind`)."""

    async def handle(request: httpx.Request) -> httpx.Response:
        call = object_of(msgspec.json.decode(request.content), "a request")
        answered = _behind(call, head) if request.url.host == "behind.test" else None
        if answered is not None:
            return httpx.Response(200, json=answered)
        async with httpx.AsyncClient() as anvil:
            forwarded = await anvil.post(chain.url, json=call)
        return httpx.Response(200, content=forwarded.content)

    return httpx.MockTransport(handle)


NODES = ("http://fresh.test/", "http://behind.test/")
# a plain transfer's gas
TRANSFER_GAS = 21_000


async def test_a_read_after_a_send_isnt_answered_by_a_node_behind(chain: LocalChain) -> None:
    """Two nodes, one still at the block before a transaction landed (answering "latest" from
    there): once Chain.send has the transaction's receipt, its sender's nonce reads as it is
    after it, however often it's asked, never as the node behind has it. As the live tooling
    met on Sepolia (2026-11-12): a nonce read from a node four blocks behind, and the next
    transaction signed with it refused as too low."""
    sender = fresh_client("a sender")
    await chain.rpc("anvil_setBalance", [sender.address, hex(10**18)])
    head = int(await chain.rpc("eth_blockNumber", []), 16)
    base_fee = await chain.base_fee()
    fees = Fees(max_fee_per_gas=2 * base_fee + 1, max_priority_fee_per_gas=1)
    async with httpx.AsyncClient(transport=two_nodes(chain, head)) as client:
        live = Chain(client, await chain.chain_id(), NODES, poll=Millis(20))
        tx = sign(sender, live.chain_id, 0, fees, TRANSFER_GAS, sender.address, b"", 1)
        await live.send(tx, "a transfer", 10)
        nonces = [await live.nonce(sender.address) for _ in range(QUOTES)]

    assert nonces == [1] * QUOTES


async def test_a_seen_block_the_chain_no_longer_has_is_given_up(chain: LocalChain) -> None:
    """A chain whose head was read, then that block gone (a reorganisation; here the local
    chain reverted to before it): a read waits for a node to have that height again, two slots,
    then takes the chain as the nodes have it, and is answered from its head."""
    account = fresh_client("an account").address
    head = int(await chain.rpc("eth_blockNumber", []), 16)
    snapshot = await chain.rpc("evm_snapshot", [])
    await chain.rpc("anvil_mine", ["0x1"])
    async with httpx.AsyncClient(transport=two_nodes(chain, head + 1)) as client:
        live = Chain(client, await chain.chain_id(), NODES[:1], poll=Millis(20))
        assert await live.head() == head + 1
        await chain.rpc("evm_revert", [snapshot])
        nonce = await live.nonce(account)

    assert (nonce, live.seen) == (0, head)
