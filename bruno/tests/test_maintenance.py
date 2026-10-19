"""Maintaining the concurrency accounts on the local chain: topping them up to their required
funds sends only shortfalls, once; sweeping leaves them at exactly 0."""

import httpx

from bruno.accounts import derive
from bruno.chain import Chain
from bruno.forks import GasSchedule
from bruno.maintenance import monitor, replenish, sweep
from bruno.tests.localchain import TEST_FLOAT, LocalChain

FLOAT_ETH = 10**18
# how long a transaction is waited for: anvil mines it at once
TIMEOUT_SECONDS = 10
PRIORITY_FEE = 10**9


async def test_replenish_tops_up_only_shortfalls(chain: LocalChain, schedule: GasSchedule) -> None:
    await chain.set_eth_balance(TEST_FLOAT.address, FLOAT_ETH)
    partly = derive(TEST_FLOAT, 0)
    await chain.set_eth_balance(partly.address, 3 * 10**16)  # account 0 holds some already
    required = {0: 10**17, 1: 5 * 10**16, 2: 10**16}
    await chain.set_eth_balance(derive(TEST_FLOAT, 2).address, 2 * 10**16)  # account 2 enough
    async with httpx.AsyncClient() as client:
        live = Chain(client, await chain.chain_id(), (chain.url,), ((0, schedule),))
        fees = await live.fees(PRIORITY_FEE)

        topped = await replenish(live, TEST_FLOAT, required, fees, schedule, TIMEOUT_SECONDS)

        assert topped is not None
        assert topped.value == (10**17 - 3 * 10**16) + 5 * 10**16
        states = {s.index: s.balance for s in await monitor(live, TEST_FLOAT, required)}
        assert states == {0: 10**17, 1: 5 * 10**16, 2: 2 * 10**16}
        assert await replenish(live, TEST_FLOAT, required, fees, schedule, TIMEOUT_SECONDS) is None


async def test_sweep_leaves_exactly_zero(chain: LocalChain, schedule: GasSchedule) -> None:
    for i, wei in ((0, 123_456_789_012_345), (1, 10**16)):
        await chain.set_eth_balance(derive(TEST_FLOAT, i).address, wei)
    await chain.set_eth_balance(TEST_FLOAT.address, FLOAT_ETH)  # the float exists
    before = await chain.eth_balance(TEST_FLOAT.address)
    async with httpx.AsyncClient() as client:
        live = Chain(client, await chain.chain_id(), (chain.url,), ((0, schedule),))
        gas_price = (await live.fees(PRIORITY_FEE)).max_fee_per_gas

        sent = await sweep(live, TEST_FLOAT, range(3), gas_price, schedule, TIMEOUT_SECONDS)

        assert len(sent) == 2  # account 2 held nothing
        assert [s.balance for s in await monitor(live, TEST_FLOAT, range(3))] == [0, 0, 0]
        assert await chain.eth_balance(TEST_FLOAT.address) - before == sum(t.value for t in sent)
