"""A relay left alone degrades, never dies: what it measures of the chain (bruno.measurements)
taken again once stale, and anything that fails taking out only what depends on it. On anvil,
against both relays."""

import asyncio

import httpx
import pytest

from bruno.config import ChainConfig
from bruno.preflight import check
from bruno.relay import UnavailableError, open_relay
from bruno.server.front import chain_path
from bruno.tests.deployments import CLIENT_GAS
from bruno.tests.localchain import REVERT_ALWAYS, TEST_FLOAT, USDC, USDT, LocalChain
from bruno.tests.served import BUILDER_URL, PAYEE_ACCOUNT, Served, Setup, relay_config
from bruno.tests.test_deployer import without_deployer

QUOTE = {"gas": hex(CLIENT_GAS), "callBytes": hex(100)}


async def quote(http: httpx.AsyncClient, served: Served, token: str) -> httpx.Response:
    path = chain_path(served.chain_id)
    return await http.get(f"/{path}/v1/quote", params={**QUOTE, "tokens": [token]})


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
        refused = await quote(http, served, USDC.address)
        x402 = await http.post(f"/{chain_path(served.chain_id)}/v1/x402")
        listed = await problems(http)

    assert refused.status_code == 503, refused.text
    assert refused.json()["error"]["code"] == -32015
    assert "deployer" in refused.json()["error"]["message"]
    assert x402.status_code == 503, x402.text
    assert any("deployer" in p for p in listed), listed


async def test_a_token_that_cant_be_measured_is_suspended(
    chain: LocalChain, served: Served
) -> None:
    """USDT's code refusing every call: it's suspended (refused, why), USDC still quoted."""
    await chain.rpc("anvil_setCode", [USDT.address, REVERT_ALWAYS])
    async with served.interface() as http:
        suspended = await quote(http, served, USDT.address)
        still = await quote(http, served, USDC.address)
        listed = await problems(http)

    assert suspended.status_code == 422, suspended.text
    assert "suspended" in suspended.json()["error"]["message"]
    assert still.status_code == 200, still.text
    assert any(USDT.address in p and "suspended" in p for p in listed), listed


@pytest.mark.parametrize("served", [Setup(measure_seconds=1)], indirect=True, ids=str)
async def test_measurements_are_taken_again_once_stale(chain: LocalChain, served: Served) -> None:
    """Measured, then the deployer gone: once the measurements are stale, the chain is found
    unavailable, without a restart."""
    async with served.interface() as http:
        before = await quote(http, served, USDC.address)
        await without_deployer(chain)
        await asyncio.sleep(1.5)
        after = await quote(http, served, USDC.address)

    assert before.status_code == 200, before.text
    assert after.status_code == 503, after.text


async def test_nodes_not_answering_leave_the_relay_unavailable(chain: LocalChain) -> None:
    """The relay opens whatever its nodes do: they not answering, it's unavailable, not dead."""
    payee = await chain.account(PAYEE_ACCOUNT)
    config: ChainConfig = relay_config(Setup(), "http://127.0.0.1:9/", payee, BUILDER_URL)
    async with httpx.AsyncClient() as client:
        relay = open_relay(client, await chain.chain_id(), config, TEST_FLOAT)
        with pytest.raises(UnavailableError, match="not measured"):
            await relay.quote(CLIENT_GAS, 100, None, [USDC.address])


async def test_preflight_finds_what_isnt_ready(chain: LocalChain) -> None:
    """Preflight, against the local chain: the deployer missing is reported, read-only (a chain
    that's unavailable has nothing more checked)."""
    await without_deployer(chain)
    payee = await chain.account(PAYEE_ACCOUNT)
    config = relay_config(Setup(), chain.url, payee, "http://127.0.0.1:9/")
    async with httpx.AsyncClient() as client:
        problems = await check(client, await chain.chain_id(), config, payee)

    assert any("deployer" in p for p in problems), problems
