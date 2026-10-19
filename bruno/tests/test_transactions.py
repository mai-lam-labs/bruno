"""Transactions signed here, as a node takes them."""

from bruno.accounts import derive
from bruno.forks import GasSchedule
from bruno.tests.localchain import TEST_FLOAT, LocalChain
from bruno.transactions import sign_drain


async def test_drain_leaves_exactly_zero(chain: LocalChain, schedule: GasSchedule) -> None:
    """A type-1 drain pays exactly the transfer's gas (the fork's) x its gas price and sends
    the rest: the account ends at 0, whatever the base fee under that price."""
    holder = derive(TEST_FLOAT, 200)
    balance = 123_456_789_012_345
    await chain.set_eth_balance(holder.address, balance)
    await chain.set_eth_balance(TEST_FLOAT.address, 10**18)  # the recipient exists
    gas_price = 2 * await chain.base_fee() + 1
    before = await chain.eth_balance(TEST_FLOAT.address)
    gas = schedule.transfer(new_recipient=False)
    tx = sign_drain(holder, await chain.chain_id(), 0, balance, gas_price, TEST_FLOAT.address, gas)
    assert tx is not None

    receipt = await chain.submit(tx.raw)

    assert receipt["status"] == "0x1"
    assert await chain.eth_balance(holder.address) == 0
    assert await chain.eth_balance(TEST_FLOAT.address) - before == tx.value
