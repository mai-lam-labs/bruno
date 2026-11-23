"""A listed call and the transfer that pays for it, each sent as its own transaction on anvil at
exactly the gas planned for it, the token's transfer measured to the gas: what a node whose
estimates are exact shows, and anvil's own, with room to spare, doesn't."""

from dataclasses import replace

from eth_typing import ChecksumAddress

from bruno.abi import address_word, selector
from bruno.eip3009 import measuring as measuring_transfer
from bruno.evm import EncodedCall
from bruno.forks import GasSchedule
from bruno.plan import CallAccounts, PaidByTransfer, drafted
from bruno.runner import Erc20
from bruno.submission import Submission
from bruno.tests.calibur import fresh_client
from bruno.tests.deployments import SENDER_ACCOUNT, payment_token
from bruno.tests.localchain import USDC, USDC_DOMAIN, LocalChain, LocalChainError
from bruno.tests.transfers import CALL_GAS, MULTICALL3, by_key, usdc_transfer

# anvil's dev account #8: the payee
PAYEE_ACCOUNT = 8


async def measured_exactly(
    chain: LocalChain, payee: ChecksumAddress, schedule: GasSchedule
) -> Erc20:
    """USDC as the relay measures it, its zero-value transfer's gas the least a transaction of
    it succeeds with, found by bisection: a node's exact estimate."""
    usdc = await payment_token(chain, USDC, payee, schedule)
    call, calldata = measuring_transfer(USDC_DOMAIN, await chain.chain_id(), USDC.address, payee)
    (transaction, *_) = call.params
    assert isinstance(transaction, dict)

    async def succeeds(gas: int) -> bool:
        try:
            await chain.rpc("eth_call", [{**transaction, "gas": hex(gas)}, "latest"])
        except LocalChainError:
            return False
        return True

    low, high = 0, int(await chain.rpc(call.method, list(call.params)), 16)
    while high - low > 1:
        middle = (low + high) // 2
        low, high = (low, middle) if await succeeds(middle) else (middle, high)
    return replace(usdc, transfer_gas=high - schedule.intrinsic(calldata).standard)


async def test_a_call_and_its_transfer_land_in_the_gas_planned(
    chain: LocalChain, schedule: GasSchedule
) -> None:
    """A listed call paid by a key's transfer, planned from a transfer measured to the gas, each
    of its two transactions sent at its gas limit: both succeed, and the payee gains the
    amount. The transfer is the transaction, so what its token spends is spent a call below
    (a proxy's implementation), which is given all but a 64th of what's left."""
    payer = fresh_client("pays for a listed call, measured exactly")
    payee = await chain.account(PAYEE_ACCOUNT)
    sender = await chain.account(SENDER_ACCOUNT)
    chain_id = await chain.chain_id()
    amount = 1_000_000
    # both hold some before and after: each balance is written, neither set nor cleared
    await chain.mint(USDC, payer.address, 2 * amount)
    await chain.mint(USDC, payee, 1)
    data = selector("getEthBalance(address)") + address_word(payer.address)
    call = EncodedCall(MULTICALL3, data)
    transfer = await usdc_transfer(chain_id, payer.address, payee, amount, by_key(payer))
    submission = Submission(call, (), CALL_GAS, USDC.address, payee, amount, 0, transfer)
    proof = PaidByTransfer()
    draft = drafted(
        submission,
        proof,
        (),
        CallAccounts(frozenset(), 0, frozenset()),
        await measured_exactly(chain, payee, schedule),
        schedule,
        payer_has_code=False,
        payee_holds=True,
    )
    before = await chain.balance_of(USDC, payee)

    for step in proof.plan(chain_id, draft, schedule).steps:
        await chain.send_call(sender, step.transaction, "a step of the plan", step.gas_limit)

    assert await chain.balance_of(USDC, payee) - before == amount
