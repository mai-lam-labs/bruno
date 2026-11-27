"""The forwarder on anvil: its generated code deployed through Arachnid's deployer, paid in real
tokens and in ETH, and called by an account that's nobody's: what it holds goes to its payee,
or it reverts."""

from typing import Any

import httpx
import pytest
from eth_account import Account
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from bruno.deployer import DeployerError, deploy_code
from bruno.evm import NATIVE_ASSET, EncodedCall
from bruno.forks import GasSchedule
from bruno.forwarder import eth_gas, forwarder_of, forwarding, token_gas
from bruno.runner import BALANCE_UNREADABLE, UNDERPAID
from bruno.tests.localchain import (
    REVERT_ALWAYS,
    STA,
    TOKEN_OWNER_ACCOUNT,
    USDC,
    USDT,
    WETH,
    LocalChain,
    LocalToken,
)

# anvil's dev accounts: #8 the payee, #5 whoever calls the forwarder, #6 a payer of ETH
PAYEE_ACCOUNT = 8
CALLER_ACCOUNT = 5
PAYER_ACCOUNT = 6
WALLET = Account.from_key(keccak(text="the forwarder test's wallet"))
AMOUNT = 1_000_000
TIP = 250
ETH_AMOUNT = 10**15
STIPEND = 2_300
# Statera burns a hundredth of each transfer
STA_BURNT = 100


def fresh(name: str) -> ChecksumAddress:
    return to_checksum_address(Account.from_key(keccak(text=name)).address)


async def revert_of(chain: LocalChain, sender: str, call: EncodedCall) -> bytes | None:
    """The revert data of `call` made by `sender` now: None if it succeeds."""
    made = {"from": sender, "to": call.to, "data": "0x" + call.data.hex()}
    simulating = {"blockStateCalls": [{"calls": [made]}]}
    (block,) = await chain.rpc("eth_simulateV1", [simulating, "latest"])
    (result,) = block["calls"]
    if int(result["status"], 16) == 1:
        return None
    return bytes.fromhex(result["error"].get("data", "0x")[2:])


async def test_a_forwarder_is_deployed_where_missing_and_not_again(chain: LocalChain) -> None:
    """The deployment the CLI makes (make NETWORK-forwarder-deploy): through Arachnid's, from a
    funded key, to the address the payee's code gives it; nothing sent where it's there."""
    forwarder = forwarder_of(fresh("a payee whose forwarder isn't deployed"))
    await chain.set_eth_balance(WALLET.address, 10**18)
    async with httpx.AsyncClient() as client:
        live = await chain.chain(client)
        sent = await deploy_code(live, WALLET, forwarder.initcode, forwarder.runtime, "it")
        again = await deploy_code(live, WALLET, forwarder.initcode, forwarder.runtime, "it")

    assert sent is not None
    assert again is None
    assert await chain.code(forwarder.address) == "0x" + forwarder.runtime.hex()


async def test_an_unfunded_wallet_deploys_nothing(chain: LocalChain) -> None:
    """A deployment from a wallet that can't cover it at its max fee is refused before
    anything is sent."""
    forwarder = forwarder_of(fresh("a payee whose forwarder an unfunded wallet can't deploy"))
    async with httpx.AsyncClient() as client:
        live = await chain.chain(client)
        with pytest.raises(DeployerError, match="under the"):
            await deploy_code(live, Account.create(), forwarder.initcode, forwarder.runtime, "it")

    assert await chain.code(forwarder.address) == "0x"


@pytest.mark.parametrize("token", [USDC, USDT, WETH], ids=lambda t: t.symbol)
async def test_a_token_paid_in_goes_to_the_payee_but_for_one_unit(
    chain: LocalChain, token: LocalToken
) -> None:
    """A token paid into the forwarder, then forwarded by anyone for the amount: the payee
    gains it, less the one unit the forwarder keeps from its first payment on; a second
    payment, a tip with it, reaches the payee whole. Whoever calls gains nothing. USDT's
    transfer answers nothing, WETH's and USDC's true."""
    payee = await chain.account(PAYEE_ACCOUNT)
    caller = await chain.account(CALLER_ACCOUNT)
    forwarder = await chain.deploy_forwarder(payee)
    held = await chain.balance_of(token, payee)

    await chain.mint(token, forwarder.address, AMOUNT)
    await chain.send_call(
        caller, forwarding(forwarder.address, token.address, AMOUNT), "forwarding"
    )

    assert await chain.balance_of(token, payee) == held + AMOUNT - 1
    assert await chain.balance_of(token, forwarder.address) == 1

    await chain.mint(token, forwarder.address, AMOUNT + TIP)
    await chain.send_call(
        caller, forwarding(forwarder.address, token.address, AMOUNT), "forwarding"
    )

    assert await chain.balance_of(token, payee) == held + 2 * AMOUNT + TIP - 1
    assert await chain.balance_of(token, forwarder.address) == 1
    assert await chain.balance_of(token, caller) == 0


@pytest.mark.parametrize(
    ("kept", "paid", "least"),
    [(0, 0, 0), (0, AMOUNT - 1, AMOUNT), (1, 0, 2), (1, AMOUNT - 2, AMOUNT)],
    ids=["holding nothing", "a unit short", "only its unit", "short with its unit kept"],
)
async def test_too_little_held_reverts_underpaid(
    chain: LocalChain, kept: int, paid: int, least: int
) -> None:
    """A forwarder holding less than was asked for reverts Underpaid(): holding nothing,
    whatever is asked; a unit short of the amount on its first payment (the unit it keeps);
    and, its unit kept from before, holding that alone or two units short. One unit short with
    its unit kept is the least it lets through: the payee gains the amount less one."""
    payee = await chain.account(PAYEE_ACCOUNT)
    caller = await chain.account(CALLER_ACCOUNT)
    forwarder = await chain.deploy_forwarder(payee)
    held = await chain.balance_of(USDC, payee)
    if kept + paid:
        await chain.mint(USDC, forwarder.address, kept + paid)

    assert (
        await revert_of(chain, caller, forwarding(forwarder.address, USDC.address, least))
        == UNDERPAID
    )

    await chain.mint(USDC, forwarder.address, 1)
    await chain.send_call(caller, forwarding(forwarder.address, USDC.address, least), "forwarding")

    assert await chain.balance_of(USDC, payee) == held + max(least, 1) - 1


async def test_a_taxed_tokens_payment_counts_as_what_arrived(chain: LocalChain) -> None:
    """Statera burns a hundredth of every transfer: forwarding the amount paid in reverts
    Underpaid(), the payee's gain being less; asked for what arrives, it lands, and the payee
    has gained that."""
    payee = await chain.account(PAYEE_ACCOUNT)
    caller = await chain.account(CALLER_ACCOUNT)
    forwarder = await chain.deploy_forwarder(payee)
    paid = 10**18
    await chain.mint(STA, forwarder.address, paid)
    arriving = paid - paid // STA_BURNT

    assert (
        await revert_of(chain, caller, forwarding(forwarder.address, STA.address, paid))
        == UNDERPAID
    )

    await chain.send_call(
        caller, forwarding(forwarder.address, STA.address, arriving), "forwarding"
    )

    assert arriving - 1 <= await chain.balance_of(STA, payee) < paid


async def test_a_token_refusing_the_transfer_reverts_underpaid(chain: LocalChain) -> None:
    """USDC with the payee blacklisted refuses the forwarder's transfer: nothing moves, so the
    forwarder, which doesn't read what the transfer answered, reverts Underpaid()."""
    payee = await chain.account(PAYEE_ACCOUNT)
    caller = await chain.account(CALLER_ACCOUNT)
    forwarder = await chain.deploy_forwarder(payee)
    await chain.mint(USDC, forwarder.address, AMOUNT)
    owner = await chain.account(TOKEN_OWNER_ACCOUNT)
    await chain.send(owner, USDC.address, "blacklist(address)", ["address"], [payee])

    assert (
        await revert_of(chain, caller, forwarding(forwarder.address, USDC.address, AMOUNT))
        == UNDERPAID
    )


async def test_a_token_that_isnt_one_reverts_balance_unreadable(chain: LocalChain) -> None:
    """An address without code answers balanceOf with nothing: BalanceUnreadable()."""
    caller = await chain.account(CALLER_ACCOUNT)
    forwarder = await chain.deploy_forwarder(await chain.account(PAYEE_ACCOUNT))
    no_token = fresh("no token is here")

    call = forwarding(forwarder.address, no_token, AMOUNT)

    assert await revert_of(chain, caller, call) == BALANCE_UNREADABLE


async def test_eth_paid_in_goes_to_the_payee_whole(chain: LocalChain) -> None:
    """ETH sent to the forwarder with no calldata is kept; forwarded by anyone for the amount,
    all of it reaches the payee, a tip included; asked for more than it holds, it reverts
    Underpaid()."""
    payee = await chain.account(PAYEE_ACCOUNT)
    caller = await chain.account(CALLER_ACCOUNT)
    payer = await chain.account(PAYER_ACCOUNT)
    forwarder = await chain.deploy_forwarder(payee)
    paying = EncodedCall(forwarder.address, b"", ETH_AMOUNT + TIP)
    await chain.send_call(payer, paying, "paying the forwarder")
    held = await chain.eth_balance(payee)

    assert await chain.eth_balance(forwarder.address) == ETH_AMOUNT + TIP
    too_much = forwarding(forwarder.address, NATIVE_ASSET, ETH_AMOUNT + TIP + 1)
    assert await revert_of(chain, caller, too_much) == UNDERPAID

    await chain.send_call(
        caller, forwarding(forwarder.address, NATIVE_ASSET, ETH_AMOUNT), "forwarding"
    )

    assert await chain.eth_balance(payee) == held + ETH_AMOUNT + TIP
    assert await chain.eth_balance(forwarder.address) == 0


async def test_a_payee_refusing_eth_reverts_as_it_did(chain: LocalChain) -> None:
    """A payee whose code refuses ETH: forwarding reverts with the payee's own revert data
    (nothing, here), and the ETH stays in the forwarder."""
    payee = fresh("a payee refusing ETH")
    await chain.rpc("anvil_setCode", [payee, REVERT_ALWAYS])
    caller = await chain.account(CALLER_ACCOUNT)
    forwarder = await chain.deploy_forwarder(payee)
    await chain.set_eth_balance(forwarder.address, ETH_AMOUNT)

    assert (
        await revert_of(chain, caller, forwarding(forwarder.address, NATIVE_ASSET, ETH_AMOUNT))
        == b""
    )


def _inner_gas(trace: dict[str, Any]) -> int:
    return sum(int(call["gasUsed"], 16) for call in trace["calls"])


@pytest.mark.parametrize("token", [USDC, USDT], ids=lambda t: t.symbol)
async def test_forwarding_a_token_takes_the_gas_the_schedule_says(
    chain: LocalChain, schedule: GasSchedule, token: LocalToken
) -> None:
    """A forwarding's gas, less its intrinsic gas and what its four calls to the token used
    (the transaction's trace), is the forwarder's own, exactly as the assembler summed it."""
    caller = await chain.account(CALLER_ACCOUNT)
    forwarder = await chain.deploy_forwarder(await chain.account(PAYEE_ACCOUNT))
    await chain.mint(token, forwarder.address, AMOUNT)
    call = forwarding(forwarder.address, token.address, AMOUNT)

    receipt = await chain.send_call(caller, call, "forwarding")
    tracer = {"tracer": "callTracer"}
    trace = await chain.rpc("debug_traceTransaction", [receipt["transactionHash"], tracer])

    execution = int(receipt["gasUsed"], 16) - schedule.intrinsic(call.data).standard

    assert len(trace["calls"]) == 4
    assert execution == token_gas(schedule) + _inner_gas(trace)


@pytest.mark.parametrize("new_payee", [False, True], ids=["an account there", "a new account"])
async def test_forwarding_eth_takes_its_gas_less_the_stipend(
    chain: LocalChain, schedule: GasSchedule, new_payee: bool
) -> None:
    """ETH forwarded to a payee without code uses exactly the gas the schedule gives for it,
    but for the stipend the payee hands back: to an account that exists, and to one the ETH
    creates."""
    caller = await chain.account(CALLER_ACCOUNT)
    payee = fresh("a payee no ETH has reached") if new_payee else await chain.account(PAYEE_ACCOUNT)
    forwarder = await chain.deploy_forwarder(payee)
    await chain.set_eth_balance(forwarder.address, ETH_AMOUNT)
    call = forwarding(forwarder.address, NATIVE_ASSET, ETH_AMOUNT)

    receipt = await chain.send_call(caller, call, "forwarding")

    assert int(receipt["gasUsed"], 16) == (
        schedule.intrinsic(call.data).standard + eth_gas(schedule, new_payee=new_payee) - STIPEND
    )
