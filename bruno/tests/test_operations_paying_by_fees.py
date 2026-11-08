"""An op paying by its fees, taken, whose block differs from its simulation: against both
relays, what the relay's transaction does in the block it's built for. Taken and sent, then
the chain changed before the block: the op landed by another bundler first, or its account
drained of the ETH its prefund needs, and nothing of the relay's lands, nothing spent; its
account drained of the token its call sends, and the op lands failed, the payee paid its gas
by the EntryPoint all the same, more than the relay's transaction cost."""

from typing import Any

from eth_abi.abi import encode

from bruno.abi import selector
from bruno.tests import kernel
from bruno.tests.calibur import fresh_client
from bruno.tests.interface import rpc
from bruno.tests.localchain import ENTRY_POINT_0_7, USDC, LocalChain
from bruno.tests.served import Served
from bruno.tests.test_user_operations import OP, fee_op, packed

# what the op's account holds to pay its fees
ACCOUNT_ETH = 10**17
HANDLE_OPS = selector(f"handleOps({OP}[],address)")


async def taken(
    chain: LocalChain, http: Any, served: Served, what: str
) -> tuple[dict[str, Any], str]:
    """A fresh Kernel's op sending someone PAID of USDC, paying by its fees at the relay's
    price, sent to the relay and taken, and its transaction sent for the next block (not yet
    built): the op, and its hash."""
    owner = fresh_client(f"{what}: the op's owner")
    recipient = fresh_client(f"{what}: the op's recipient").address
    await chain.set_eth_balance(await kernel.address_of(chain, owner), ACCOUNT_ETH)
    price = (await rpc(http, served.chain_id, "pimlico_getUserOperationGasPrice", []))["result"]
    op = await fee_op(chain, http, served, owner, recipient, price["standard"])
    sent = await rpc(http, served.chain_id, "eth_sendUserOperation", [op, ENTRY_POINT_0_7])
    assert "result" in sent, sent
    await served.send()
    return op, sent["result"]


async def receipt_of(http: Any, served: Served, op_hash: str) -> dict[str, Any] | None:
    answer = await rpc(http, served.chain_id, "eth_getUserOperationReceipt", [op_hash])
    return answer["result"]


async def test_an_op_another_bundler_landed_first_lands_nothing_of_the_relays(
    chain: LocalChain, served: Served
) -> None:
    """An op taken and sent, then landed by another bundler before the relay's block: the
    relay's transaction would fail the op's validation, so nothing of the relay's lands in that
    block, and its payee is paid nothing; the op is then the other bundler's, never the
    relay's."""
    async with served.interface() as http:
        op, op_hash = await taken(chain, http, served, "an op another bundler lands first")
        before = await chain.eth_balance(served.payee)
        other = fresh_client("another bundler's beneficiary").address
        signature = bytes.fromhex(op["signature"][2:])
        data = HANDLE_OPS + encode([f"{OP}[]", "address"], [[packed(op, op, signature)], other])
        await chain.rpc(
            "eth_sendTransaction",
            [{"from": await chain.account(0), "to": ENTRY_POINT_0_7, "data": "0x" + data.hex()}],
        )
        block = await served.builder.build_block()
        await served.on_block(await served.head())
        await served.land()
        receipt = await receipt_of(http, served, op_hash)

    assert block.transactions == (), block
    assert receipt is None, receipt
    assert await chain.eth_balance(served.payee) == before
    assert await chain.eth_balance(other) > 0


async def test_an_op_whose_account_cant_pay_its_prefund_lands_nothing_of_the_relays(
    chain: LocalChain, served: Served
) -> None:
    """An op taken and sent, then its account drained of its ETH before the relay's block: its
    prefund can't be paid, so its validation fails, nothing of the relay's lands, and the
    payee is paid nothing."""
    async with served.interface() as http:
        op, op_hash = await taken(chain, http, served, "an op whose account is drained of ETH")
        before = await chain.eth_balance(served.payee)
        await chain.set_eth_balance(op["sender"], 0)
        block = await served.builder.build_block()
        await served.on_block(await served.head())
        await served.land()
        receipt = await receipt_of(http, served, op_hash)

    assert block.transactions == (), block
    assert receipt is None, receipt
    assert await chain.eth_balance(served.payee) == before


async def test_an_op_whose_call_fails_at_its_block_still_pays(
    chain: LocalChain, served: Served
) -> None:
    """An op taken and sent, then its account drained of the USDC its call sends before the
    relay's block: its call reverts, cheaper than it simulated, but the op is still charged its
    gas, so it lands failed, the EntryPoint paying the payee its gas cost, more than the
    relay's transaction cost."""
    async with served.interface() as http:
        op, op_hash = await taken(chain, http, served, "an op whose account is drained of USDC")
        before = await chain.eth_balance(served.payee)
        await chain.set_token_balance(USDC, op["sender"], 0)
        block = await served.builder.build_block()
        await served.on_block(await served.head())
        receipt = await receipt_of(http, served, op_hash)

    assert len(block.transactions) == 1, block
    assert receipt is not None
    assert receipt["success"] is False, receipt
    gained = await chain.eth_balance(served.payee) - before
    assert gained == int(receipt["actualGasCost"], 16)
    cost = int(receipt["receipt"]["gasUsed"], 16) * int(receipt["receipt"]["effectiveGasPrice"], 16)
    assert gained > cost, (gained, cost)
