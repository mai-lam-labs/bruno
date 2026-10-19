"""The runner, executed through the deployer on anvil. The client's one call is a batch it
signed, run by its account, an EOA delegated to Calibur."""

import httpx
import pytest
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_typing import HexStr
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from bruno.evm import EncodedCall
from bruno.forks import GasSchedule
from bruno.net.jsonrpc import JsonRpcError, call
from bruno.runner import BALANCE_UNREADABLE, UNDERPAID, Erc20, Payment, in_call_runner
from bruno.tests.calibur import calibur_batch
from bruno.tests.deployments import deploy, payment_token, raw_call, transfer, transfer_from
from bruno.tests.localchain import CALIBUR, USDC, USDT, LocalChain, LocalToken

# anvil's dev account #8: the payee, which sends nothing, so its ETH moves only by payments
PAYEE_ACCOUNT = 8
# makes a call (calldata target ‖ value ‖ data, from its own balance), then burns all but
# ~300 of the gas it's given, and succeeds.
#   PUSH1 64 CALLDATASIZE SUB DUP1 PUSH1 64 PUSH0 CALLDATACOPY
#   PUSH0 PUSH0 DUP3 PUSH0 PUSH1 32 CALLDATALOAD PUSH0 CALLDATALOAD GAS CALL
#   PUSH1 30 JUMPI RETURNDATASIZE PUSH0 PUSH0 RETURNDATACOPY RETURNDATASIZE PUSH0 REVERT
#   JUMPDEST PUSH2 300 GAS GT PUSH1 30 JUMPI STOP
PAY_THEN_BURN = to_checksum_address(keccak(text="pay then burn")[-20:])
PAY_THEN_BURN_CODE = (
    "0x604036038060405f375f5f825f6020355f355af1601e573d5f5f3e3d5ffd5b61012c5a11601e5700"
)
# the client gas the burning call is given: small enough that what EIP-150 keeps back from
# it (1/64) is less than the payment check needs, so only R leaves it enough. With R forced
# to 0 the test fails from 30,000 to 50,000 on Prague and 40,000 to 50,000 on Amsterdam, and
# passes as emitted (scratchpad/burn_sweep.py, 2026-10-19)
TIGHT_CLIENT_GAS = 45_000


@pytest.fixture
async def client(chain: LocalChain) -> LocalAccount:
    """The client's account, delegated to Calibur."""
    account = Account.from_key(keccak(text="a client"))
    await chain.rpc("anvil_setCode", [account.address, "0xef0100" + CALIBUR[2:].lower()])
    return account


async def batch(chain: LocalChain, client: LocalAccount, calls: list[EncodedCall]) -> EncodedCall:
    """The client's one call: `calls`, signed, run by its account."""
    return calibur_batch(client, await chain.chain_id(), calls)


def pays(token: LocalToken | None, to: str, amount: int) -> EncodedCall:
    """A payment to `to`: a token transfer, or ETH."""
    if token is None:
        return EncodedCall(to_checksum_address(to), HexStr("0x"), amount)
    return transfer(token, to, amount)


async def fund(chain: LocalChain, token: LocalToken | None, account: str, amount: int) -> None:
    if token is None:
        await chain.set_eth_balance(account, amount)
    else:
        await chain.mint(token, account, amount)


async def held(chain: LocalChain, token: LocalToken | None, account: str) -> int:
    if token is None:
        return await chain.eth_balance(account)
    return await chain.balance_of(token, account)


@pytest.mark.parametrize("token", [USDC, USDT, None], ids=["USDC", "USDT", "ETH"])
@pytest.mark.parametrize("over", [1, 0, -1], ids=["above", "at", "below"])
async def test_payment_against_minimum(
    chain: LocalChain,
    schedule: GasSchedule,
    client: LocalAccount,
    token: LocalToken | None,
    over: int,
) -> None:
    """At or above the minimum deploys, leaving the runner's address with no code; below
    reverts ERCNNNN_Underpaid(). USDT's transfer returns no bool; ETH is read with BALANCE."""
    payee = await chain.account(PAYEE_ACCOUNT)
    minimum = 1_000_000 if token is not None else 10**15
    paid = minimum + over
    await fund(chain, token, client.address, paid)
    erc20 = None if token is None else await payment_token(chain, token, payee, schedule)
    the_call = await batch(chain, client, [pays(token, payee, paid)])
    runner = in_call_runner(
        the_call, Payment(erc20, payee, minimum), schedule, delegated={client.address}
    )
    before = await held(chain, token, payee)

    deployment = await deploy(chain, runner, schedule)

    if over >= 0:
        assert deployment.succeeded
        assert await held(chain, token, payee) - before == paid
        address = to_checksum_address(deployment.output[12:])
        assert await chain.code(address) == "0x"
        assert await chain.nonce(address) == 1
    else:
        assert not deployment.succeeded
        assert deployment.output == UNDERPAID
        assert await held(chain, token, payee) == before


async def test_failing_call_reverts_with_its_own_data(
    chain: LocalChain, schedule: GasSchedule, client: LocalAccount
) -> None:
    """A call that reverts reverts the runner with exactly the call's revert data."""
    payee = await chain.account(PAYEE_ACCOUNT)
    amount = 1_000_000
    the_call = await batch(chain, client, [transfer(USDC, payee, amount)])  # it holds none
    usdc = await payment_token(chain, USDC, payee, schedule)
    runner = in_call_runner(the_call, Payment(usdc, payee, amount), schedule, {client.address})
    async with httpx.AsyncClient() as http:
        query = {"to": the_call.to, "data": the_call.data}
        with pytest.raises(JsonRpcError) as alone:
            await call(http, chain.url, "eth_call", [query, "pending"])

    deployment = await deploy(chain, runner, schedule)

    assert not deployment.succeeded
    assert isinstance(alone.value.data, str)
    assert deployment.output == bytes.fromhex(alone.value.data[2:])


async def test_paying_then_pulling_back_is_underpaid(
    chain: LocalChain, schedule: GasSchedule, client: LocalAccount
) -> None:
    """The payment is measured across the whole call: paid, then taken back, is unpaid."""
    payee = await chain.account(PAYEE_ACCOUNT)
    elsewhere = await chain.account(7)
    amount = 1_000_000
    await fund(chain, USDC, client.address, amount)
    await chain.approve(USDC, payee, client.address, amount)
    the_call = await batch(
        chain,
        client,
        [transfer(USDC, payee, amount), transfer_from(USDC, payee, elsewhere, amount)],
    )
    usdc = await payment_token(chain, USDC, payee, schedule)
    runner = in_call_runner(the_call, Payment(usdc, payee, amount), schedule, {client.address})

    deployment = await deploy(chain, runner, schedule)

    assert not deployment.succeeded
    assert deployment.output == UNDERPAID


async def test_payee_losing_funds_is_underpaid(
    chain: LocalChain, schedule: GasSchedule, client: LocalAccount
) -> None:
    """A payee left with less than it started with is underpaid: after - before isn't
    computed, so can't wrap to a huge payment."""
    payee = await chain.account(PAYEE_ACCOUNT)
    elsewhere = await chain.account(7)
    amount = 1_000_000
    await fund(chain, USDC, client.address, amount)
    await chain.mint(USDC, payee, 2 * amount)
    await chain.approve(USDC, payee, client.address, 2 * amount)
    the_call = await batch(
        chain,
        client,
        [transfer(USDC, payee, amount), transfer_from(USDC, payee, elsewhere, 2 * amount)],
    )
    usdc = await payment_token(chain, USDC, payee, schedule)
    runner = in_call_runner(the_call, Payment(usdc, payee, amount), schedule, {client.address})

    deployment = await deploy(chain, runner, schedule)

    assert not deployment.succeeded
    assert deployment.output == UNDERPAID


async def test_payment_in_parts(
    chain: LocalChain, schedule: GasSchedule, client: LocalAccount
) -> None:
    """Only the payee's total gain counts, however the call makes it up."""
    payee = await chain.account(PAYEE_ACCOUNT)
    amount = 1_000_000
    await fund(chain, USDT, client.address, amount)
    half = amount // 2
    the_call = await batch(
        chain, client, [transfer(USDT, payee, half), transfer(USDT, payee, amount - half)]
    )
    usdt = await payment_token(chain, USDT, payee, schedule)
    runner = in_call_runner(the_call, Payment(usdt, payee, amount), schedule, {client.address})

    assert (await deploy(chain, runner, schedule)).succeeded


async def test_unreadable_balance(
    chain: LocalChain, schedule: GasSchedule, client: LocalAccount
) -> None:
    """A payment token with no code answers balanceOf with nothing:
    ERCNNNN_BalanceUnreadable()."""
    payee = await chain.account(PAYEE_ACCOUNT)
    no_code = to_checksum_address(keccak(text="no token here")[-20:])
    the_call = await batch(chain, client, [])
    runner = in_call_runner(
        the_call, Payment(Erc20(no_code, 10_000), payee, 1), schedule, {client.address}
    )

    deployment = await deploy(chain, runner, schedule)

    assert not deployment.succeeded
    assert deployment.output == BALANCE_UNREADABLE


async def test_balance_gas_is_a_cap(
    chain: LocalChain, schedule: GasSchedule, client: LocalAccount
) -> None:
    """A balanceOf given less than it needs fails, and the runner with it."""
    payee = await chain.account(PAYEE_ACCOUNT)
    amount = 1_000_000
    await fund(chain, USDC, client.address, amount)
    measured = await payment_token(chain, USDC, payee, schedule)
    short = Erc20(USDC.address, measured.balance_gas // 2)
    the_call = await batch(chain, client, [transfer(USDC, payee, amount)])
    runner = in_call_runner(the_call, Payment(short, payee, amount), schedule, {client.address})

    deployment = await deploy(chain, runner, schedule)

    assert not deployment.succeeded
    assert deployment.output == BALANCE_UNREADABLE


async def test_payment_check_survives_a_call_that_burns_its_gas(
    chain: LocalChain, schedule: GasSchedule
) -> None:
    """A call that pays, then burns all it was given and succeeds, still leaves the runner
    enough to check the payment, at the gas limit the bounds give for a tight client
    budget."""
    payee = await chain.account(PAYEE_ACCOUNT)
    amount = 1_000_000
    await chain.rpc("anvil_setCode", [PAY_THEN_BURN, PAY_THEN_BURN_CODE])
    await chain.mint(USDT, PAY_THEN_BURN, amount)
    await chain.mint(USDT, payee, 1)  # the payee's balance slot exists: no new slot to pay for
    paying = transfer(USDT, payee, amount)
    data = bytes.fromhex(paying.data[2:])
    the_call = raw_call(PAY_THEN_BURN, int(USDT.address, 16).to_bytes(32) + bytes(32) + data)
    usdt = await payment_token(chain, USDT, payee, schedule)
    runner = in_call_runner(the_call, Payment(usdt, payee, amount), schedule)

    deployment = await deploy(chain, runner, schedule, client_gas=TIGHT_CLIENT_GAS)

    assert deployment.succeeded
