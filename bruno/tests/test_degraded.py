"""A relay left alone degrades, never dies: what it measures of the chain (bruno.measurements)
taken again once stale, and anything that fails taking out only what depends on it. On anvil,
against both relays."""

import asyncio
import time
from typing import Any

import httpx
import msgspec
import pytest

from bruno.builders import BUILDER_TIMEOUT_SECONDS
from bruno.config import ChainConfig
from bruno.measurements import UnavailableError
from bruno.preflight import check
from bruno.relay import open_relay
from bruno.server.front import chain_path
from bruno.tests.calibur import fresh_client
from bruno.tests.deployments import CLIENT_GAS
from bruno.tests.interface import quote, quoting, submit_paying
from bruno.tests.localchain import (
    REVERT_ALWAYS,
    TEST_FLOAT,
    USDC,
    USDT,
    LocalChain,
    without_deployer,
)
from bruno.tests.served import (
    BUILDER_URL,
    FAST_BATCH_SECONDS,
    FAST_SLOT_SECONDS,
    FAST_SLOTS,
    PAGE,
    PAYEE_ACCOUNT,
    Served,
    Setup,
    relay_config,
)

# the call quoted: its size
CALL_BYTES = 100
QUOTE = {"gas": hex(CLIENT_GAS), "callBytes": hex(CALL_BYTES)}
# quotes enough that a node chosen at random for each request is asked at least once
QUOTES = 20


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
            http, chain_path(served.chain_id), USDC.address, call_bytes=CALL_BYTES
        )
        x402 = await http.post(f"/{chain_path(served.chain_id)}/v1/x402")
        listed = await problems(http)

    assert refused.status_code == 503, refused.text
    assert refused.json()["error"]["code"] == -32015
    assert refused.json()["error"]["data"] == {"category": "operator", "reason": "no-deployer"}
    assert x402.status_code == 503, x402.text
    assert any("deployer" in p for p in listed), listed


async def test_a_token_that_cant_be_measured_is_suspended(
    chain: LocalChain, served: Served
) -> None:
    """USDT's code refusing every call: it's suspended (refused, why), USDC still quoted."""
    await chain.rpc("anvil_setCode", [USDT.address, REVERT_ALWAYS])
    async with served.interface() as http:
        suspended = await quoting(
            http, chain_path(served.chain_id), USDT.address, call_bytes=CALL_BYTES
        )
        still = await quoting(
            http, chain_path(served.chain_id), USDC.address, call_bytes=CALL_BYTES
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
            http, chain_path(served.chain_id), USDC.address, call_bytes=CALL_BYTES
        )
        await without_deployer(chain)
        await asyncio.sleep(1.5)
        after = await quoting(
            http, chain_path(served.chain_id), USDC.address, call_bytes=CALL_BYTES
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
            await relay.quote(CLIENT_GAS, 100, None, [USDC.address])
        with pytest.raises(UnavailableError, match="not measured"):  # not asked again yet
            await relay.quote(CLIENT_GAS, 100, None, [USDC.address])

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


@pytest.mark.parametrize("served", [Setup(page_node=True)], indirect=True, ids=str)
async def test_a_node_answering_a_web_page_is_passed_over(
    chain: LocalChain, served: Served
) -> None:
    """Two nodes, one answering every request with a web page: each request it gets goes on to
    the other, and the relay answers as if it had the one."""
    async with served.interface() as http:
        quotes = [
            await quoting(http, chain_path(served.chain_id), USDC.address, call_bytes=CALL_BYTES)
            for _ in range(QUOTES)
        ]

    assert all(q.status_code == 200 for q in quotes), [q.text for q in quotes]
    assert served.faults.paged > 0, served.node


async def test_a_node_behind_the_block_is_unavailable_not_a_revert(
    chain: LocalChain, served: Served
) -> None:
    """A node answering geth's "header not found" to a submission's simulation hasn't the block
    asked at: the relay is unavailable for now (503, to try again), not the call reverting."""
    async with served.interface() as http:
        price = await quote(http, served.chain_id, authority=fresh_client("anyone").address)
        served.faults.unseen_once.add("eth_call")
        answer = await submit_paying(http, chain, served, "a node behind", price)

    assert answer.status_code == 503, answer.text
    assert not served.faults.unseen_once, "no eth_call was made"


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
        warm = await quoting(http, chain_path(served.chain_id), USDC.address, call_bytes=CALL_BYTES)
        assert warm.status_code == 200, warm.text
        served.faults.garbled["eth_call"] = lambda _params, _result: "0x00"
        refused = await quoting(
            http, chain_path(served.chain_id), USDT.address, call_bytes=CALL_BYTES
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
            http, chain_path(served.chain_id), USDC.address, call_bytes=CALL_BYTES
        )
        served.faults.pages = True
        rest = await quoting(http, chain_path(served.chain_id), USDC.address, call_bytes=CALL_BYTES)
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


@pytest.mark.parametrize(
    "served",
    [Setup(batch_seconds=FAST_BATCH_SECONDS, slot_seconds=FAST_SLOT_SECONDS)],
    indirect=True,
    ids=str,
)
async def test_a_funding_no_node_took_is_sent_again(chain: LocalChain, served: Served) -> None:
    """The funding's first send answered with a web page, so no node has it: it's sent again
    each block, and the submission lands within a few slots, not after the funding's
    timeout."""
    served.faults.page_once.add("eth_sendRawTransaction")
    async with served.interface() as http:
        price = await quote(http, served.chain_id, authority=fresh_client("anyone").address)
        taken = await submit_paying(http, chain, served, "funded at the second send", price)
        await served.start()
        status: dict[str, Any] = {}
        for _ in range(FAST_SLOTS):
            await asyncio.sleep(FAST_SLOT_SECONDS)
            await served.builder.build_block()
            status = (await http.get(taken.headers["Location"])).json()
            if status["status"] != "pending":
                break

    sends = [r for r in served.node if "eth_sendRawTransaction" in r]
    assert status["status"] == "included", status
    assert len(sends) >= 2, served.node


async def test_a_builder_that_hangs_holds_a_block_only_its_timeout(
    chain: LocalChain, served: Served
) -> None:
    """A builder that takes a bundle and never answers: the block's work waits for it no longer
    than its timeout, the bundle counted as sent."""
    async with served.interface() as http:
        price = await quote(http, served.chain_id, authority=fresh_client("anyone").address)
        taken = await submit_paying(http, chain, served, "bundled to a hung builder", price)
        await served.close_batch()
        served.faults.builder_hangs = True
        started = time.monotonic()
        await served.on_block(await served.head())
        took = time.monotonic() - started
        status = (await http.get(taken.headers["Location"])).json()

    assert BUILDER_TIMEOUT_SECONDS <= took < 2 * BUILDER_TIMEOUT_SECONDS, took
    assert status["attempts"] == "0x1", status
