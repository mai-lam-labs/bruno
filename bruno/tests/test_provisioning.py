"""EIP-7702 provisioning through the runner: a fresh account holding only a token, no ETH,
signs an authorization to a delegate and its first calls, and the relay runs them in one
type-4 transaction to the deployer, paid in that token.

The relay doesn't care what the delegate is or what the calls do: the runner only asserts
the payee was paid. Calibur is the delegate here, its signed batch (executor 0: anyone may
run it) the client's one call; any delegate that runs calls on the account's own signature
works the same way."""

from eth_account import Account

from bruno.forks import GasSchedule
from bruno.runner import UNDERPAID, Payment, in_call_runner
from bruno.tests.calibur import calibur_batch, fresh_client
from bruno.tests.deployments import CLIENT_GAS, approve, deploy, payment_token, transfer
from bruno.tests.localchain import CALIBUR, PERMIT2, USDC, WETH, LocalChain

PAYEE_ACCOUNT = 8
AMOUNT = 1_000_000
MAX_UINT = 2**256 - 1


async def test_fresh_account_is_provisioned_and_pays(
    chain: LocalChain, schedule: GasSchedule
) -> None:
    """A fresh account holding only USDC delegates to Calibur, approves Permit2 for WETH and
    pays the payee, in one type-4 transaction it pays no ETH for."""
    client = fresh_client()
    payee = await chain.account(PAYEE_ACCOUNT)
    await chain.mint(USDC, client.address, AMOUNT)
    chain_id = await chain.chain_id()
    authorization = Account.sign_authorization(
        {"chainId": chain_id, "address": CALIBUR, "nonce": 0}, client.key
    )
    batch = calibur_batch(
        client, chain_id, [approve(WETH, PERMIT2, MAX_UINT), transfer(USDC, payee, AMOUNT)]
    )
    usdc = await payment_token(chain, USDC, payee, schedule=schedule)
    runner = in_call_runner(
        batch,
        Payment(usdc, payee, AMOUNT),
        delegated={client.address},
        schedule=schedule,
        client_gas=CLIENT_GAS,
    )
    before = await chain.balance_of(USDC, payee)

    # the client holds only USDC: no ETH, no nonce, so its account is empty
    deployment = await deploy(
        chain, runner, schedule, authorizations=[authorization], empty_authorities=1
    )

    assert deployment.succeeded
    assert await chain.code(client.address) == "0xef0100" + CALIBUR[2:].lower()
    assert await chain.balance_of(USDC, payee) - before == AMOUNT
    allowance = await chain.call(
        WETH.address,
        "allowance(address,address)",
        ["address", "address"],
        [client.address, PERMIT2],
    )
    assert int.from_bytes(allowance) == MAX_UINT
    assert await chain.eth_balance(client.address) == 0


async def test_stale_authorization_is_underpaid(chain: LocalChain, schedule: GasSchedule) -> None:
    """An authorization with the wrong nonce is skipped (EIP-7702): the account stays
    codeless, the call to it does nothing, and the runner reverts Underpaid()."""
    client = fresh_client()
    payee = await chain.account(PAYEE_ACCOUNT)
    await chain.mint(USDC, client.address, AMOUNT)
    chain_id = await chain.chain_id()
    stale = Account.sign_authorization(
        {"chainId": chain_id, "address": CALIBUR, "nonce": 1}, client.key
    )
    batch = calibur_batch(client, chain_id, [transfer(USDC, payee, AMOUNT)])
    usdc = await payment_token(chain, USDC, payee, schedule=schedule)
    runner = in_call_runner(
        batch,
        Payment(usdc, payee, AMOUNT),
        delegated={client.address},
        schedule=schedule,
        client_gas=CLIENT_GAS,
    )

    deployment = await deploy(chain, runner, schedule, authorizations=[stale], empty_authorities=1)

    assert not deployment.succeeded
    assert deployment.output == UNDERPAID
    assert await chain.code(client.address) == "0x"
