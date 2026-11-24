"""A sponsorship planned and landed on anvil, each transaction of the relay's signed at exactly
its planned gas: an account holding a token and no ETH, its own transactions between the
relay's two; and one whose delegate's code pays, with no transaction of its own."""

from dataclasses import replace
from typing import Any

import httpx
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress

from bruno.accounts import derive
from bruno.chain import Chain
from bruno.evm import EncodedCall
from bruno.forks import GasSchedule
from bruno.measurements import Measurements, measure
from bruno.plan import CheckedByForwarder, Plan, Step, make_plan, transaction_json
from bruno.runner import UNDERPAID
from bruno.simulation import SignedSimulation, simulations
from bruno.sponsorship import Sponsorship
from bruno.tests.calibur import calibur_batch, fresh_client
from bruno.tests.deployments import transfer
from bruno.tests.localchain import CALIBUR, TEST_FLOAT, USDC, LocalChain
from bruno.tests.served import PAYEE_ACCOUNT, Setup, relay_config
from bruno.transactions import Decoded, Fees, decoded, sign

# the relay's account: a concurrency account's place, funded
RELAY = derive(TEST_FLOAT, 400)
AMOUNT = 2_000_000
ACTION = 500_000
TIP = 10**9
DEADLINE_BLOCKS = 10


async def measured_for(chain: LocalChain, live: Chain, payee: ChecksumAddress) -> Measurements:
    """The chain as the relay measures it for `payee`, whose forwarder is deployed first."""
    config = relay_config(Setup(), chain.url, payee, "http://127.0.0.1:9/")
    return await measure(live, config, payee)


async def signed_by(
    chain: LocalChain, user: LocalAccount, nonce: int, call: EncodedCall, max_fee: int
) -> Decoded:
    """`call` as `user`'s wallet signs it, without sending it: its gas the node's estimate."""
    asked = {"from": user.address, "to": call.to, "data": "0x" + call.data.hex()}
    tx = {
        "type": 2,
        "chainId": await chain.chain_id(),
        "nonce": nonce,
        "to": call.to,
        "data": "0x" + call.data.hex(),
        "value": 0,
        "gas": int(await chain.rpc("eth_estimateGas", [asked]), 16),
        "maxFeePerGas": max_fee,
        "maxPriorityFeePerGas": TIP,
    }
    return decoded(bytes(Account.sign_transaction(tx, user.key).raw_transaction))


async def landed(chain: LocalChain, plan: Plan, fees: Fees) -> list[dict[str, Any]]:
    """`plan`'s transactions sent in order, the relay's signed at exactly their planned gas and
    the account's as signed: their receipts, each of which succeeded."""
    chain_id = await chain.chain_id()
    nonce = await chain.nonce(RELAY.address)
    receipts: list[dict[str, Any]] = []
    for step in plan.steps:
        if isinstance(step, Step):
            call = step.transaction
            tx = sign(
                RELAY,
                chain_id,
                nonce,
                fees,
                step.gas_limit,
                call.to,
                call.data,
                call.value,
                authorizations=step.authorizations,
            )
            nonce += 1
            receipts.append(await chain.submit(tx.raw))
        else:
            receipts.append(await chain.submit(step.transaction.signed.raw))
        assert receipts[-1]["status"] == "0x1", (step, receipts[-1])
    return receipts


async def test_an_accounts_transactions_land_between_the_relays_two(
    chain: LocalChain, schedule: GasSchedule
) -> None:
    """An account holding USDC and no ETH signs a transfer of its own and its payment to the
    forwarder. The relay's first transaction sends it the ETH the two need, its last calls the
    forwarder for the amount: simulated as they'll be signed, then sent at exactly their planned
    gas, all four land. The payee gains the amount (less the forwarder's unit), the account's
    transfer was made as its own, and what its transactions didn't spend stays with it. Asked
    for more than was paid, the same transactions fail their simulation, Underpaid()."""
    user = fresh_client("sponsored, holding USDC and no ETH")
    payee = await chain.account(PAYEE_ACCOUNT)
    friend = await chain.account(3)
    forwarder = await chain.deploy_forwarder(payee)
    await chain.set_eth_balance(RELAY.address, 10**18)
    await chain.mint(USDC, user.address, ACTION + AMOUNT)
    max_fee = 2 * await chain.base_fee() + TIP
    fees = Fees(max_fee, TIP)
    transactions = (
        await signed_by(chain, user, 0, transfer(USDC, friend, ACTION), max_fee),
        await signed_by(chain, user, 1, transfer(USDC, forwarder.address, AMOUNT), max_fee),
    )
    held = await chain.balance_of(USDC, payee), await chain.balance_of(USDC, friend)

    async with httpx.AsyncClient() as client:
        live = await chain.chain(client)
        head = await live.head()
        measured = await measured_for(chain, live, payee)
        sponsorship = Sponsorship(
            account=user.address,
            authorizations=(),
            call=None,
            gas=0,
            transactions=transactions,
            token=USDC.address,
            payee=forwarder.address,
            amount=AMOUNT,
            max_block_number=head + DEADLINE_BLOCKS,
        )

        async def planned(asking: Sponsorship) -> tuple[Plan, bytes | Exception | None]:
            plan = await make_plan(
                live,
                asking,
                CheckedByForwarder(),
                schedule,
                measured.tokens,
                RELAY.address,
                measured.payee_holds,
                payee_new=measured.payee_new,
            )
            nonce = await chain.nonce(RELAY.address)
            signing = SignedSimulation(asking, plan, RELAY.address, nonce, fees)
            (revert,) = await simulations(live, [signing])
            return plan, revert

        plan, revert = await planned(sponsorship)
        _, short = await planned(replace(sponsorship, amount=AMOUNT + 2))

    assert revert is None
    assert short == UNDERPAID
    assert plan.basis[0].value == sponsorship.funding

    receipts = await landed(chain, plan, fees)

    spent = sum(int(r["gasUsed"], 16) * int(r["effectiveGasPrice"], 16) for r in receipts[1:-1])
    assert await chain.balance_of(USDC, payee) == held[0] + AMOUNT - 1
    assert await chain.balance_of(USDC, friend) == held[1] + ACTION
    assert await chain.nonce(user.address) == 2
    assert await chain.eth_balance(user.address) == sponsorship.funding - spent


async def test_an_accounts_delegate_pays_with_no_transaction_of_its_own(
    chain: LocalChain, schedule: GasSchedule
) -> None:
    """An account holding USDC and nothing else signs a delegation to Calibur and a batch
    paying the forwarder. The relay's first transaction carries both and sends no ETH: the
    account's code runs in it and pays. Its last calls the forwarder. Both land at their planned
    gas, the first given the gas its estimate says the account's code needs; the account is
    delegated, was sent nothing, and the payee gains the amount."""
    user = fresh_client("sponsored by its delegate's code")
    payee = await chain.account(PAYEE_ACCOUNT)
    forwarder = await chain.deploy_forwarder(payee)
    chain_id = await chain.chain_id()
    await chain.set_eth_balance(RELAY.address, 10**18)
    await chain.mint(USDC, user.address, AMOUNT)
    fees = Fees(2 * await chain.base_fee() + TIP, TIP)
    signing = {"chainId": chain_id, "address": CALIBUR, "nonce": 0}
    authorization = Account.sign_authorization(signing, user.key)
    call = calibur_batch(user, chain_id, [transfer(USDC, forwarder.address, AMOUNT)])
    held = await chain.balance_of(USDC, payee)

    async with httpx.AsyncClient() as client:
        live = await chain.chain(client)
        head = await live.head()
        measured = await measured_for(chain, live, payee)

        async def planned(gas: int) -> Plan:
            sponsorship = Sponsorship(
                account=user.address,
                authorizations=(authorization,),
                call=call,
                gas=gas,
                transactions=(),
                token=USDC.address,
                payee=forwarder.address,
                amount=AMOUNT,
                max_block_number=head + DEADLINE_BLOCKS,
            )
            return await make_plan(
                live,
                sponsorship,
                CheckedByForwarder(),
                schedule,
                measured.tokens,
                RELAY.address,
                measured.payee_holds,
                payee_new=measured.payee_new,
            )

        # what the account's code needs: the first transaction's estimate, less what a plan
        # giving that code no gas already holds
        bare = (await planned(0)).own[0]
        estimate = int(
            await chain.rpc("eth_estimateGas", [transaction_json(bare, RELAY.address)]), 16
        )
        plan = await planned(estimate - bare.gas_limit)

    assert len(plan.steps) == 2
    assert plan.basis[0].value == 0

    await landed(chain, plan, fees)

    assert await chain.code(user.address) == "0xef0100" + CALIBUR[2:].lower()
    assert await chain.eth_balance(user.address) == 0
    assert await chain.balance_of(USDC, payee) == held + AMOUNT - 1
