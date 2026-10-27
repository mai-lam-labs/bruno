"""make NETWORK-verify's run, on anvil against both relays: the client funded by the payee, a
payment a unit short refused, the paid one landed, and one over x402 landed too."""

import httpx

from bruno.chain import Chain
from bruno.forks import GasSchedule
from bruno.server.front import chain_path
from bruno.tests.localchain import TEST_FLOAT, USDC, USDC_DOMAIN, LocalChain
from bruno.tests.served import PAYEE_KEY, Served
from bruno.tests.verify import verify


async def test_verify(chain: LocalChain, served: Served, schedule: GasSchedule) -> None:
    await chain.mint(USDC, served.payee, 10 * 10**6)

    async with httpx.AsyncClient() as nodes, served.interface() as http:
        live = Chain(nodes, served.chain_id, (chain.url,))
        verified = await verify(
            http,
            f"/{chain_path(served.chain_id)}",
            live,
            TEST_FLOAT,
            PAYEE_KEY,
            USDC.address,
            USDC_DOMAIN,
            served.land,
            30,
            10 * 10**6,
        )

    assert verified.paid > 0
    assert verified.x402_transaction_hash is not None
