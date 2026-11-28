"""The hosted x402 facilitator (bruno/server/facilitator.py), over HTTP on both relays: a
merchant's payout address, its receipt address, and a buyer's payment verified and settled as
x402's /verify and /settle have it, the fee's split told to the merchant alone."""

import asyncio
import base64
import json
from typing import Any

import httpx
import pytest
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address

from bruno.eip3009 import TransferAuthorization
from bruno.server.facilitator import WAIT_BLOCKS
from bruno.tests import smartwallet
from bruno.tests.calibur import fresh_client
from bruno.tests.interface import landing
from bruno.tests.localchain import USDC, USDC_DOMAIN, USDT, LocalChain
from bruno.tests.served import Served
from bruno.tests.transfers import (
    MULTICALL3,
    by_key,
    by_undeployed_wallet,
    by_wallet,
    usdc_transfer,
)

# what a buyer pays in these tests: well over a settlement's fee on the local chain
PRICE = 100_000_000


def network(served: Served) -> str:
    return f"eip155:{served.chain_id}"


async def pay_to(http: httpx.AsyncClient, served: Served, payout: str) -> ChecksumAddress:
    """Where `payout`'s buyers pay on the served chain, as its facilitator says."""
    answered = await http.get(f"/x402/facilitator/{payout}/")
    assert answered.status_code == 200, answered.text
    return to_checksum_address(answered.json()["payTo"][network(served)])


def asked(
    served: Served, receipt: str, transfer: TransferAuthorization, amount: int | None = None
) -> dict[str, object]:
    """x402's /verify and /settle request for `transfer`, a payment of the local USDC to
    `receipt`, the amount required its own unless `amount` says otherwise."""
    required = {
        "scheme": "exact",
        "network": network(served),
        "amount": str(transfer.value if amount is None else amount),
        "asset": USDC.address,
        "payTo": receipt,
        "maxTimeoutSeconds": 60,
        "extra": {"name": USDC_DOMAIN.name, "version": USDC_DOMAIN.version},
    }
    payload = {
        "x402Version": 2,
        "accepted": required,
        "payload": {
            "signature": "0x" + transfer.signature.hex(),
            "authorization": {
                "from": transfer.payer,
                "to": transfer.to,
                "value": str(transfer.value),
                "validAfter": str(transfer.valid_after),
                "validBefore": str(transfer.valid_before),
                "nonce": "0x" + transfer.nonce.hex(),
            },
        },
    }
    return {"x402Version": 2, "paymentPayload": payload, "paymentRequirements": required}


def split_of(response: httpx.Response) -> dict[str, Any]:
    """What an answer tells the merchant of the fee: EXTENSION-RESPONSES' `bruno-fee`."""
    told: dict[str, Any] = json.loads(base64.b64decode(response.headers["EXTENSION-RESPONSES"]))
    assert list(told) == ["bruno-fee"]
    fee: dict[str, Any] = told["bruno-fee"]
    return fee


async def paying(
    served: Served, buyer: LocalAccount, receipt: ChecksumAddress, value: int = PRICE
) -> TransferAuthorization:
    """`buyer`'s payment of `value` of the local USDC to `receipt`, signed."""
    return await usdc_transfer(served.chain_id, buyer.address, receipt, value, by_key(buyer))


async def test_a_payment_is_verified_and_settled_for_a_merchant(
    chain: LocalChain, served: Served
) -> None:
    """A merchant's payout address has a receipt address, which a buyer pays. /verify says
    the payment would be settled, and tells the merchant its split, nothing moved. /settle
    lands it in one transaction, Multicall3's: the buyer pays the price, Bruno's payee gains
    the fee, the receipt wallet keeps one unit the first time, and the merchant gains the
    rest. The same payment sent to /settle again is answered by the same transaction, and a
    second payment leaves the receipt wallet nothing more."""
    merchant = fresh_client("a merchant paid through the facilitator").address
    buyer = fresh_client("a buyer paying a merchant")
    await chain.mint(USDC, buyer.address, 2 * PRICE)
    base = f"/x402/facilitator/{merchant}"
    async with served.interface() as http:
        receipt = await pay_to(http, served, merchant)
        supported = (await http.get(f"{base}/supported")).json()
        body = asked(served, receipt, await paying(served, buyer, receipt))
        held = [await chain.balance_of(USDC, a) for a in (merchant, served.payee, receipt)]
        verified = await http.post(f"{base}/verify", json=body)
        assert verified.status_code == 200, verified.text
        quoted = split_of(verified)
        unmoved = [await chain.balance_of(USDC, a) for a in (merchant, served.payee, receipt)]
        settled = await landing(served, asyncio.create_task(http.post(f"{base}/settle", json=body)))
        assert settled.json()["success"] is True, settled.text
        charged = split_of(settled)
        again = await http.post(f"{base}/settle", json=body)
        second = asked(served, receipt, await paying(served, buyer, receipt))
        later = split_of(await http.post(f"{base}/verify", json=second))

    assert {"x402Version": 2, "scheme": "exact", "network": network(served)} in supported["kinds"]
    assert supported["extensions"] == ["bruno-fee"]
    # the accounts that send for the chain: the settlement's own sender among them
    assert len(supported["signers"][network(served)]) >= 1
    assert verified.json() == {"isValid": True, "payer": buyer.address}
    assert unmoved == held
    fee = int(quoted["fee"])
    assert quoted == {
        "asset": USDC.address,
        "amount": str(PRICE),
        "fee": str(fee),
        "kept": "1",
        "payout": str(PRICE - fee - 1),
    }
    assert 0 < fee < PRICE
    paid = int(charged["fee"])
    assert charged["payout"] == str(PRICE - paid - 1)
    now = [await chain.balance_of(USDC, a) for a in (merchant, served.payee, receipt)]
    assert [after - before for before, after in zip(held, now, strict=True)] == [
        PRICE - paid - 1,
        paid,
        1,
    ]
    assert await chain.balance_of(USDC, buyer.address) == PRICE
    answer = settled.json()
    assert answer["payer"] == buyer.address
    assert answer["network"] == network(served)
    assert answer["amount"] == str(PRICE)
    sent = await chain.rpc("eth_getTransactionByHash", [answer["transaction"]])
    assert to_checksum_address(sent["to"]) == MULTICALL3
    assert to_checksum_address(sent["from"]) in supported["signers"][network(served)]
    assert again.json()["success"] is True, again.text
    assert again.json()["transaction"] == answer["transaction"]
    # the wallet holds the token now: nothing more is kept
    assert later["kept"] == "0"


@pytest.mark.parametrize(
    ("wrong", "reason"),
    [
        ("paid to another address", "invalid_exact_evm_payload_recipient_mismatch"),
        ("another amount required", "invalid_exact_evm_payload_authorization_value_mismatch"),
        ("signed by another key", "invalid_exact_evm_payload_signature"),
        ("a buyer holding too little", "insufficient_funds"),
        ("a token the terms don't take", "bruno_asset_not_accepted"),
        ("a fee over the terms' cap", "bruno_fee_over_max"),
        ("a payment under the fee", "bruno_payment_under_fee"),
        ("a chain not served", "invalid_network"),
    ],
    ids=lambda v: v if isinstance(v, str) and " " in v else "",
)
async def test_a_payment_that_wouldnt_settle_is_refused_for_why(
    chain: LocalChain, served: Served, wrong: str, reason: str
) -> None:
    """/verify and /settle refuse a payment alike, with x402's code for why or one of Bruno's
    own, and nothing moves: paid to an address that isn't the merchant's receipt address; of
    an amount that isn't the one required; signed by a key that isn't the buyer's; from a
    buyer holding less; in a token the merchant's terms don't take; at a fee over the terms'
    cap; too small to cover the fee; on a chain the relay doesn't serve."""
    merchant = fresh_client(f"a merchant refused: {wrong}").address
    buyer, other = fresh_client(f"a buyer refused: {wrong}"), fresh_client("someone else")
    await chain.mint(USDC, buyer.address, PRICE)
    base = f"/x402/facilitator/{merchant}"
    async with served.interface() as http:
        receipt = await pay_to(http, served, merchant)
        transfer = await paying(served, buyer, receipt)
        amount: int | None = None
        if wrong == "paid to another address":
            transfer = await paying(served, buyer, other.address)
        elif wrong == "another amount required":
            amount = PRICE + 1
        elif wrong == "signed by another key":
            transfer = await usdc_transfer(
                served.chain_id, buyer.address, receipt, PRICE, by_key(other)
            )
        elif wrong == "a buyer holding too little":
            transfer = await paying(served, buyer, receipt, 2 * PRICE)
        elif wrong == "a token the terms don't take":
            base += f"/tokens={USDT.address}"
        elif wrong == "a fee over the terms' cap":
            base += f"/max-fee={USDC.address}:1"
        elif wrong == "a payment under the fee":
            transfer = await paying(served, buyer, receipt, 2)
        body = asked(served, receipt, transfer, amount)
        if wrong == "a chain not served":
            requirements: Any = body["paymentRequirements"]
            requirements["network"] = "eip155:424242"
        before = await chain.balance_of(USDC, buyer.address)
        verified = await http.post(f"{base}/verify", json=body)
        settled = await http.post(f"{base}/settle", json=body)

    assert verified.status_code == settled.status_code == 200, (verified.text, settled.text)
    assert verified.json()["isValid"] is False, verified.text
    assert verified.json()["invalidReason"] == reason, verified.text
    assert "EXTENSION-RESPONSES" not in verified.headers
    assert settled.json()["success"] is False, settled.text
    assert settled.json()["errorReason"] == reason, settled.text
    assert settled.json()["transaction"] == ""
    assert await chain.balance_of(USDC, buyer.address) == before


@pytest.mark.parametrize(
    ("path", "field"),
    [
        ("0x1234/verify", "payout"),
        ("{merchant}/max-fee={usdc}/verify", "max-fee"),
        ("{merchant}/tokens={usdc}/tokens={usdc}/verify", "tokens"),
        ("{merchant}/fee=1/verify", "fee"),
        ("{merchant}/elsewhere", "elsewhere"),
    ],
)
async def test_a_url_that_names_no_facilitator_is_refused(
    served: Served, path: str, field: str
) -> None:
    """A facilitator URL whose path isn't one is invalid params, naming the part at fault: a
    payout address that isn't an address, a term that isn't what it's to be or is given
    twice, a term unknown, an endpoint unknown."""
    merchant = fresh_client("a merchant with a wrong URL").address
    async with served.interface() as http:
        refused = await http.post(
            "/x402/facilitator/" + path.format(merchant=merchant, usdc=USDC.address), json={}
        )

    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["data"] == {"reason": "malformed", "field": field}


async def test_terms_are_read_however_a_client_encodes_them(
    chain: LocalChain, served: Served
) -> None:
    """Terms whose `=`, `:` and `,` a client percent-encoded are the same terms: a cap of one
    unit refuses the payment as it does unencoded, and a cap well over the fee lets it
    through."""
    merchant = fresh_client("a merchant whose client encodes its URL").address
    buyer = fresh_client("a buyer under encoded terms")
    await chain.mint(USDC, buyer.address, PRICE)
    usdc = USDC.address
    low = f"tokens%3D{usdc}%2C{USDT.address}/max-fee%3D{usdc}%3A1"
    high = f"tokens%3D{usdc}/max-fee%3D{usdc}%3A{PRICE}"
    async with served.interface() as http:
        receipt = await pay_to(http, served, merchant)
        body = asked(served, receipt, await paying(served, buyer, receipt))
        capped = await http.post(f"/x402/facilitator/{merchant}/{low}/verify", json=body)
        allowed = await http.post(f"/x402/facilitator/{merchant}/{high}/verify", json=body)

    assert capped.json()["invalidReason"] == "bruno_fee_over_max", capped.text
    assert allowed.json()["isValid"] is True, allowed.text


async def test_a_settlement_not_included_by_its_deadline_is_tried_again(
    chain: LocalChain, served: Served
) -> None:
    """A settlement the relay takes and no block includes by its deadline, a few blocks on: its
    /settle is answered that it wasn't settled, by a reason of Bruno's own, nothing moved and
    the payment unused. The same payment sent to /settle again is tried again, and lands."""
    merchant = fresh_client("a merchant whose settlement is late").address
    buyer = fresh_client("a buyer whose payment is settled late")
    await chain.mint(USDC, buyer.address, PRICE)
    base = f"/x402/facilitator/{merchant}"
    async with served.interface() as http:
        receipt = await pay_to(http, served, merchant)
        body = asked(served, receipt, await paying(served, buyer, receipt))
        before = await chain.balance_of(USDC, buyer.address)
        first = asyncio.create_task(http.post(f"{base}/settle", json=body))
        # taken, and then blocks that the relay sends nothing for, past its deadline
        await asyncio.wait({first}, timeout=1.0)
        for _ in range(WAIT_BLOCKS + 1):
            await chain.rpc("evm_mine", [])
        late = await landing(served, first)
        unmoved = await chain.balance_of(USDC, buyer.address)
        again = await landing(served, asyncio.create_task(http.post(f"{base}/settle", json=body)))

    assert late.status_code == 200, late.text
    assert late.json() == {
        "success": False,
        "transaction": "",
        "network": network(served),
        "errorReason": "bruno_not_included",
        "payer": buyer.address,
    }
    assert unmoved == before
    assert again.json()["success"] is True, again.text
    assert await chain.balance_of(USDC, buyer.address) == before - PRICE


async def test_a_deployed_smart_wallet_buys(chain: LocalChain, served: Served) -> None:
    """A buyer that's a deployed smart wallet, its signature checked by its own code: the
    relay doesn't bound its transfer, so it's a transaction of its own, ahead of the receipt
    wallet's two in Multicall3's, all in one block."""
    merchant = fresh_client("a merchant paid by a smart wallet").address
    owner = fresh_client("a buying smart wallet's owner")
    wallet = await smartwallet.create(chain, owner)
    await chain.mint(USDC, wallet, PRICE)
    base = f"/x402/facilitator/{merchant}"
    async with served.interface() as http:
        receipt = await pay_to(http, served, merchant)
        transfer = await usdc_transfer(
            served.chain_id, wallet, receipt, PRICE, by_wallet(chain, wallet, owner)
        )
        body = asked(served, receipt, transfer)
        verified = await http.post(f"{base}/verify", json=body)
        settled = await landing(served, asyncio.create_task(http.post(f"{base}/settle", json=body)))

    assert verified.json() == {"isValid": True, "payer": wallet}, verified.text
    assert settled.json()["success"] is True, settled.text
    last = await chain.rpc("eth_getTransactionByHash", [settled.json()["transaction"]])
    block = await chain.rpc("eth_getBlockByNumber", [last["blockNumber"], True])
    assert [to_checksum_address(t["to"]) for t in block["transactions"]] == [
        USDC.address,
        MULTICALL3,
    ]
    assert await chain.balance_of(USDC, wallet) == 0
    assert await chain.balance_of(USDC, merchant) == PRICE - int(split_of(settled)["fee"]) - 1


async def test_a_smart_wallet_not_deployed_yet_isnt_served(
    chain: LocalChain, served: Served
) -> None:
    """A buyer that's a smart wallet not deployed yet, its signature wrapped with its
    deployment (ERC-6492): refused by /verify and /settle alike, the wallet not deployed and
    nothing moved. It signed exactly the price, which leaves nothing to deploy it with."""
    merchant = fresh_client("a merchant an undeployed wallet would pay").address
    owner = fresh_client("an undeployed buying wallet's owner")
    wallet = await smartwallet.address_of(chain, owner)
    await chain.mint(USDC, wallet, PRICE)
    base = f"/x402/facilitator/{merchant}"
    async with served.interface() as http:
        receipt = await pay_to(http, served, merchant)
        transfer = await usdc_transfer(
            served.chain_id,
            wallet,
            receipt,
            PRICE,
            by_undeployed_wallet(served.chain_id, wallet, owner),
        )
        body = asked(served, receipt, transfer)
        verified = await http.post(f"{base}/verify", json=body)
        settled = await http.post(f"{base}/settle", json=body)

    reason = "invalid_exact_evm_payload_undeployed_smart_wallet"
    assert verified.json() == {"isValid": False, "invalidReason": reason, "payer": wallet}
    assert settled.json()["errorReason"] == reason, settled.text
    assert await chain.code(wallet) == "0x"
    assert await chain.balance_of(USDC, wallet) == PRICE
