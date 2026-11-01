"""make NETWORK-verify's run, on anvil against both relays: the client funded by the payee, a
payment a unit short refused, the paid one landed, and one over x402 landed too; on a network
whose money is real, capped, and on a test network, what the payee lacks bought through
Uniswap's pool."""

import httpx
import pytest

from bruno.accounts import derive
from bruno.chain import Chain
from bruno.config import Hop, PoolKind
from bruno.server.front import chain_path
from bruno.tests.live import WalletShortError
from bruno.tests.localchain import (
    SWAP_ROUTER,
    TEST_FLOAT,
    UNISWAP_V3_USDC_WETH,
    USDC,
    USDC_DOMAIN,
    WETH,
    LocalChain,
)
from bruno.tests.served import PAYEE_KEY, Served
from bruno.tests.verify import Capped, Swapped, VerifyError, verify

SWAPPED = Swapped(Hop(PoolKind.UNISWAP_V3, UNISWAP_V3_USDC_WETH), WETH.address, SWAP_ROUTER)


async def test_verify_capped(chain: LocalChain, served: Served) -> None:
    """Real money: the payee funds the client from what it holds, the run under its cap."""
    await chain.mint(USDC, served.payee, 10 * 10**6)

    async with httpx.AsyncClient() as nodes, served.interface() as http:
        live = Chain(nodes, served.chain_id, (chain.url,))
        verified = await verify(
            http,
            f"/{chain_path(served.chain_id)}",
            live,
            derive(TEST_FLOAT, 2000),
            PAYEE_KEY,
            USDC.address,
            USDC_DOMAIN,
            served.land,
            30,
            Capped(10 * 10**6),
        )

    assert verified.paid > 0
    assert verified.x402_transaction_hash is not None


async def test_verify_swapped(chain: LocalChain, served: Served) -> None:
    """A test network: the payee holding none of the token buys what the client lacks with ETH
    through the pool, and the run lands."""
    await chain.set_token_balance(USDC, served.payee, 0)

    async with httpx.AsyncClient() as nodes, served.interface() as http:
        live = Chain(nodes, served.chain_id, (chain.url,))
        verified = await verify(
            http,
            f"/{chain_path(served.chain_id)}",
            live,
            derive(TEST_FLOAT, 2001),
            PAYEE_KEY,
            USDC.address,
            USDC_DOMAIN,
            served.land,
            30,
            SWAPPED,
        )

    assert verified.paid > 0
    assert verified.x402_transaction_hash is not None


async def test_verify_refuses_over_its_cap(chain: LocalChain, served: Served) -> None:
    """A run the relay quotes over the cap is refused before anything is sent: the payee's
    nonce unmoved."""
    await chain.mint(USDC, served.payee, 10 * 10**6)

    async with httpx.AsyncClient() as nodes, served.interface() as http:
        live = Chain(nodes, served.chain_id, (chain.url,))
        before = await live.nonce(PAYEE_KEY.address)
        with pytest.raises(VerifyError):
            await verify(
                http,
                f"/{chain_path(served.chain_id)}",
                live,
                derive(TEST_FLOAT, 2002),
                PAYEE_KEY,
                USDC.address,
                USDC_DOMAIN,
                served.land,
                30,
                Capped(1),
            )
        assert await live.nonce(PAYEE_KEY.address) == before


async def test_verify_swapped_payee_short_of_eth(chain: LocalChain, served: Served) -> None:
    """A test network's payee without the ETH to buy what it lacks: WalletShortError naming the
    payee, before it sends anything."""
    await chain.set_token_balance(USDC, served.payee, 0)
    eth = await chain.eth_balance(served.payee)
    await chain.set_eth_balance(served.payee, 0)

    try:
        async with httpx.AsyncClient() as nodes, served.interface() as http:
            live = Chain(nodes, served.chain_id, (chain.url,))
            before = await live.nonce(PAYEE_KEY.address)
            with pytest.raises(WalletShortError) as short:
                await verify(
                    http,
                    f"/{chain_path(served.chain_id)}",
                    live,
                    derive(TEST_FLOAT, 2003),
                    PAYEE_KEY,
                    USDC.address,
                    USDC_DOMAIN,
                    served.land,
                    30,
                    SWAPPED,
                )
            assert short.value.address == PAYEE_KEY.address
            assert short.value.held == 0
            assert await live.nonce(PAYEE_KEY.address) == before
    finally:
        await chain.set_eth_balance(served.payee, eth)
