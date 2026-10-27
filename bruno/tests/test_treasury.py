"""The treasury on anvil, against both relays: the payee's ETH forwarded to the float, its
WETH unwrapped and its stablecoins swapped through Uniswap's router when the float needs them
and the conversion is worth it, the payee keeping its reserve, and the root showing each run."""

from math import ceil

import httpx
import msgspec

from bruno.chain import Chain, lasting_fees, market_tip
from bruno.config import PoolKind
from bruno.forks import GasSchedule
from bruno.router import Step, swap_calldata
from bruno.server.front import page
from bruno.server.wire import IndexResult
from bruno.tests.funds import (
    a_gwei_base_fee,
    a_transaction,
    approve_router,
    probe,
    treasury_entry,
    weth_dust,
)
from bruno.tests.interface import origin
from bruno.tests.localchain import SWAP_ROUTER, TEST_FLOAT, USDC, USDT, WETH, LocalChain
from bruno.tests.served import Served
from bruno.treasury import BUNDLE_BLOCKS, GAS_HEADROOM

# what a payee holds of a stablecoin in the treasury's tests: 200 USD
STABLES = 200 * 10**6


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


async def test_the_treasury_keeps_a_unit_of_usdc(chain: LocalChain, served: Served) -> None:
    """The float empty, the payee holding a few USDC and its reserve: the treasury converts
    all of it but one base unit, so the payee's balance slot stays set."""
    await a_gwei_base_fee(chain)
    await approve_router(chain, served.payee)
    await chain.set_eth_balance(TEST_FLOAT.address, 0)
    await chain.mint(USDC, served.payee, 5 * 10**6)
    _, reserve = await probe(chain, served)
    await chain.set_eth_balance(served.payee, reserve)
    await served.treasury()
    await served.builder.build_block()

    assert await chain.balance_of(USDC, served.payee) == 1
