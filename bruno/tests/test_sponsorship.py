"""A sponsorship planned and landed on anvil, each transaction of the relay's signed at exactly
its planned gas: an account holding a token and no ETH, its own transactions between the
relay's two; and one whose delegate's code pays, with no transaction of its own. And one taken
by the relay over its interface, priced, sent through the mock builder as a bundle that lands
whole, or refused, against both relays."""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

import httpx
import pytest
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress

from bruno.accounts import derive
from bruno.chain import Chain, risen_base_fee
from bruno.evm import NATIVE_ASSET, EncodedCall
from bruno.forks import NONZERO_BYTE_GAS, ZERO_BYTE_GAS, GasSchedule
from bruno.forwarder import forwarder_of
from bruno.measurements import Measurements, measure
from bruno.plan import CheckedByForwarder, Plan, Step, make_plan, transaction_json
from bruno.runner import UNDERPAID
from bruno.server.wire import Code
from bruno.simulation import SignedSimulation, simulations
from bruno.sponsorship import Sponsorship
from bruno.tests.calibur import calibur_batch, fresh_client
from bruno.tests.deployments import transfer
from bruno.tests.interface import quoting, rpc, wallet_signed
from bruno.tests.localchain import CALIBUR, PAYEE_ACCOUNT, TEST_FLOAT, USDC, USDT, LocalChain
from bruno.tests.served import Served, Setup, relay_config
from bruno.tokens import balance_of_call
from bruno.transactions import Decoded, Fees, authorization_json, decoded, sign

# the relay's account: a concurrency account's place, funded
RELAY = derive(TEST_FLOAT, 400)
AMOUNT = 2_000_000
ACTION = 500_000
TIP = 10**9
DEADLINE_BLOCKS = 10
# the most an amount's word adds to a transfer's calldata gas over a unit's: every byte nonzero
AMOUNT_WORD_GAS = 32 * (NONZERO_BYTE_GAS - ZERO_BYTE_GAS)


async def measured_for(chain: LocalChain, live: Chain, payee: ChecksumAddress) -> Measurements:
    """The chain as the relay measures it for `payee`, whose forwarder is deployed first."""
    config = relay_config(Setup(), chain.url, payee, "http://127.0.0.1:9/")
    return await measure(live, config, payee, forwarder_of(payee).address)


async def signed_by(
    chain: LocalChain,
    user: LocalAccount,
    nonce: int,
    call: EncodedCall,
    max_fee: int,
    *,
    gas: int | None = None,
    chain_id: int | None = None,
    value: int = 0,
) -> Decoded:
    """`call` as `user`'s wallet signs it, without sending it: its gas the node's estimate,
    unless it's given; for this chain, unless another is; sending `value`."""
    asked = {"from": user.address, "to": call.to, "data": "0x" + call.data.hex()}
    if gas is None:
        gas = int(await chain.rpc("eth_estimateGas", [asked]), 16)
    tx = {
        "type": 2,
        "chainId": await chain.chain_id() if chain_id is None else chain_id,
        "nonce": nonce,
        "to": call.to,
        "data": "0x" + call.data.hex(),
        "value": value,
        "gas": gas,
        "maxFeePerGas": max_fee,
        "maxPriorityFeePerGas": min(TIP, max_fee),
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


def sponsored(
    forwarder: str, amount: int, deadline: int, transactions: Sequence[Decoded]
) -> dict[str, object]:
    """A sponsorship as submitted: its account's transactions as signed, and its payment in
    USDC."""
    return {
        "account": transactions[0].signed.sender,
        "transactions": ["0x" + t.signed.raw.hex() for t in transactions],
        "payment": {"token": USDC.address, "payee": forwarder, "amount": hex(amount)},
        "maxBlockNumber": hex(deadline),
    }


@dataclass(frozen=True, slots=True)
class Signing:
    """What a test's account signs a sponsorship with: where it pays, its deadline, and the max
    fee a block may ask by then."""

    forwarder: ChecksumAddress
    deadline: int
    max_fee: int


async def signing(served: Served) -> Signing:
    latest = await served.latest()
    return Signing(
        forwarder_of(served.payee).address,
        latest.number + DEADLINE_BLOCKS,
        risen_base_fee(latest.next_base_fee, DEADLINE_BLOCKS),
    )


async def test_a_sponsorship_is_taken_at_its_price_and_lands_whole(
    chain: LocalChain, served: Served
) -> None:
    """An account holding USDC and no ETH submits a transfer of its own and its payment, each
    signed. Paying too little it's refused, underpaid, told what's required; paying that, it's
    taken, and the next block holds its four transactions together: the relay's funding, the
    account's two, the forwarder's call. Its status is included, its transaction the last. The
    payee gained what was required (less the forwarder's unit) and the account's transfer was
    made as its own."""
    user = fresh_client("sponsored through the relay")
    friend = await chain.account(3)
    await chain.mint(USDC, user.address, 10**9)
    held = await chain.balance_of(USDC, served.payee), await chain.balance_of(USDC, friend)
    by = await signing(served)
    acting = await signed_by(chain, user, 0, transfer(USDC, friend, ACTION), by.max_fee)
    # its gas whatever it pays: a unit's estimate, and what an amount with no zero byte adds
    probe = await signed_by(chain, user, 1, transfer(USDC, by.forwarder, 1), by.max_fee)
    gas = probe.signed.gas + AMOUNT_WORD_GAS
    paying = await signed_by(chain, user, 1, transfer(USDC, by.forwarder, 1), by.max_fee, gas=gas)
    url = f"/{served.chain_id}/v1/sponsorships"

    async with served.interface() as http:
        short = await http.post(url, json=sponsored(by.forwarder, 1, by.deadline, [acting, paying]))
        error = short.json()["error"]
        assert error["data"]["reason"] == "underpaid", short.text
        required = int(error["data"]["required"], 16)
        # the same gas, so the same price: only what it pays changed
        paying = await signed_by(
            chain,
            user,
            1,
            transfer(USDC, by.forwarder, required),
            by.max_fee,
            gas=gas,
        )
        body = sponsored(by.forwarder, required, by.deadline, [acting, paying])
        taken = await http.post(url, json=body)
        assert taken.status_code == 202, taken.text
        block = await served.land()
        status = (await http.get(taken.headers["Location"])).json()

    # its status is a submission's
    assert taken.headers["Location"].endswith(f"/v1/submissions/{taken.json()['id']}")
    assert len(block.transactions) == 4, block
    assert block.transactions[1:3] == (acting.signed.hash, paying.signed.hash)
    assert status["status"] == "included", status
    # every transaction of it, in order: the relay's two and the account's own between them
    assert status["transactionHashes"] == ["0x" + h.hex() for h in block.transactions]
    assert await chain.balance_of(USDC, served.payee) == held[0] + required - 1
    assert await chain.balance_of(USDC, friend) == held[1] + ACTION


@pytest.mark.parametrize(
    ("wrong", "field", "reason"),
    [
        ("pays the payee", None, "wrong-payee"),
        ("a max fee a block may ask more than", "transactions[1]", "transaction-fee"),
        ("another chain's transaction", "transactions[1]", "transaction-chain"),
        ("not a transaction", "transactions[1]", "transaction-undecodable"),
        ("another account's transaction", "transactions[1]", "sponsored-not-accounts"),
        ("another account named", "transactions[0]", "sponsored-not-accounts"),
        ("a call into another account", "call", "sponsored-not-accounts"),
        ("a call into the account given no gas", "gas", "sponsored-no-gas"),
        ("a signed transfer in its payment", "payment.transfer", "malformed"),
        ("a submission's field", "deployment", "malformed"),
    ],
    ids=lambda v: v if isinstance(v, str) and " " in v else "",
)
async def test_a_sponsorship_that_isnt_one_is_refused(
    chain: LocalChain, served: Served, wrong: str, field: str | None, reason: str
) -> None:
    """A sponsorship is refused, naming the field and why: paying the payee where it's the
    forwarder that's paid; a transaction a block could ask more of by the deadline, one signed
    for another chain, one that isn't a transaction, one another account signed; an account
    named that didn't sign them, or a call into some other; a call into the account with no gas
    stated for it; and a field a sponsorship doesn't have, refused where it would have been
    ignored: a signed transfer in its payment, a submission's deployment."""
    user, other = fresh_client("refused its sponsorship"), fresh_client("someone else")
    await chain.mint(USDC, user.address, 10**9)
    await chain.mint(USDC, other.address, 10**9)
    by = await signing(served)
    payment = transfer(USDC, by.forwarder, AMOUNT)
    acting = await signed_by(chain, user, 0, transfer(USDC, await chain.account(3), 1), by.max_fee)
    paying = await signed_by(chain, user, 1, payment, by.max_fee)
    payee: str = by.forwarder
    account: str = user.address
    extra: dict[str, object] = {}
    payment_extra: dict[str, object] = {}
    raws = ["0x" + t.signed.raw.hex() for t in (acting, paying)]
    if wrong == "pays the payee":
        payee = served.payee
    elif wrong == "a max fee a block may ask more than":
        low = await signed_by(chain, user, 1, payment, by.max_fee - 1)
        raws[1] = "0x" + low.signed.raw.hex()
    elif wrong == "another chain's transaction":
        elsewhere = await signed_by(chain, user, 1, payment, by.max_fee, chain_id=1)
        raws[1] = "0x" + elsewhere.signed.raw.hex()
    elif wrong == "not a transaction":
        raws[1] = raws[1][:-2]
    elif wrong == "another account's transaction":
        raws[1] = "0x" + (await signed_by(chain, other, 1, payment, by.max_fee)).signed.raw.hex()
    elif wrong == "another account named":
        account = other.address
    elif wrong == "a call into another account":
        extra = {"call": {"to": other.address, "data": "0x"}, "gas": "0x0"}
    elif wrong == "a call into the account given no gas":
        extra = {"call": {"to": user.address, "data": "0x"}}
    elif wrong == "a signed transfer in its payment":
        payment_extra = {"transfer": None}
    else:
        extra = {"deployment": None}
    body = {
        "account": account,
        "transactions": raws,
        "payment": {
            "token": USDC.address,
            "payee": payee,
            "amount": hex(10**9),
            **payment_extra,
        },
        "maxBlockNumber": hex(by.deadline),
        **extra,
    }

    async with served.interface() as http:
        refused = await http.post(f"/{served.chain_id}/v1/sponsorships", json=body)

    data = refused.json()["error"]["data"]
    assert data["reason"] == reason, refused.text
    assert data.get("field") == field, refused.text


async def test_an_account_has_one_sponsorship_at_a_time(chain: LocalChain, served: Served) -> None:
    """A second sponsorship of an account whose first is pending is refused, to be tried again:
    its transactions' nonces are the first's. Once the first has landed, it's taken."""
    user = fresh_client("sponsored twice")
    await chain.mint(USDC, user.address, 10**9)
    by = await signing(served)
    url = f"/{served.chain_id}/v1/sponsorships"
    paid = 10**8

    async def paying(nonce: int, amount: int) -> dict[str, object]:
        signed = await signed_by(
            chain, user, nonce, transfer(USDC, by.forwarder, amount), by.max_fee
        )
        return sponsored(by.forwarder, amount, by.deadline, [signed])

    async with served.interface() as http:
        first = await http.post(url, json=await paying(0, paid))
        second = await http.post(url, json=await paying(0, paid + 1))
        await served.land()
        after = await http.post(url, json=await paying(1, paid))

    assert first.status_code == 202, first.text
    assert second.json()["error"]["data"] == {
        "category": "retry",
        "reason": "sponsored-in-flight",
    }, second.text
    assert after.status_code == 202, after.text


async def problems(http: httpx.AsyncClient) -> list[str]:
    """The chain's problems, as the relay's root lists them."""
    root = (await http.get("/", headers={"Accept": "application/json"})).json()
    found: list[str] = root["chains"][0].get("problems", [])
    return found


@dataclass(frozen=True, slots=True)
class Taken:
    """A sponsorship a test's account had taken: its id, what it pays, the account's two
    transactions, and what the payee held before."""

    id: str
    amount: int
    transactions: tuple[Decoded, Decoded]
    payee_held: int


async def taken_paying(
    chain: LocalChain, served: Served, http: httpx.AsyncClient, user: LocalAccount
) -> Taken:
    """`user`, holding USDC and no ETH, has a sponsorship taken: a transfer of its own and its
    payment of what's required, learnt from a submission paying too little."""
    await chain.mint(USDC, user.address, 10**9)
    held = await chain.balance_of(USDC, served.payee)
    by = await signing(served)
    url = f"/{served.chain_id}/v1/sponsorships"
    friend = await chain.account(3)
    acting = await signed_by(chain, user, 0, transfer(USDC, friend, ACTION), by.max_fee)
    probe = await signed_by(chain, user, 1, transfer(USDC, by.forwarder, 1), by.max_fee)
    gas = probe.signed.gas + AMOUNT_WORD_GAS
    short = await http.post(url, json=sponsored(by.forwarder, 1, by.deadline, [acting, probe]))
    required = int(short.json()["error"]["data"]["required"], 16)
    payment = transfer(USDC, by.forwarder, required)
    paying = await signed_by(chain, user, 1, payment, by.max_fee, gas=gas)
    # the probe's gas is under the payment's, so what's required of it is at most that
    body = sponsored(by.forwarder, required, by.deadline, [acting, paying])
    again = await http.post(url, json=body)
    if again.status_code != 202:
        required = int(again.json()["error"]["data"]["required"], 16)
        paying = await signed_by(
            chain, user, 1, transfer(USDC, by.forwarder, required), by.max_fee, gas=gas
        )
        again = await http.post(
            url, json=sponsored(by.forwarder, required, by.deadline, [acting, paying])
        )
    assert again.status_code == 202, again.text
    return Taken(again.json()["id"], required, (acting, paying), held)


async def test_a_broken_bundle_is_rejected_and_nothing_more_is_sent(
    chain: LocalChain, served: Served
) -> None:
    """A builder lands a sponsorship's first transaction and nothing else: the account holds
    the relay's ETH and the payee is unpaid. A sponsorship lands whole or not at all, so the
    relay stops: it's rejected, its bundle broken, and listed with the chain's problems with the
    ETH it sent. The blocks after hold nothing of it: the account's transactions aren't sent
    again, the account is sent nothing more, and the payee stays unpaid."""
    user = fresh_client("sponsored through a builder that breaks a bundle")

    async with served.interface() as http:
        taken = await taken_paying(chain, served, http, user)
        served.builder.breaking = True
        broken = await served.land()
        funded = await chain.eth_balance(user.address)
        status = (await http.get(f"/{served.chain_id}/v1/submissions/{taken.id}")).json()
        listed = await problems(http)
        served.builder.breaking = False
        after = [await served.land() for _ in range(2)]

    assert len(broken.transactions) == 1, broken
    assert funded > 0
    assert status["status"] == "rejected", status
    assert status["reason"] == "bundle-broken", status
    assert status["transactionHashes"] == ["0x" + broken.transactions[0].hex()]
    assert listed == [
        f"a bundle broken: {taken.id}: 1 of its 2 transactions landed, {funded} wei sent"
    ]
    assert all(block.transactions == () for block in after), after
    assert await chain.eth_balance(user.address) == funded
    assert await chain.nonce(user.address) == 0
    assert await chain.balance_of(USDC, served.payee) == taken.payee_held


async def test_a_sponsorship_signed_as_quoted_lands(chain: LocalChain, served: Served) -> None:
    """An account holding USDC and no ETH asks what a transfer of its own would cost,
    unsigned. The quote fills it in, and offers the payment after it. Signed as they are and
    submitted paying the quote's amount to the quote's forwarder, by the quote's deadline, the
    sponsorship is taken and lands whole: nothing was estimated or guessed by the client."""
    user = fresh_client("sponsored as quoted")
    friend = await chain.account(3)
    await chain.mint(USDC, user.address, 10**9)
    held = await chain.balance_of(USDC, served.payee), await chain.balance_of(USDC, friend)
    acting = transfer(USDC, friend, ACTION)
    asking = {
        "tokens": [USDC.address],
        "maxBlockNumber": hex(await served.head() + DEADLINE_BLOCKS),
        "account": user.address,
        "transactions": [{"to": acting.to, "data": "0x" + acting.data.hex()}],
    }

    async with served.interface() as http:
        quoted = await http.post(f"/{served.chain_id}/v1/sponsorships/quote", json=asking)
        assert quoted.status_code == 200, quoted.text
        quote = quoted.json()
        (in_usdc,) = quote["quotes"]
        to_sign = [*quote["transactions"], in_usdc["payment"]]
        body = {
            "maxBlockNumber": quote["maxBlockNumber"],
            "payment": {
                "token": USDC.address,
                "payee": quote["forwarder"],
                "amount": in_usdc["amount"],
            },
            "account": user.address,
            "transactions": [wallet_signed(user, t) for t in to_sign],
        }
        taken = await http.post(f"/{served.chain_id}/v1/sponsorships", json=body)
        assert taken.status_code == 202, taken.text
        block = await served.land()
        status = (await http.get(f"/{served.chain_id}/v1/submissions/{taken.json()['id']}")).json()

    amount = int(in_usdc["amount"], 16)
    assert quote["forwarder"] == forwarder_of(served.payee).address
    assert [int(t["nonce"], 16) for t in to_sign] == [0, 1]
    assert len(block.transactions) == 4, block
    assert status["status"] == "included", status
    assert await chain.balance_of(USDC, served.payee) == held[0] + amount - 1
    assert await chain.balance_of(USDC, friend) == held[1] + ACTION


async def test_an_account_delegated_already_is_quoted_the_gas_its_code_takes(
    chain: LocalChain, served: Served
) -> None:
    """An account delegated to Calibur already, holding USDC and no ETH, asks what one transfer
    of its own costs. Receiving the relay's funding runs its delegate's code, so the quote
    answers the gas the relay's first transaction is given for that, though no call or
    authorization was asked about. Submitted without it, the sponsorship is refused, the gas
    missing; with it, it lands whole."""
    user = fresh_client("sponsored, delegated already")
    friend = await chain.account(3)
    await chain.rpc("anvil_setCode", [user.address, "0xef0100" + CALIBUR[2:].lower()])
    await chain.mint(USDC, user.address, 10**9)
    held = await chain.balance_of(USDC, served.payee), await chain.balance_of(USDC, friend)
    acting = transfer(USDC, friend, ACTION)
    asking = {
        "tokens": [USDC.address],
        "maxBlockNumber": hex(await served.head() + DEADLINE_BLOCKS),
        "account": user.address,
        "transactions": [{"to": acting.to, "data": "0x" + acting.data.hex()}],
    }

    async with served.interface() as http:
        quoted = await http.post(f"/{served.chain_id}/v1/sponsorships/quote", json=asking)
        assert quoted.status_code == 200, quoted.text
        quote = quoted.json()
        (in_usdc,) = quote["quotes"]
        to_sign = [*quote["transactions"], in_usdc["payment"]]
        body = {
            "maxBlockNumber": quote["maxBlockNumber"],
            "payment": {
                "token": USDC.address,
                "payee": quote["forwarder"],
                "amount": in_usdc["amount"],
            },
            "account": user.address,
            "transactions": [wallet_signed(user, t) for t in to_sign],
        }
        # its code runs on receiving the relay's ETH: submitted with no gas for it, it's refused
        # for that, not taken to a simulation that fails
        unstated = await http.post(f"/{served.chain_id}/v1/sponsorships", json=body)
        body["gas"] = quote["gas"]
        taken = await http.post(f"/{served.chain_id}/v1/sponsorships", json=body)
        assert taken.status_code == 202, taken.text
        block = await served.land()
        status = (await http.get(f"/{served.chain_id}/v1/submissions/{taken.json()['id']}")).json()

    assert int(quote["gas"], 16) > 0
    assert unstated.status_code == 400, unstated.text
    assert unstated.json()["error"]["data"] == {"reason": "sponsored-no-gas", "field": "gas"}
    assert len(block.transactions) == 4, block
    assert status["status"] == "included", status
    assert await chain.balance_of(USDC, served.payee) == held[0] + int(in_usdc["amount"], 16) - 1
    assert await chain.balance_of(USDC, friend) == held[1] + ACTION


async def test_a_delegates_payment_is_quoted_and_lands(chain: LocalChain, served: Served) -> None:
    """An account holding USDC and nothing else asks what delegating to Calibur and running a
    batch of its own would cost, with a batch paying the forwarder a unit. The quote gives the
    amount and the gas the call needs, and offers no payment, there being no transaction of
    the account's. The batch signed again paying the amount, the sponsorship is taken with the
    quote's gas and lands as the relay's two transactions; the account was sent no ETH."""
    user = fresh_client("sponsored by its delegate's code, as quoted")
    forwarder = forwarder_of(served.payee).address
    chain_id = served.chain_id
    await chain.mint(USDC, user.address, 10**9)
    held = await chain.balance_of(USDC, served.payee)
    signing_it = {"chainId": chain_id, "address": CALIBUR, "nonce": 0}
    authorization = authorization_json(Account.sign_authorization(signing_it, user.key))
    deadline = hex(await served.head() + DEADLINE_BLOCKS)

    def calling(amount: int) -> dict[str, str]:
        call = calibur_batch(user, chain_id, [transfer(USDC, forwarder, amount)])
        return {"to": call.to, "data": "0x" + call.data.hex()}

    async with served.interface() as http:
        asking = {
            "tokens": [USDC.address],
            "maxBlockNumber": deadline,
            "account": user.address,
            "authorizationList": [authorization],
            "call": calling(1),
        }
        quote = (await rpc(http, chain_id, "postpaid_quoteSponsorship", [asking]))["result"]
        (in_usdc,) = quote["quotes"]
        amount = int(in_usdc["amount"], 16)
        body = {
            "maxBlockNumber": quote["maxBlockNumber"],
            "payment": {"token": USDC.address, "payee": forwarder, "amount": hex(amount)},
            "account": user.address,
            "authorizationList": [authorization],
            "call": calling(amount),
            "gas": quote["gas"],
        }
        taken = (await rpc(http, chain_id, "postpaid_sponsor", [body]))["result"]
        block = await served.land()
        status = (await rpc(http, chain_id, "postpaid_getStatus", [taken["id"]]))["result"]

    assert "payment" not in in_usdc
    assert quote["transactions"] == []
    assert len(block.transactions) == 2, block
    assert status["status"] == "included", status
    assert await chain.eth_balance(user.address) == 0
    assert await chain.balance_of(USDC, served.payee) == held + amount - 1


@pytest.mark.parametrize(
    ("wrong", "field", "reason"),
    [
        ("a sender of its own", "transactions[0].from", "malformed"),
        ("a nonce of its own", "transactions[0].nonce", "malformed"),
        ("a call's quote's field", "payer", "malformed"),
        ("no account", "account", "malformed"),
        ("another account's signed transaction", "transactions[1]", "sponsored-not-accounts"),
        ("nothing to sponsor", "transactions", "sponsored-nothing"),
    ],
    ids=lambda v: v if isinstance(v, str) and " " in v else "",
)
async def test_a_sponsorships_quote_that_isnt_one_is_refused(
    chain: LocalChain, served: Served, wrong: str, field: str, reason: str
) -> None:
    """A sponsorship's quote is refused, naming the field and why: a transaction to sign given
    a sender or a nonce, which are the account's and the quote's to give, where they'd have
    been ignored; a field a call's quote has and a sponsorship's doesn't; no account named; a
    transaction another account signed; and nothing to sponsor."""
    user, other = fresh_client("refused its quote"), fresh_client("someone else")
    by = await signing(served)
    acting = transfer(USDC, await chain.account(3), 1)
    unsigned: dict[str, object] = {"to": acting.to, "data": "0x" + acting.data.hex()}
    transactions: list[object] = [unsigned]
    asking: dict[str, object] = {
        "tokens": [USDC.address],
        "account": user.address,
        "transactions": transactions,
    }
    if wrong == "a sender of its own":
        unsigned["from"] = user.address
    elif wrong == "a nonce of its own":
        unsigned["nonce"] = "0x0"
    elif wrong == "a call's quote's field":
        asking["payer"] = user.address
    elif wrong == "no account":
        del asking["account"]
    elif wrong == "another account's signed transaction":
        theirs = await signed_by(chain, other, 1, acting, by.max_fee, gas=100_000)
        transactions.append("0x" + theirs.signed.raw.hex())
    else:
        transactions.clear()

    async with served.interface() as http:
        refused = await http.post(f"/{served.chain_id}/v1/sponsorships/quote", json=asking)

    data = refused.json()["error"]["data"]
    assert data["reason"] == reason, refused.text
    assert data["field"] == field, refused.text


@pytest.mark.parametrize("short_of", ["gas for its code", "ETH to pay with"])
async def test_a_sponsorship_its_account_cant_pay_for_is_the_clients_to_hear_of(
    chain: LocalChain, served: Served, short_of: str
) -> None:
    """A sponsorship signed as quoted, whose account can't pay for one of its transactions
    when they're run, is refused as the client's mistake, saying which, where the node's own
    answer (a sender without funds) read as the node failing, to be tried again. An account
    with code, given too little gas for it: the relay's first transaction fails as the account
    is sent its ETH, so nothing funds the rest, and the answer is that transaction's failed
    simulation. An account holding no ETH that pays in ETH: the relay sends it its
    transactions' gas, not what one of them pays the forwarder, and the answer says so."""
    user = fresh_client(f"sponsored, short of {short_of}")
    friend = await chain.account(3)
    await chain.mint(USDC, user.address, 10**9)
    token = USDC.address
    if short_of == "gas for its code":
        await chain.rpc("anvil_setCode", [user.address, "0xef0100" + CALIBUR[2:].lower()])
    else:
        token = NATIVE_ASSET
    acting = transfer(USDC, friend, ACTION)
    asking = {
        "tokens": [token],
        "account": user.address,
        "transactions": [{"to": acting.to, "data": "0x" + acting.data.hex()}],
    }

    async with served.interface() as http:
        quoted = await http.post(f"/{served.chain_id}/v1/sponsorships/quote", json=asking)
        assert quoted.status_code == 200, quoted.text
        quote = quoted.json()
        (in_token,) = quote["quotes"]
        body = {
            "account": user.address,
            "transactions": [
                wallet_signed(user, t) for t in [*quote["transactions"], in_token["payment"]]
            ],
            "payment": {"token": token, "payee": quote["forwarder"], "amount": in_token["amount"]},
            "maxBlockNumber": quote["maxBlockNumber"],
        }
        if short_of == "gas for its code":
            body["gas"] = "0x1"
        refused = await http.post(f"/{served.chain_id}/v1/sponsorships", json=body)

    error = refused.json()["error"]
    if short_of == "gas for its code":
        assert refused.status_code == 422, refused.text
        assert error["code"] == Code.SIMULATION_FAILED, refused.text
    else:
        assert refused.status_code == 400, refused.text
        assert error["data"] == {"reason": "sponsored-unfunded", "field": "transactions"}


async def test_without_the_payees_forwarder_nothing_is_sponsored(
    chain: LocalChain, served: Served
) -> None:
    """The payee's forwarder not deployed: a call to its address would succeed and check
    nothing, so a sponsorship is neither quoted nor taken (503, the operator's to fix) and the
    root says why. A call is quoted as it was: it doesn't use the forwarder. Once the forwarder
    is deployed a sponsorship is quoted at once, not when the chain is next measured (an hour
    on), and the root lists nothing of it."""
    forwarder = forwarder_of(served.payee).address
    await chain.rpc("anvil_setCode", [forwarder, "0x"])  # the setup's, taken away
    user = fresh_client("sponsored where no forwarder is")
    await chain.mint(USDC, user.address, 10**9)
    by = await signing(served)
    paid = 10**8
    paying = await signed_by(chain, user, 0, transfer(USDC, forwarder, paid), by.max_fee)
    acting = transfer(USDC, await chain.account(3), ACTION)
    asking = {
        "tokens": [USDC.address],
        "account": user.address,
        "transactions": [{"to": acting.to, "data": "0x" + acting.data.hex()}],
    }

    async with served.interface() as http:
        quoted = await http.post(f"/{served.chain_id}/v1/sponsorships/quote", json=asking)
        taken = await http.post(
            f"/{served.chain_id}/v1/sponsorships",
            json=sponsored(forwarder, paid, by.deadline, [paying]),
        )
        call = await quoting(http, served.chain_id)
        listed = await problems(http)
        runtime = "0x" + forwarder_of(served.payee).runtime.hex()
        await chain.rpc("anvil_setCode", [forwarder, runtime])
        deployed = await http.post(f"/{served.chain_id}/v1/sponsorships/quote", json=asking)
        after = await problems(http)

    for refused in (quoted, taken):
        assert refused.status_code == 503, refused.text
        assert refused.json()["error"]["data"] == {
            "category": "operator",
            "reason": "no-forwarder",
        }, refused.text
    assert call.status_code == 200, call.text
    assert any("forwarder" in p for p in listed), listed
    assert deployed.status_code == 200, deployed.text
    assert not any("forwarder" in p for p in after), after


async def test_the_root_shows_what_the_forwarder_holds(chain: LocalChain, served: Served) -> None:
    """The forwarder is read with the accounts when a batch is built: the root shows its
    address, its ETH and what it holds of each token, each as the chain had it at the block
    read. What's paid to it and never forwarded stays there to be seen."""
    forwarder = forwarder_of(served.payee).address
    await chain.mint(USDC, forwarder, 5)
    await chain.mint(USDT, forwarder, 7)
    await chain.set_eth_balance(forwarder, 3)
    user = fresh_client("sponsored, the forwarder watched")

    async with served.interface() as http:
        await taken_paying(chain, served, http, user)
        await served.land()
        root = (await http.get("/", headers={"Accept": "application/json"})).json()
        browsed = (await http.get("/", headers={"Accept": "text/html"})).text

    (entry,) = root["chains"]
    status = entry["accountsStatus"]
    block = status["block"]

    async def held(token: ChecksumAddress) -> int:
        read = balance_of_call(token, forwarder, block)
        return int(await chain.rpc(read.method, list(read.params)), 16)

    assert entry["forwarder"] == forwarder
    assert int(status["forwarderBalance"], 16) == 3
    amounts = {h["symbol"]: int(h["amount"], 16) for h in status["forwarderTokens"]}
    assert amounts["USDC"] == await held(USDC.address) > 0
    assert amounts["USDT"] == await held(USDT.address) == 7
    assert "<td>forwarder</td>" in browsed


async def test_a_sponsorship_costs_no_more_than_a_transaction_may(
    chain: LocalChain, served: Served
) -> None:
    """One total caps what a sponsorship costs the relay: its own transactions' gas and all
    the account is sent, the value its transactions send included, within what a transaction
    of the most gas costs by the deadline. A transaction sending more ETH than that is refused
    when quoted, told the most and what it needs; one sending exactly what's left is quoted,
    and a wei more isn't. Submitted signed, a value over it and a max fee far over what the
    deadline needs are each refused, before anything is priced."""
    user = fresh_client("sponsored past the cap")
    friend = await chain.account(3)
    await chain.mint(USDC, user.address, 10**9)
    by = await signing(served)
    over = 10 * 10**18

    def asking(value: int) -> dict[str, object]:
        return {
            "tokens": [USDC.address],
            "maxBlockNumber": hex(by.deadline),
            "account": user.address,
            "transactions": [{"to": friend, "value": hex(value)}],
        }

    paying = await signed_by(chain, user, 1, transfer(USDC, by.forwarder, 10**8), by.max_fee)
    sending = await signed_by(
        chain, user, 0, EncodedCall(friend, b""), by.max_fee, gas=21_000, value=over
    )
    dear = await signed_by(
        chain, user, 0, transfer(USDC, friend, ACTION), 10**6 * by.max_fee, gas=100_000
    )
    quote_url = f"/{served.chain_id}/v1/sponsorships/quote"
    url = f"/{served.chain_id}/v1/sponsorships"

    async with served.interface() as http:
        refused = await http.post(quote_url, json=asking(over))
        data = refused.json()["error"]["data"]
        most, needs = int(data["most"], 16), int(data["needs"], 16)
        fitting = over - (needs - most)
        at_most = await http.post(quote_url, json=asking(fitting))
        a_wei_more = await http.post(quote_url, json=asking(fitting + 1))
        valued = await http.post(
            url, json=sponsored(by.forwarder, 10**8, by.deadline, [sending, paying])
        )
        overpriced = await http.post(
            url, json=sponsored(by.forwarder, 10**8, by.deadline, [dear, paying])
        )

    assert refused.status_code == 400, refused.text
    assert data["reason"] == "sponsored-over", refused.text
    assert data["field"] == "transactions", refused.text
    assert 0 < most < needs, refused.text
    assert 0 < fitting < over
    assert at_most.status_code == 200, at_most.text
    for each in (a_wei_more, valued, overpriced):
        assert each.status_code == 400, each.text
        assert each.json()["error"]["data"]["reason"] == "sponsored-over", each.text
