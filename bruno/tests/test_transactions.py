"""Transactions signed here, as a node takes them."""

import pytest
from eth_account import Account
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from bruno.accounts import derive
from bruno.forks import GasSchedule
from bruno.tests.localchain import CALIBUR, TEST_FLOAT, LocalChain
from bruno.transactions import decoded, sign_drain

WALLET = Account.from_key(keccak(text="a wallet signing its own transactions"))


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


@pytest.mark.parametrize("delegating", [False, True], ids=["type 2", "type 4"])
async def test_a_wallets_transaction_is_read_back_as_the_node_has_it(
    chain: LocalChain, delegating: bool
) -> None:
    """A transaction a wallet signed (eth-account's encoding), with an access list, and one
    delegating the wallet's account: what the relay reads back from its bytes is what the node
    says of it once it has landed, its sender, hash and every field."""
    await chain.set_eth_balance(WALLET.address, 10**18)
    target = await chain.account(3)
    tx = {
        "type": 2,
        "chainId": await chain.chain_id(),
        "nonce": 0,
        "to": target,
        "data": "0xabcdef",
        "value": 12_345,
        "gas": 200_000,
        "maxFeePerGas": 2 * await chain.base_fee() + 10**9,
        "maxPriorityFeePerGas": 10**9,
        "accessList": [{"address": target, "storageKeys": ["0x" + (7).to_bytes(32).hex()]}],
    }
    if delegating:
        # sent by the wallet itself, so the authorization's nonce is the one after its own
        signing = {"chainId": tx["chainId"], "address": CALIBUR, "nonce": 1}
        tx |= {"type": 4, "authorizationList": [Account.sign_authorization(signing, WALLET.key)]}
    raw = bytes(Account.sign_transaction(tx, WALLET.key).raw_transaction)

    read = decoded(raw)
    receipt = await chain.submit(raw)
    landed = await chain.rpc("eth_getTransactionByHash", [receipt["transactionHash"]])

    assert receipt["status"] == "0x1"
    assert "0x" + read.signed.hash.hex() == landed["hash"]
    assert read.signed.sender == to_checksum_address(landed["from"]) == WALLET.address
    assert read.to == to_checksum_address(landed["to"])
    assert (read.signed.nonce, read.signed.gas) == (
        int(landed["nonce"], 16),
        int(landed["gas"], 16),
    )
    assert read.signed.max_fee_per_gas == int(landed["maxFeePerGas"], 16)
    assert read.max_priority_fee_per_gas == int(landed["maxPriorityFeePerGas"], 16)
    assert (read.signed.value, read.chain_id) == (
        int(landed["value"], 16),
        int(landed["chainId"], 16),
    )
    assert "0x" + read.data.hex() == landed["input"]
    assert [(a.address, ["0x" + k.hex() for k in a.storage_keys]) for a in read.access_list] == [
        (to_checksum_address(a["address"]), a["storageKeys"]) for a in landed["accessList"]
    ]
    assert [to_checksum_address(a.authority) for a in read.authorizations] == (
        [WALLET.address] if delegating else []
    )
    if delegating:
        assert await chain.code(WALLET.address) == "0xef0100" + CALIBUR[2:].lower()
