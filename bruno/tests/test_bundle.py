"""A block's bundle end to end, through the mock builder: the float's funding transaction,
then one transaction a submission, each from its own concurrency account, each a fresh
account's EIP-7702 provisioning paid in a token. Only the paying submissions land; the rest
are dropped and cost nothing."""

from dataclasses import dataclass

import httpx
import pytest
from eth_account import Account
from eth_account.datastructures import SignedSetCodeAuthorization
from eth_account.signers.local import LocalAccount
from eth_utils.address import to_checksum_address

from bruno.accounts import derive
from bruno.bundle import Mode, TopUp, bundles, shortfall, sign_funding
from bruno.chain import Chain
from bruno.deployer import DEPLOYER, transaction_gas
from bruno.forks import GasSchedule
from bruno.maintenance import replenish
from bruno.runner import Payment, in_call_runner
from bruno.tests.calibur import calibur_batch, fresh_client
from bruno.tests.deployments import CLIENT_GAS, payment_token, transfer
from bruno.tests.localchain import CALIBUR, TEST_FLOAT, USDC, USDT, LocalChain, LocalToken
from bruno.tests.mockbuilder import MockBuilder
from bruno.transactions import Fees, SignedTransaction, sign

AMOUNT = 1_000_000
FLOAT_ETH = 10**18
# how long a transaction is waited for: anvil mines it at once
TIMEOUT_SECONDS = 10


@dataclass(frozen=True, slots=True)
class Case:
    """A submission: its client pays `paid` of `token` (the price being AMOUNT), its
    authorization's nonce is `auth_nonce` (0 is right for a fresh account), and its
    concurrency account sends at `nonce_offset` past its nonce (0 is right)."""

    name: str
    token: LocalToken
    paid: int = AMOUNT
    auth_nonce: int = 0
    nonce_offset: int = 0

    @property
    def lands(self) -> bool:
        return self.paid >= AMOUNT and self.auth_nonce == 0 and self.nonce_offset == 0


CASES = (
    Case("pays in USDC", USDC),
    Case("pays short", USDC, paid=AMOUNT - 1),
    Case("stale authorization", USDC, auth_nonce=1),
    Case("stale account nonce", USDC, nonce_offset=1),
    Case("pays in USDT", USDT),
)


async def submission(
    chain: LocalChain,
    schedule: GasSchedule,
    case: Case,
    index: int,
    account: LocalAccount,
    payee: str,
    fees: Fees,
) -> SignedTransaction:
    """The case's client provisioned and paying, run by `account`'s transaction."""
    client = fresh_client(case.name)
    chain_id = await chain.chain_id()
    await chain.mint(case.token, client.address, case.paid)
    authorization: SignedSetCodeAuthorization = Account.sign_authorization(
        {"chainId": chain_id, "address": CALIBUR, "nonce": case.auth_nonce}, client.key
    )
    batch = calibur_batch(client, chain_id, [transfer(case.token, payee, case.paid)])
    token = await payment_token(chain, case.token, payee, schedule)
    runner = in_call_runner(
        batch,
        Payment(token, to_checksum_address(payee), AMOUNT),
        schedule,
        delegated={client.address},
    )
    # the client holds only tokens: its account is empty
    gas = transaction_gas(runner.initcode, runner.frame_gas(CLIENT_GAS), schedule, 1, 1)
    nonce = await chain.nonce(account.address) + case.nonce_offset
    return sign(
        account,
        chain_id,
        nonce,
        fees,
        gas,
        DEPLOYER,
        runner.initcode,
        authorizations=[authorization],
    )


@pytest.mark.parametrize("mode", [Mode.DROPPING, Mode.STANDALONE])
async def test_only_paying_submissions_land(
    chain: LocalChain, schedule: GasSchedule, mode: Mode
) -> None:
    """In either mode, only the paying submissions land. Dropping: one bundle, the accounts
    funded by its first transaction. Standalone: a bundle a submission, the accounts topped
    up beforehand by maintenance."""
    float_ = TEST_FLOAT
    accounts = [derive(float_, i) for i in range(len(CASES))]
    await chain.set_eth_balance(float_.address, FLOAT_ETH)
    base = await chain.base_fee()
    fees = Fees(max_fee_per_gas=2 * base + 10**9, max_priority_fee_per_gas=10**9)
    txs = [
        await submission(chain, schedule, case, i, account, float_.address, fees)
        for i, (case, account) in enumerate(zip(CASES, accounts, strict=True))
    ]
    funding: SignedTransaction | None = None
    if mode is Mode.DROPPING:
        top_ups = [
            TopUp(tx.sender, shortfall(tx, 0), new_account=True) for tx in txs
        ]  # the accounts are new: no balance, no nonce
        nonce = await chain.nonce(float_.address)
        funding = sign_funding(float_, await chain.chain_id(), nonce, fees, top_ups, schedule)
    else:
        async with httpx.AsyncClient() as client:
            live = Chain(client, await chain.chain_id(), (chain.url,), ((0, schedule),))
            required = {i: tx.max_cost for i, tx in enumerate(txs)}
            topped = await replenish(live, float_, required, fees, schedule, TIMEOUT_SECONDS)
            assert topped is not None
    held = {t: await chain.balance_of(t, float_.address) for t in (USDC, USDT)}
    eth_before = await eth_held(chain, float_, accounts)
    nonces = [await chain.nonce(a.address) for a in accounts]
    builder = MockBuilder(chain)
    head = int(await chain.rpc("eth_blockNumber", []), 16)
    for params in bundles(mode, head + 1, funding, txs):
        await builder.send_bundle(params)

    block = await builder.build_block()

    landed = [tx for case, tx in zip(CASES, txs, strict=True) if case.lands]
    first = () if funding is None else (funding.hash,)
    assert block.transactions == (*first, *(tx.hash for tx in landed))
    # ETH: the float and its accounts together lose exactly the fees of what landed
    fees_paid = 0
    for h in block.transactions:
        receipt = await chain.rpc("eth_getTransactionReceipt", ["0x" + h.hex()])
        assert receipt["status"] == "0x1"
        fees_paid += int(receipt["gasUsed"], 16) * int(receipt["effectiveGasPrice"], 16)
    assert eth_before - await eth_held(chain, float_, accounts) == fees_paid
    # tokens: the float gains exactly the landed payments
    for token in (USDC, USDT):
        gained = await chain.balance_of(token, float_.address) - held[token]
        assert gained == sum(c.paid for c in CASES if c.lands and c.token is token)
    # nonces: a landed submission's account moved on; a dropped one's didn't
    for case, account, before in zip(CASES, accounts, nonces, strict=True):
        assert await chain.nonce(account.address) == before + (1 if case.lands else 0)


async def eth_held(chain: LocalChain, float_: LocalAccount, accounts: list[LocalAccount]) -> int:
    """What the float and its concurrency accounts hold together."""
    return await chain.eth_balance(float_.address) + sum(
        [await chain.eth_balance(a.address) for a in accounts]
    )
