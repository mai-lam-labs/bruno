"""The mock builder's rule, rbuilder's (mockbuilder.py cites it), on plain transactions:
which of a bundle's transactions a block includes, and when the bundle fails whole."""

import pytest
from eth_account.signers.local import LocalAccount
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from bruno.accounts import derive
from bruno.bundle import bundle_params
from bruno.forks import GasSchedule
from bruno.tests.localchain import REVERT_ALWAYS, TEST_FLOAT, LocalChain
from bruno.tests.mockbuilder import MockBuilder, MockBuilderError
from bruno.transactions import Fees, SignedTransaction, sign

REVERTER = to_checksum_address(keccak(text="reverter")[-20:])
# where the test transfers go: anvil's dev account #9, which exists, so a transfer to it
# creates nothing (a new account costs 183,600 more under Amsterdam)
SINK_ACCOUNT = 9
# a call that reverts at once: ample for PUSH0 PUSH0 REVERT
REVERTING_GAS = 30_000


async def senders(chain: LocalChain, n: int) -> list[LocalAccount]:
    """`n` funded accounts (derived from the test float, from index 100: not ones the bundle
    tests use)."""
    accounts = [derive(TEST_FLOAT, 100 + i) for i in range(n)]
    for a in accounts:
        await chain.set_eth_balance(a.address, 10**18)
    return accounts


async def fees(chain: LocalChain) -> Fees:
    base = await chain.base_fee()
    return Fees(max_fee_per_gas=2 * base + 10**9, max_priority_fee_per_gas=10**9)


async def pays(
    chain: LocalChain, schedule: GasSchedule, sender: LocalAccount, nonce_offset: int = 0
) -> SignedTransaction:
    """A transfer of 1 wei to the sink; `nonce_offset` ahead of the sender's nonce: invalid."""
    nonce = await chain.nonce(sender.address) + nonce_offset
    sink = await chain.account(SINK_ACCOUNT)
    gas = schedule.transfer(new_recipient=False)
    return sign(sender, await chain.chain_id(), nonce, await fees(chain), gas, sink, b"", 1)


async def reverts(chain: LocalChain, sender: LocalAccount) -> SignedTransaction:
    nonce = await chain.nonce(sender.address)
    return sign(
        sender, await chain.chain_id(), nonce, await fees(chain), REVERTING_GAS, REVERTER, b""
    )


def params(
    block: int, first: SignedTransaction, rest: list[SignedTransaction]
) -> dict[str, object]:
    """A bundle as the service sends one: `first` not droppable, `rest` droppable."""
    return bundle_params(block, first, rest)


async def next_block(chain: LocalChain) -> int:
    return int((await chain.rpc("eth_blockNumber", [])), 16) + 1


@pytest.fixture
async def builder(chain: LocalChain) -> MockBuilder:
    await chain.rpc("anvil_setCode", [REVERTER, REVERT_ALWAYS])
    return MockBuilder(chain)


async def test_droppable_failures_are_left_out(
    chain: LocalChain, builder: MockBuilder, schedule: GasSchedule
) -> None:
    """A droppable transaction that reverts, or is invalid, is left out, costing its sender
    nothing; the rest of the bundle lands, in one block."""
    a, b, c, d = await senders(chain, 4)
    first, reverted, invalid, last = (
        await pays(chain, schedule, a),
        await reverts(chain, b),
        await pays(chain, schedule, c, nonce_offset=1),
        await pays(chain, schedule, d),
    )
    await builder.send_bundle(params(await next_block(chain), first, [reverted, invalid, last]))

    block = await builder.build_block()

    assert block.transactions == (first.hash, last.hash)
    assert block.bundles[0].dropped == [reverted.hash, invalid.hash]
    assert await chain.nonce(b.address) == 0
    assert await chain.eth_balance(b.address) == 10**18
    mined = await chain.rpc("eth_getBlockByNumber", [hex(block.number), False])
    assert [bytes.fromhex(h[2:]) for h in mined["transactions"]] == [first.hash, last.hash]


@pytest.mark.parametrize("failure", ["reverts", "invalid"])
async def test_undroppable_failure_fails_the_bundle(
    chain: LocalChain, builder: MockBuilder, schedule: GasSchedule, failure: str
) -> None:
    """A transaction that isn't droppable failing fails the whole bundle: none of it lands."""
    a, b = await senders(chain, 2)
    first = await reverts(chain, a) if failure == "reverts" else await pays(chain, schedule, a, 1)
    await builder.send_bundle(
        params(await next_block(chain), first, [await pays(chain, schedule, b)])
    )

    block = await builder.build_block()

    assert block.transactions == ()
    assert block.bundles[0].error == (
        "TransactionReverted" if failure == "reverts" else "InvalidTransaction"
    )
    assert await chain.nonce(b.address) == 0


async def test_all_dropped_is_an_empty_bundle(
    chain: LocalChain, builder: MockBuilder, schedule: GasSchedule
) -> None:
    """A bundle left with nothing to include fails (EmptyBundle)."""
    (a,) = await senders(chain, 1)
    reverted = await reverts(chain, a)
    await builder.send_bundle(bundle_params(await next_block(chain), None, [reverted]))

    block = await builder.build_block()

    assert block.transactions == ()
    assert block.bundles[0].error == "EmptyBundle"


async def test_bundle_for_another_block_waits(
    chain: LocalChain, builder: MockBuilder, schedule: GasSchedule
) -> None:
    """A bundle lands only in the block it names."""
    (a,) = await senders(chain, 1)
    tx = await pays(chain, schedule, a)
    later = await next_block(chain) + 1
    await builder.send_bundle(bundle_params(later, tx, []))

    assert (await builder.build_block()).transactions == ()
    assert (await builder.build_block()).transactions == (tx.hash,)


async def test_v1_bundle_with_dropping_is_rejected(
    chain: LocalChain, builder: MockBuilder, schedule: GasSchedule
) -> None:
    (a, b) = await senders(chain, 2)
    bundle = params(
        await next_block(chain), await pays(chain, schedule, a), [await pays(chain, schedule, b)]
    )
    with pytest.raises(MockBuilderError):
        await builder.send_bundle({**bundle, "version": "v1"})
