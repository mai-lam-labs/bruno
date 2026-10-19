"""The relay's bundles on a live testnet, through a real builder (`-m hoodi` or `-m sepolia`:
spends testnet ETH, run only when asked, `make check-hoodi` / `make check-sepolia`).

The wallet is the float. Each block, until it lands, two submissions, each a client
provisioned through Calibur paying the float in native ETH from its signed batch: one pays
the price, one a wei short. The block must hold the payer's and not the other's: its
runner reverts Underpaid(), so it's left out, costing nothing. In the run's mode:

- dropping (Hoodi, Titan): one bundle, the float's funding transaction first while a
  concurrency account is short, both submissions droppable;
- standalone (Sepolia, the nflaig ePBS builder, which takes no droppingTxHashes): each
  submission alone, its account topped up beforehand by maintenance.

The clients are funded from the float to exactly what they spend: the payer ends at 0, and
the short client, which never sends, keeps its balance for the next run. A concurrency
account's leftover is its working balance."""

import asyncio
from dataclasses import dataclass

import httpx
import pytest
from eth_account import Account
from eth_account.datastructures import SignedSetCodeAuthorization
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress, HexStr

from bruno.accounts import derive
from bruno.builders import send_bundle
from bruno.bundle import Mode, TopUp, bundles, shortfall, sign_funding
from bruno.chain import Chain
from bruno.deployer import DEPLOYER, transaction_gas
from bruno.evm import EncodedCall
from bruno.forks import GasSchedule
from bruno.maintenance import replenish
from bruno.runner import Payment, in_call_runner
from bruno.tests.calibur import calibur_batch
from bruno.tests.deployments import CLIENT_GAS
from bruno.tests.live import (
    BUILDER_IDENTITY_INDEX,
    HOODI,
    PAYER_INDEX,
    PRIORITY_FEE,
    RECEIPT_TIMEOUT_SECONDS,
    SEPOLIA,
    SHORT_CLIENT_INDEX,
    Network,
    fund,
    key,
)
from bruno.tests.localchain import CALIBUR
from bruno.transactions import Fees, SignedTransaction, sign

# the price the payer pays, in wei: a choice, over a submission's gas at testnet fees
AMOUNT = 10**15
# how often the chain is read while waiting for the target block: an engineering bound
POLL_SECONDS = 2
# the concurrency accounts the payer's and the short payer's transactions come from
PAYER_ACCOUNT, SHORT_ACCOUNT = 0, 1


@dataclass(frozen=True, slots=True)
class Run:
    """A network, its builder, and the mode its bundles go in. `max_blocks` bounds a run:
    Titan builds ~1 Hoodi block in 23 (relayscan, 2026-10-19), so 150 misses all its blocks
    with probability ~e^(-150/23), 0.15%; the nflaig builder built 48% of Sepolia's
    (2026-10-19), so 20 misses all of its with probability ~0.52^20, under 0.001%."""

    network: Network
    builder: str
    mode: Mode
    signed: bool
    max_blocks: int


RUNS = [
    pytest.param(
        Run(HOODI, "https://rpc-hoodi.titanbuilder.xyz/", Mode.DROPPING, False, 150),
        marks=pytest.mark.hoodi,
        id="hoodi-titan-dropping",
    ),
    pytest.param(
        Run(SEPOLIA, "https://builder-sepolia.nflaig.dev/rpc", Mode.STANDALONE, True, 20),
        marks=pytest.mark.sepolia,
        id="sepolia-nflaig-standalone",
    ),
]


async def fresh_payer(chain: Chain, float_: LocalAccount) -> LocalAccount:
    """The first paying client from PAYER_INDEX that has never sent or been delegated."""
    i = PAYER_INDEX
    while True:
        client = derive(float_, i)
        if await chain.nonce(client.address) == 0 and await chain.code(client.address) == "0x":
            return client
        i += 1


@dataclass(frozen=True, slots=True)
class Unsigned:
    """A submission's transaction before it's signed: the runner's deployment calldata, its
    authorization, and its gas."""

    data: bytes
    authorization: SignedSetCodeAuthorization
    gas: int


def unsigned(
    chain: Chain,
    schedule: GasSchedule,
    client: LocalAccount,
    payee: ChecksumAddress,
    paid: int,
) -> Unsigned:
    """`client` provisioned through Calibur, its batch paying `paid` to `payee`, the price
    AMOUNT. The client holds ETH, so its account isn't empty."""
    authorization = Account.sign_authorization(
        {"chainId": chain.chain_id, "address": CALIBUR, "nonce": 0}, client.key
    )
    batch = calibur_batch(client, chain.chain_id, [EncodedCall(payee, HexStr("0x"), paid)])
    runner = in_call_runner(
        batch, Payment(None, payee, AMOUNT), schedule, delegated={client.address}
    )
    gas = transaction_gas(runner.initcode, runner.frame_gas(CLIENT_GAS), schedule, 1)
    return Unsigned(runner.initcode, authorization, gas)


async def submission(
    chain: Chain, account: LocalAccount, tx: Unsigned, fees: Fees
) -> SignedTransaction:
    """`tx` signed by `account`, at its nonce now."""
    nonce = await chain.nonce(account.address)
    authorizations = [tx.authorization]
    return sign(
        account,
        chain.chain_id,
        nonce,
        fees,
        tx.gas,
        DEPLOYER,
        tx.data,
        authorizations=authorizations,
    )


@pytest.mark.parametrize("run", RUNS)
async def test_bundle_lands_without_its_non_payer(run: Run) -> None:
    async with httpx.AsyncClient() as client:
        chain = run.network.chain(client)
        await chain.check_chain()
        float_ = key(run.network.wallet_key)
        short = derive(float_, SHORT_CLIENT_INDEX)
        payer = await fresh_payer(chain, float_)
        accounts = {
            "float": float_.address,
            "account 0": derive(float_, PAYER_ACCOUNT).address,
            "account 1": derive(float_, SHORT_ACCOUNT).address,
            "payer": payer.address,
            "short payer": short.address,
        }
        before = {name: await chain.balance(a) for name, a in accounts.items()}
        print(f"\nbefore: { {k: v / 10**18 for k, v in before.items()} }")
        await fund(chain, float_, payer.address, AMOUNT)
        await fund(chain, float_, short.address, AMOUNT - 1)
        paid_tx, short_tx, funding = await run_until_landed(
            run, chain, client, float_, payer, short
        )
        after = {name: await chain.balance(a) for name, a in accounts.items()}
        print(f"after: { {k: v / 10**18 for k, v in after.items()} }")
        print(f"spent, all told: {(sum(before.values()) - sum(after.values())) / 10**18} ETH")

        paid_receipt = await chain.receipt(paid_tx.hash)
        assert paid_receipt is not None
        assert paid_receipt["status"] == "0x1"
        assert await chain.receipt(short_tx.hash) is None
        assert await chain.code(short.address) == "0x"
        if funding is not None:
            funding_receipt = await chain.receipt(funding.hash)
            assert funding_receipt is not None
            assert funding_receipt["blockNumber"] == paid_receipt["blockNumber"]
        # no dust: the payer spent exactly what it was given; the short payer kept its own
        assert after["payer"] == 0
        assert after["short payer"] == AMOUNT - 1


async def run_until_landed(
    run: Run,
    chain: Chain,
    client: httpx.AsyncClient,
    float_: LocalAccount,
    payer: LocalAccount,
    short: LocalAccount,
) -> tuple[SignedTransaction, SignedTransaction, SignedTransaction | None]:
    """Send the bundles for each next block until the payer's transaction lands: it, the
    short payer's, and the funding sent with it (dropping mode)."""
    identity = derive(float_, BUILDER_IDENTITY_INDEX) if run.signed else None
    payer_account, short_account = derive(float_, PAYER_ACCOUNT), derive(float_, SHORT_ACCOUNT)
    schedule = await chain.schedule()
    print(f"  gas schedule: {schedule.name}")
    paying = unsigned(chain, schedule, payer, float_.address, AMOUNT)
    unpaying = unsigned(chain, schedule, short, float_.address, AMOUNT - 1)
    for attempt in range(run.max_blocks):
        fees = await chain.fees(PRIORITY_FEE)
        txs = [
            await submission(chain, payer_account, paying, fees),
            await submission(chain, short_account, unpaying, fees),
        ]
        funding: SignedTransaction | None = None
        if run.mode is Mode.STANDALONE:
            required = {PAYER_ACCOUNT: txs[0].max_cost, SHORT_ACCOUNT: txs[1].max_cost}
            topped = await replenish(
                chain, float_, required, fees, schedule, RECEIPT_TIMEOUT_SECONDS
            )
            if topped is not None:
                print(f"  topped up the accounts: 0x{topped.hash.hex()}")
        else:
            top_ups: list[TopUp] = []
            for tx in txs:
                balance = await chain.balance(tx.sender)
                if (need := shortfall(tx, balance)) > 0:
                    new = balance == 0 and await chain.nonce(tx.sender) == 0
                    top_ups.append(TopUp(tx.sender, need, new_account=new))
            if top_ups:
                nonce = await chain.nonce(float_.address)
                funding = sign_funding(float_, chain.chain_id, nonce, fees, top_ups, schedule)
        target = await chain.head() + 1
        for params in bundles(run.mode, target, funding, txs):
            result = await send_bundle(client, run.builder, params, identity)
            print(f"block {target} (attempt {attempt + 1}): {result}")
        # the head and the block may come from different backends of one node: wait for the
        # block itself
        while (block := await chain.rpc("eth_getBlockByNumber", [hex(target), False])) is None:
            await asyncio.sleep(POLL_SECONDS)
        builder = bytes.fromhex(block["extraData"][2:]).decode(errors="replace")
        mined = {bytes.fromhex(h[2:]) for h in block["transactions"]}
        ours = [
            name
            for name, tx in (("funding", funding), ("payer", txs[0]), ("short payer", txs[1]))
            if tx is not None and tx.hash in mined
        ]
        print(f"  built by {builder!r}: ours in it: {ours or 'none'}")
        if txs[0].hash in mined:
            return txs[0], txs[1], funding
    raise AssertionError(f"the bundle didn't land in {run.max_blocks} blocks")
