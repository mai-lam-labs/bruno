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

The clients are the same every run, funded from the float to exactly what they spend: the
payer ends at 0, and the short client keeps its own until the sweep. A concurrency
account's leftover is its working balance."""

import asyncio
import os
from dataclasses import dataclass

import httpx
import msgspec
import pytest
from eth_account import Account
from eth_account.datastructures import SignedSetCodeAuthorization
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress, HexStr
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from bruno.accounts import derive
from bruno.builders import send_bundle
from bruno.bundle import Mode, TopUp, bundles, shortfall, sign_funding
from bruno.chain import Chain
from bruno.config import Bundler, ChainConfig, Eip712Domain, Hop, PoolKind, Token
from bruno.deployer import DEPLOYER, transaction_gas
from bruno.evm import NATIVE_ASSET, EncodedCall
from bruno.forks import GasSchedule
from bruno.maintenance import replenish
from bruno.net.jsonrpc import JsonRpcError
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
    X402_CLIENT_INDEX,
    Network,
    fund,
    hold,
    key,
)
from bruno.tests.localchain import CALIBUR
from bruno.tests.verify import (
    authorized,
    balance_of,
    decode_payment_response,
    word,
    x402_payment,
)
from bruno.tests.verify import fund as fund_token
from bruno.transactions import Fees, SignedTransaction, authorization_json, sign

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


async def provisioned(
    chain: Chain, client: LocalAccount, calls: list[EncodedCall]
) -> tuple[SignedSetCodeAuthorization, EncodedCall]:
    """`client`'s authorization to Calibur and its signed batch of `calls`. The authorization is
    at the account's nonce now: nothing in a run moves it until the authorization is applied,
    and re-delegating to Calibur changes nothing (one a stale node's nonce makes invalid is
    skipped, the account still Calibur's). The batch's nonce is a random key's first, so no
    run's batch is another's, with nothing kept between runs."""
    nonce = await chain.nonce(client.address)
    authorization = Account.sign_authorization(
        {"chainId": chain.chain_id, "address": CALIBUR, "nonce": nonce}, client.key
    )
    lane = int.from_bytes(os.urandom(24)) << 64  # Calibur: a 192-bit key, a 64-bit sequence
    return authorization, calibur_batch(client, chain.chain_id, calls, lane)


@dataclass(frozen=True, slots=True)
class Unsigned:
    """A submission's transaction before it's signed: the runner's deployment calldata, its
    authorization, and its gas."""

    data: bytes
    authorization: SignedSetCodeAuthorization
    gas: int


async def unsigned(
    chain: Chain,
    schedule: GasSchedule,
    client: LocalAccount,
    payee: ChecksumAddress,
    paid: int,
) -> Unsigned:
    """`client` provisioned through Calibur, its batch paying `paid` to `payee`, the price
    AMOUNT. The client holds ETH, so its account isn't empty."""
    authorization, batch = await provisioned(
        chain, client, [EncodedCall(payee, HexStr("0x"), paid)]
    )
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
        payer = derive(float_, PAYER_INDEX)
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
        paid_receipt = await chain.receipt(paid_tx.hash)
        assert paid_receipt is not None
        assert paid_receipt["status"] == "0x1"
        # read at the landed block: "latest" can come from a backend not yet at it
        landed = paid_receipt["blockNumber"]
        after = {
            name: int(await chain.rpc("eth_getBalance", [a, landed]), 16)
            for name, a in accounts.items()
        }
        print(f"after: { {k: v / 10**18 for k, v in after.items()} }")
        print(f"spent, all told: {(sum(before.values()) - sum(after.values())) / 10**18} ETH")

        assert await chain.receipt(short_tx.hash) is None
        assert await chain.rpc("eth_getCode", [short.address, landed]) == "0x"
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
    paying = await unsigned(chain, schedule, payer, float_.address, AMOUNT)
    unpaying = await unsigned(chain, schedule, short, float_.address, AMOUNT - 1)
    for attempt in range(run.max_blocks):
        fees = await chain.fees(PRIORITY_FEE)
        txs = [
            await submission(chain, payer_account, paying, fees),
            await submission(chain, short_account, unpaying, fees),
        ]
        funding: SignedTransaction | None = None
        if run.mode is Mode.STANDALONE:
            required = {PAYER_ACCOUNT: txs[0].max_cost, SHORT_ACCOUNT: txs[1].max_cost}
            try:
                topped = await replenish(
                    chain, float_, required, fees, schedule, RECEIPT_TIMEOUT_SECONDS
                )
            except JsonRpcError as e:  # refused (a node behind on the float's nonce): next block
                print(f"  topping up the accounts refused: {e}")
                continue
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


# the Worker's live run on Sepolia: the nflaig ePBS builder (standalone, signed), Sepolia's
# WETH9 (the configuration names one; ETH needs no pools), and how many slots the submission
# is given to land once the alarms start: the builder built 48% of Sepolia's blocks
# (2026-10-19), so 25 miss all of its with probability ~0.52^25, under 0.0001%
SEPOLIA_BUILDER = "https://builder-sepolia.nflaig.dev/rpc"
SEPOLIA_WETH = "0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14"
WORKER_SLOTS = 25
WORKER_DEADLINE_BLOCKS = 50
SLOT_SECONDS = 12
# the binding the bundler's identity key is read from, supplied by the test's setup
IDENTITY_BINDING = "BUILDER_IDENTITY"


@pytest.mark.sepolia
async def test_worker_lands_on_sepolia(workerd: str) -> None:
    """The Worker as the relay on Sepolia, under workerd: its own alarms on Sepolia's slots,
    its bundles to the nflaig ePBS builder. The paying client, given exactly the quoted amount
    of ETH, pays it all to the float through Calibur: it lands, and the client ends at 0."""
    float_ = key(SEPOLIA.wallet_key)
    identity = derive(float_, BUILDER_IDENTITY_INDEX)
    chain_id = SEPOLIA.chain_id
    config = ChainConfig(
        rpc=SEPOLIA.nodes,
        payee=float_.address,
        weth=SEPOLIA_WETH,
        bundlers=(Bundler(SEPOLIA_BUILDER, Mode.STANDALONE, identity_key=IDENTITY_BINDING),),
        tokens=(Token(NATIVE_ASSET),),
        accounts=2,
        max_pending=4,
        max_deadline_blocks=WORKER_DEADLINE_BLOCKS,
        min_fee_per_gas=PRIORITY_FEE,
        max_gas=CLIENT_GAS,
        max_call_bytes=10_000,
        quote_blocks=5,
    )
    async with (
        httpx.AsyncClient() as client,
        httpx.AsyncClient(base_url=workerd, timeout=RECEIPT_TIMEOUT_SECONDS) as worker,
    ):
        chain = SEPOLIA.chain(client)
        await chain.check_chain()
        payer = derive(float_, PAYER_INDEX)
        hooks = f"/__test/{chain_id}"
        configured = await worker.post(
            f"{hooks}/configure",
            json={
                "config": msgspec.to_builtins(config),
                "floatKey": "0x" + float_.key.hex(),
                "identities": {IDENTITY_BINDING: "0x" + identity.key.hex()},
            },
        )
        assert configured.status_code == 200, configured.text
        quote = await worker.get(
            f"/{chain_id}/v1/quote",
            params={
                "gas": hex(CLIENT_GAS),
                "callBytes": hex(2_000),
                "tokens": [NATIVE_ASSET],
                "authority": payer.address,
            },
        )
        amount = int(quote.json()["quotes"][0]["amount"], 16)
        print(f"\nquoted {amount / 10**18} ETH; payer {payer.address}")
        await fund(chain, float_, payer.address, amount)
        authorization, call = await provisioned(
            chain, payer, [EncodedCall(float_.address, HexStr("0x"), amount)]
        )
        body = {
            "call": {"to": call.to, "data": call.data},
            "authorizationList": [authorization_json(authorization)],
            "gas": hex(CLIENT_GAS),
            "payment": {"token": NATIVE_ASSET, "payee": float_.address, "amount": hex(amount)},
            "maxBlockNumber": hex(await chain.head() + WORKER_DEADLINE_BLOCKS),
        }
        taken = await worker.post(f"/{chain_id}/v1/submissions", json=body)
        assert taken.status_code == 202, taken.text
        started = await worker.post(f"{hooks}/start")
        assert started.status_code == 200, started.text
        status: dict[str, str] = {}
        try:
            for slot in range(WORKER_SLOTS):
                await asyncio.sleep(SLOT_SECONDS)
                status = (await worker.get(taken.headers["Location"])).json()
                print(f"  slot {slot + 1}: {status}")
                if status["status"] != "pending":
                    break
        finally:
            await worker.post(f"{hooks}/stop")

        assert status["status"] == "included", status
        receipt = await chain.receipt(bytes.fromhex(status["transactionHash"][2:]))
        assert receipt is not None
        assert receipt["status"] == "0x1"
        block = await chain.rpc("eth_getBlockByNumber", [receipt["blockNumber"], False])
        builder = bytes.fromhex(block["extraData"][2:]).decode(errors="replace")
        print(f"landed: {status['transactionHash']} in block {int(receipt['blockNumber'], 16)}")
        print(f"  built by {builder!r}")
        # read at the landed block: "latest" can come from a backend not yet at it
        landed = receipt["blockNumber"]
        assert int(await chain.rpc("eth_getBalance", [payer.address, landed]), 16) == 0


# Sepolia's USDC (FiatToken v2.2), its EIP-712 domain, its pool to WETH and Uniswap's
# SwapRouter02, as the default configuration has them (the float buys its USDC there); the
# client's call, its own USDC balance read, and the gas it's given
SEPOLIA_USDC = to_checksum_address("0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238")
SEPOLIA_USDC_DOMAIN = Eip712Domain("USDC", "2")
SEPOLIA_USDC_POOL = "0x6Ce0896eAE6D4BD668fDe41BB784548fb8F59b50"
SEPOLIA_ROUTER = to_checksum_address("0x3bFA4769FB09eefC5a80d6E87c3B9C650f7Ae48E")
X402_CALL_GAS = 50_000
# how much longer than maxTimeoutSeconds the held request is waited for: its answer's way back
HELD_SLACK_SECONDS = 30
AUTHORIZATION_USED = keccak(text="AuthorizationUsed(address,bytes32)")


@pytest.mark.sepolia
async def test_worker_settles_x402_on_sepolia(workerd: str) -> None:
    """The Worker on Sepolia taking an x402 payment as a deployment would: its own alarms on
    Sepolia's slots, the nflaig ePBS builder, the default x402 deadline. A client holding only
    the quoted USDC, no ETH, pays for a call (its own USDC balance read), its request held
    until the transaction lands: 200 with that transaction, in which USDC logs the
    authorization used by the client. The client ends with no USDC; the float, the payee, gets
    it back, so a run costs only gas."""
    float_ = key(SEPOLIA.wallet_key)
    identity = derive(float_, BUILDER_IDENTITY_INDEX)
    chain_id = SEPOLIA.chain_id
    config = ChainConfig(
        rpc=SEPOLIA.nodes,
        payee=float_.address,
        weth=SEPOLIA_WETH,
        bundlers=(Bundler(SEPOLIA_BUILDER, Mode.STANDALONE, identity_key=IDENTITY_BINDING),),
        tokens=(
            Token(NATIVE_ASSET),
            Token(SEPOLIA_WETH),
            Token(
                SEPOLIA_USDC,
                (Hop(PoolKind.UNISWAP_V3, SEPOLIA_USDC_POOL),),
                eip3009=SEPOLIA_USDC_DOMAIN,
            ),
        ),
        accounts=2,
        max_pending=4,
        max_deadline_blocks=WORKER_DEADLINE_BLOCKS,
        min_fee_per_gas=PRIORITY_FEE,
        max_gas=CLIENT_GAS,
        max_call_bytes=10_000,
        quote_blocks=5,
    )
    async with (
        httpx.AsyncClient() as client,
        httpx.AsyncClient(base_url=workerd, timeout=RECEIPT_TIMEOUT_SECONDS) as worker,
    ):
        chain = SEPOLIA.chain(client)
        await chain.check_chain()
        payer = derive(float_, X402_CLIENT_INDEX)
        hooks = f"/__test/{chain_id}"
        configured = await worker.post(
            f"{hooks}/configure",
            json={
                "config": msgspec.to_builtins(config),
                "floatKey": "0x" + float_.key.hex(),
                "identities": {IDENTITY_BINDING: "0x" + identity.key.hex()},
            },
        )
        assert configured.status_code == 200, configured.text
        balance_call = keccak(text="balanceOf(address)")[:4] + word(payer.address)
        body: dict[str, object] = {
            "call": {"to": SEPOLIA_USDC, "data": "0x" + balance_call.hex()},
            "gas": hex(X402_CALL_GAS),
        }
        url = f"/{chain_id}/v1/x402"
        challenge = await worker.post(url, json=body)
        assert challenge.status_code == 402, challenge.text
        accepts = next(a for a in challenge.json()["accepts"] if a["asset"] == SEPOLIA_USDC)
        amount = int(accepts["amount"])
        held = await balance_of(chain, SEPOLIA_USDC, payer.address)
        print(f"\nquoted {amount / 10**6} USDC; payer {payer.address} holds {held / 10**6}")
        if held < amount:
            await hold(
                chain,
                float_,
                SEPOLIA_USDC,
                Hop(PoolKind.UNISWAP_V3, SEPOLIA_USDC_POOL),
                to_checksum_address(SEPOLIA_WETH),
                SEPOLIA_ROUTER,
                amount - held,
            )
            await fund_token(
                chain, float_, SEPOLIA_USDC, payer.address, amount - held, RECEIPT_TIMEOUT_SECONDS
            )
        signed = authorized(
            payer, SEPOLIA_USDC_DOMAIN, chain_id, SEPOLIA_USDC, float_.address, amount
        )
        started = await worker.post(f"{hooks}/start")
        assert started.status_code == 200, started.text
        try:
            paid = await worker.post(
                url,
                json=body,
                headers={"PAYMENT-SIGNATURE": x402_payment(accepts, signed)},
                timeout=int(accepts["maxTimeoutSeconds"]) + HELD_SLACK_SECONDS,
            )
            status: dict[str, str] = {}
            if paid.is_success:
                status = (await worker.get(paid.headers["Location"])).json()
        finally:
            await worker.post(f"{hooks}/stop")

        assert paid.status_code == 200, (paid.status_code, paid.text, paid.headers)
        transaction = decode_payment_response(paid.headers["PAYMENT-RESPONSE"])["transaction"]
        print(f"settled: {transaction}; status {status}")
        assert status["transactionHash"] == transaction
        receipt = await chain.receipt(bytes.fromhex(transaction[2:]))
        assert receipt is not None
        assert receipt["status"] == "0x1"
        used = [
            log
            for log in receipt["logs"]
            if log["address"].lower() == SEPOLIA_USDC.lower()
            and bytes.fromhex(log["topics"][0][2:]) == AUTHORIZATION_USED
        ]
        assert [log["topics"][1][-40:] for log in used] == [payer.address[2:].lower()]
        block = await chain.rpc("eth_getBlockByNumber", [receipt["blockNumber"], False])
        builder = bytes.fromhex(block["extraData"][2:]).decode(errors="replace")
        print(f"  in block {int(receipt['blockNumber'], 16)}, built by {builder!r}")
        # read at the landed block: "latest" can come from a backend not yet at it
        landed = receipt["blockNumber"]
        assert await balance_of(chain, SEPOLIA_USDC, payer.address, landed) == 0
