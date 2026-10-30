"""x402 on anvil, against both relays: a call paid by a signed EIP-3009 authorization of
the local USDC (FiatTokenV2_2), settled by the runner in the call's own transaction. The
challenge, a payment taken and landed, and the payments refused."""

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import msgspec
import pytest
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress, HexStr
from eth_utils.address import to_checksum_address
from jsonschema import Draft202012Validator

from bruno.config import ChainConfig
from bruno.erc6492 import MAGIC
from bruno.evm import NATIVE_ASSET, SECP256K1_N, EncodedCall
from bruno.server.front import chain_path
from bruno.server.wire import X402Params, data
from bruno.server.x402 import INVALID_SIGNATURE, VALUE_MISMATCH, payload_of
from bruno.submission import Submission
from bruno.tests import kernel, smartwallet
from bruno.tests.calibur import fresh_client
from bruno.tests.interface import challenged, landing, signed_call
from bruno.tests.localchain import (
    CALIBUR,
    REVERT_ALWAYS,
    TEST_FLOAT,
    USDC,
    USDC_DOMAIN,
    LocalChain,
)
from bruno.tests.served import (
    BUILDER_URL,
    PAYEE_ACCOUNT,
    X402_DEADLINE_BLOCKS,
    Served,
    Setup,
    relay_config,
)
from bruno.tests.transfers import (
    CALL_GAS,
    as_given,
    balance_call,
    by_kernel,
    by_key,
    by_undeployed_kernel,
    by_undeployed_wallet,
    by_wallet,
    decoded,
    deployment_json,
    payment,
)


async def authorization_used(chain: LocalChain, client: LocalAccount, nonce: str) -> bool:
    used = await chain.call(
        USDC.address,
        "authorizationState(address,bytes32)",
        ["address", "bytes32"],
        [client.address, bytes.fromhex(nonce[2:])],
    )
    return int.from_bytes(used) == 1


def submission_id(served: Served, body: dict[str, object], signature: str) -> str:
    """The id of the submission a payment for `body`'s call makes: its transfer in place of
    its deadline, so known before it's taken."""
    params = msgspec.convert(body, X402Params)
    transfer = payload_of(signature).transfer()
    deployment = None if params.deployment is None else params.deployment.deployment()
    submission = Submission(
        call=EncodedCall(to_checksum_address(params.call.to), HexStr(params.call.data.lower())),
        authorizations=(),
        gas=int(params.gas, 16),
        token=USDC.address,
        payee=served.payee,
        amount=transfer.value,
        max_block_number=0,
        transfer=transfer,
        deployment=deployment,
    )
    return data(submission.id)


def sending(
    http: httpx.AsyncClient, served: Served, body: dict[str, object], signature: str
) -> asyncio.Task[httpx.Response]:
    """A payment for `body`'s call sent, its request held in the background."""
    headers = {"PAYMENT-SIGNATURE": signature}
    return asyncio.create_task(http.post(f"/{served.chain_id}/v1/x402", json=body, headers=headers))


async def taken(
    http: httpx.AsyncClient, served: Served, id_: str, sent: asyncio.Task[httpx.Response]
) -> None:
    """Once the submission `id_` is taken, or `sent` is answered without it."""
    while not sent.done():
        if (await http.get(f"/{served.chain_id}/v1/submissions/{id_}")).status_code == 200:
            return
        await asyncio.sleep(0.05)


async def held(
    http: httpx.AsyncClient, served: Served, body: dict[str, object], signature: str
) -> httpx.Response:
    """A payment's answer: sent, taken, then held while blocks land until it's answered."""
    sent = sending(http, served, body, signature)
    await taken(http, served, submission_id(served, body, signature), sent)
    return await landing(served, sent)


async def test_the_challenge(served: Served) -> None:
    """Without a payment, 402: what to pay in USDC, for the call posted, or a reference
    call's price without one; with the bazaar extension, its info valid by its schema."""
    client = fresh_client("an x402 client")
    async with served.interface() as http:
        required = await challenged(http, served.chain_id, balance_call(client))
        probed = await challenged(http, served.chain_id, None)

    (accepts,) = required["accepts"]
    assert required["x402Version"] == 2
    assert required["resource"]["url"].endswith(f"/{chain_path(served.chain_id)}/v1/x402")
    assert accepts["scheme"] == "exact"
    assert accepts["network"] == f"eip155:{served.chain_id}"
    assert accepts["asset"] == USDC.address
    assert accepts["payTo"] == served.payee
    assert accepts["maxTimeoutSeconds"] == X402_DEADLINE_BLOCKS * 12
    assert accepts["extra"] == {"name": USDC_DOMAIN.name, "version": USDC_DOMAIN.version}
    # the reference call is larger: an authorization, 150,000 gas, 512 bytes
    assert int(probed["accepts"][0]["amount"]) > int(accepts["amount"]) > 0
    for each in (required, probed):
        bazaar = each["extensions"]["bazaar"]
        Draft202012Validator(bazaar["schema"]).validate(bazaar["info"])


async def test_a_payment_is_held_until_it_lands(chain: LocalChain, served: Served) -> None:
    """A client holding only USDC, its account never used, pays the challenge's amount, twice
    at once: one submission, waiting, its status saying so. Both requests are held until its
    transaction is included: 200, that transaction in PAYMENT-RESPONSE, its status confirming.
    The payee gains the amount once, the authorization is used, and the payment sent again is
    answered with the same transaction."""
    client = fresh_client("a paying x402 client")
    body = balance_call(client)
    async with served.interface() as http:
        accepts = (await challenged(http, served.chain_id, body))["accepts"][0]
        amount = int(accepts["amount"])
        await chain.mint(USDC, client.address, amount)
        signature = await payment(served.chain_id, accepts, client.address, by_key(client))
        before = await chain.balance_of(USDC, served.payee)
        both = [sending(http, served, body, signature) for _ in range(2)]
        id_ = submission_id(served, body, signature)
        await taken(http, served, id_, both[0])
        waiting = await http.get(f"/{served.chain_id}/v1/submissions/{id_}")
        paid, again = [await landing(served, sent) for sent in both]
        confirming = await http.get(paid.headers["Location"])
        landed = await http.post(
            f"/{served.chain_id}/v1/x402", json=body, headers={"PAYMENT-SIGNATURE": signature}
        )

    assert waiting.json()["stage"] == "waiting", waiting.json()
    assert waiting.headers["Retry-After"] == "12"
    assert paid.status_code == 200, paid.text
    settled = decoded(paid.headers["PAYMENT-RESPONSE"])
    status = confirming.json()
    assert settled["success"] is True
    assert settled["transaction"] == status["transactionHash"]
    assert settled["payer"] == client.address
    assert settled["extensions"]["status"]["info"]["url"] == paid.json()["statusUrl"]
    assert paid.json()["statusUrl"].endswith(paid.headers["Location"])
    assert (status["status"], status["stage"]) == ("included", "confirming"), status
    assert int(status["confirmations"], 16) >= 1
    for each in (again, landed):
        assert each.status_code == 200, each.text
        assert each.json()["id"] == paid.json()["id"] == id_
        assert decoded(each.headers["PAYMENT-RESPONSE"])["transaction"] == settled["transaction"]
    assert await chain.balance_of(USDC, served.payee) - before == amount
    nonce = decoded(signature)["payload"]["authorization"]["nonce"]
    assert await authorization_used(chain, client, nonce)


async def test_a_tip_is_an_amount_accepted_above_the_quote(
    chain: LocalChain, served: Served
) -> None:
    """A client that tips accepts more than the quote, and signs for exactly that: taken, and
    the payee gains the whole of it."""
    client = fresh_client("an x402 client that tips")
    body = balance_call(client)
    async with served.interface() as http:
        accepts = (await challenged(http, served.chain_id, body))["accepts"][0]
        tipped = 2 * int(accepts["amount"])
        await chain.mint(USDC, client.address, tipped)
        accepted = {**accepts, "amount": str(tipped)}
        before = await chain.balance_of(USDC, served.payee)
        paid = await held(
            http,
            served,
            body,
            await payment(served.chain_id, accepted, client.address, by_key(client)),
        )

    assert paid.status_code == 200, paid.text
    assert decoded(paid.headers["PAYMENT-RESPONSE"])["amount"] == str(tipped)
    assert await chain.balance_of(USDC, served.payee) - before == tipped


async def test_a_payment_signed_again_is_held_for_the_first(
    chain: LocalChain, served: Served
) -> None:
    """A client that gave up waiting and signed the same call's payment again, while the first
    is pending: the second is held for the first, answered with its transaction, and its own
    authorization is never used. The payee gains the amount once."""
    client = fresh_client("an x402 client that signs again")
    body = balance_call(client)
    async with served.interface() as http:
        accepts = (await challenged(http, served.chain_id, body))["accepts"][0]
        amount = int(accepts["amount"])
        await chain.mint(USDC, client.address, 2 * amount)
        first, second = [
            await payment(served.chain_id, accepts, client.address, by_key(client))
            for _ in range(2)
        ]
        before = await chain.balance_of(USDC, served.payee)
        sent_first = sending(http, served, body, first)
        await taken(http, served, submission_id(served, body, first), sent_first)
        sent_second = sending(http, served, body, second)
        # held: not answered before anything lands
        done, _ = await asyncio.wait({sent_second}, timeout=1.0)
        assert not done
        answers = [await landing(served, sent) for sent in (sent_first, sent_second)]

    transactions = {decoded(a.headers["PAYMENT-RESPONSE"])["transaction"] for a in answers}
    assert [a.status_code for a in answers] == [200, 200], [a.text for a in answers]
    assert len(transactions) == 1
    assert await chain.balance_of(USDC, served.payee) - before == amount
    nonce = decoded(second)["payload"]["authorization"]["nonce"]
    assert not await authorization_used(chain, client, nonce)


async def contract_payer_pays(
    chain: LocalChain,
    served: Served,
    payer: ChecksumAddress,
    body: dict[str, object],
    sign: Callable[[dict[str, Any]], Awaitable[str]],
) -> None:
    """`payer`, an account with code holding USDC, its payments signed by `sign`, pays the
    challenge. Its transfer is priced as it runs, its own signature check (ERC-1271) included:
    either the challenge's amount covers it, or it's answered 402 with what does and pays that.
    Either way its request is held until its transaction lands, and the payee gains what it
    paid."""
    async with served.interface() as http:
        accepts = (await challenged(http, served.chain_id, body))["accepts"][0]
        await chain.mint(USDC, payer, 10 * int(accepts["amount"]))
        before = await chain.balance_of(USDC, served.payee)
        answer = await landing(served, sending(http, served, body, await sign(accepts)))
        paid_amount = int(accepts["amount"])
        if answer.status_code == 402:  # its own transfer costs more: what that is, paid
            assert decoded(answer.headers["PAYMENT-RESPONSE"])["errorReason"] == VALUE_MISMATCH
            repriced = decoded(answer.headers["PAYMENT-REQUIRED"])["accepts"][0]
            assert int(repriced["amount"]) > paid_amount
            answer = await held(http, served, body, await sign(repriced))
            paid_amount = int(repriced["amount"])

    assert answer.status_code == 200, answer.text
    assert decoded(answer.headers["PAYMENT-RESPONSE"])["payer"] == payer
    assert await chain.balance_of(USDC, served.payee) - before == paid_amount


@pytest.mark.parametrize(
    "served", [Setup(max_authorizations=2)], indirect=True, ids=["two authorizations"]
)
@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("nonce", "nonce out of range"),
        ("yParity", "yParity is 0 or 1"),
        ("high s", "r or s out of range"),
        ("the second's nonce", "nonce out of range"),
    ],
)
async def test_a_challenge_checks_authorizations_as_a_submission(
    chain: LocalChain, served: Served, case: str, message: str
) -> None:
    """A challenge's authorizations are checked as a submission's are, every one of them,
    before any price: a nonce past 2**53 - 1, a yParity that isn't 0 or 1, a high s, and a
    second authorization's nonce past it are each invalid params, the same on both relays."""
    client = fresh_client(f"an x402 client's authorization: {case}")
    call, auth = await signed_call(chain, client, served.payee, 1)
    match case:
        case "nonce":
            authorizations = [{**auth, "nonce": hex(2**53)}]
        case "yParity":
            authorizations = [{**auth, "yParity": "0x2"}]
        case "high s":
            flipped = hex(SECP256K1_N - int(auth["s"], 16))
            authorizations = [{**auth, "s": flipped, "yParity": hex(1 - int(auth["yParity"], 16))}]
        case _:
            authorizations = [auth, {**auth, "nonce": hex(2**53)}]
    body = {"call": call, "gas": hex(CALL_GAS), "authorizationList": authorizations}
    async with served.interface() as http:
        answer = await http.post(f"/{served.chain_id}/v1/x402", json=body)

    assert answer.status_code == 400, answer.text
    expected = {
        "code": -32602,
        "message": f"authorizationList: {message}",
        "data": "authorizationList",
    }
    assert answer.json()["error"] == expected, answer.text


async def test_a_smart_wallet_pays(chain: LocalChain, served: Served) -> None:
    """A Coinbase Smart Wallet pays, its signature ERC-1271's, longer than a key's."""
    owner = fresh_client("a smart wallet's owner")
    wallet = await smartwallet.create(chain, owner)

    async def sign(accepted: dict[str, Any]) -> str:
        return await payment(served.chain_id, accepted, wallet, by_wallet(chain, wallet, owner))

    await contract_payer_pays(chain, served, wallet, balance_call(owner), sign)


@pytest.mark.parametrize("kind", ["coinbase", "kernel"])
async def test_an_undeployed_smart_wallet_pays(
    chain: LocalChain, served: Served, kind: str
) -> None:
    """A smart wallet that isn't deployed yet, holding USDC, pays with an ERC-6492 signature: a
    Coinbase Smart Wallet, or ZeroDev's Kernel as AgentKit makes it (through a meta-factory).
    Declared in the body, its deployment is in the challenge's price, which it pays at once;
    its transaction deploys it first. Deployed, it then pays again unwrapped, for less."""
    owner = fresh_client(f"an undeployed smart wallet's owner: {kind}")
    if kind == "kernel":
        wallet = await kernel.address_of(chain, owner)
        deployment = kernel.deployment(wallet, owner)
        undeployed = by_undeployed_kernel(served.chain_id, wallet, owner)
        deployed = by_kernel(served.chain_id, wallet, owner)
    else:
        wallet = await smartwallet.address_of(chain, owner)
        deployment = smartwallet.deployment(wallet, owner)
        undeployed = by_undeployed_wallet(served.chain_id, wallet, owner)
        deployed = by_wallet(chain, wallet, owner)
    body = {**balance_call(owner), "payer": wallet, "deployment": deployment_json(deployment)}
    async with served.interface() as http:
        accepts = (await challenged(http, served.chain_id, body))["accepts"][0]
        await chain.mint(USDC, wallet, 10 * int(accepts["amount"]))
        before = await chain.balance_of(USDC, served.payee)
        amount = int(accepts["amount"])
        payment_header = await payment(served.chain_id, accepts, wallet, undeployed)
        first = await held(http, served, body, payment_header)
        deployed_code = await chain.code(wallet)
        paid = await chain.balance_of(USDC, served.payee) - before
        again_body = {**balance_call(owner), "payer": wallet}
        again = (await challenged(http, served.chain_id, again_body))["accepts"][0]
        second = await held(
            http,
            served,
            again_body,
            await payment(served.chain_id, again, wallet, deployed),
        )

    assert first.status_code == 200, first.text
    assert deployed_code != "0x"
    assert paid == amount
    assert second.status_code == 200, second.text
    assert int(again["amount"]) < amount


async def test_an_account_delegated_to_calibur_pays(chain: LocalChain, served: Served) -> None:
    """A key's account delegated to Calibur (EIP-7702) pays with the signature the key alone
    would sign: having code, it's checked by ERC-1271, Calibur's root key recovering it."""
    client = fresh_client("an x402 client delegated to Calibur")
    await chain.rpc("anvil_setCode", [client.address, "0xef0100" + CALIBUR[2:].lower()])

    async def sign(accepted: dict[str, Any]) -> str:
        return await payment(served.chain_id, accepted, client.address, by_key(client))

    await contract_payer_pays(chain, served, client.address, balance_call(client), sign)


async def test_a_smart_wallet_signed_by_another_is_refused(
    chain: LocalChain, served: Served
) -> None:
    """A contract payer's payment signed by a key that isn't its owner: its isValidSignature
    refuses it, and so does the relay, with x402's invalid-signature code, nothing spent."""
    owner = fresh_client("another smart wallet's owner")
    wallet = await smartwallet.create(chain, owner)
    body = balance_call(owner)
    async with served.interface() as http:
        accepts = (await challenged(http, served.chain_id, body))["accepts"][0]
        await chain.mint(USDC, wallet, 10 * int(accepts["amount"]))
        generous = {**accepts, "amount": str(5 * int(accepts["amount"]))}
        forged = await payment(
            served.chain_id,
            generous,
            wallet,
            by_wallet(chain, wallet, fresh_client("not its owner")),
        )
        refused = await http.post(
            f"/{served.chain_id}/v1/x402", json=body, headers={"PAYMENT-SIGNATURE": forged}
        )

    assert refused.status_code == 402, refused.text
    assert decoded(refused.headers["PAYMENT-RESPONSE"])["errorReason"] == INVALID_SIGNATURE


async def test_a_payee_holding_usdc_is_quoted_less(chain: LocalChain, served: Served) -> None:
    """Once the payee holds USDC (as the treasury finds), a transfer sets no new slot for it:
    the challenge asks less, and a payment of that lands."""
    client = fresh_client("an x402 client paying a payee that holds USDC")
    body = balance_call(client)
    async with served.interface() as http:
        empty = int((await challenged(http, served.chain_id, body))["accepts"][0]["amount"])
        await chain.mint(USDC, served.payee, 1)
        await chain.set_eth_balance(served.payee, 0)  # the treasury reads, but sends nothing
        await served.treasury()
        await chain.rpc("evm_mine", [])  # the challenge's block read again
        accepts = (await challenged(http, served.chain_id, body))["accepts"][0]
        amount = int(accepts["amount"])
        await chain.mint(USDC, client.address, amount)
        signature = await payment(served.chain_id, accepts, client.address, by_key(client))
        paid = await held(http, served, body, signature)
        status = (await http.get(paid.headers["Location"])).json()

    assert amount < empty
    assert paid.status_code == 200, paid.text
    assert status["status"] == "included", status


async def test_a_call_that_fails_spends_nothing(chain: LocalChain, served: Served) -> None:
    """A call that reverts: the payment is refused, 402, the call's revert in the body, and
    the authorization is never used."""
    reverter = "0x00000000000000000000000000000000000Ba5ed"
    await chain.rpc("anvil_setCode", [reverter, REVERT_ALWAYS])
    client = fresh_client("an x402 client whose call fails")
    body = {"call": {"to": reverter, "data": "0x"}, "gas": hex(CALL_GAS)}
    async with served.interface() as http:
        accepts = (await challenged(http, served.chain_id, body))["accepts"][0]
        await chain.mint(USDC, client.address, int(accepts["amount"]))
        signature = await payment(served.chain_id, accepts, client.address, by_key(client))
        refused = await http.post(
            f"/{served.chain_id}/v1/x402", json=body, headers={"PAYMENT-SIGNATURE": signature}
        )

    assert refused.status_code == 402
    response = decoded(refused.headers["PAYMENT-RESPONSE"])
    assert response["success"] is False
    assert response["errorReason"] == "invalid_transaction_state"
    nonce = decoded(signature)["payload"]["authorization"]["nonce"]
    assert not await authorization_used(chain, client, nonce)


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("underpaid", "invalid_exact_evm_payload_authorization_value_mismatch"),
        ("expired", "invalid_exact_evm_payload_authorization_valid_before"),
        ("elsewhere", "invalid_exact_evm_payload_recipient_mismatch"),
        ("used", "invalid_transaction_state"),
        ("overpaid", "invalid_exact_evm_payload_authorization_value_mismatch"),
        ("forged", "invalid_exact_evm_payload_signature"),
        ("broke", "insufficient_funds"),
        ("permit2", "invalid_payment_requirements"),
        ("malformed wrapper", "invalid_exact_evm_payload_signature"),
    ],
)
async def test_payments_refused(chain: LocalChain, served: Served, case: str, reason: str) -> None:
    """A payment of a unit, one already expired, one to another payee, one whose authorization
    was spent already, one of more than the amount accepted (exact), one signed by someone else,
    one from a client without the USDC, one accepted as Permit2, and one whose signature has
    ERC-6492's magic bytes but no wrapper: each refused, 402, with x402's code."""
    client = fresh_client(f"an x402 client, {case}")
    body = balance_call(client)
    async with served.interface() as http:
        accepts = (await challenged(http, served.chain_id, body))["accepts"][0]
        amount = int(accepts["amount"])
        if case != "broke":
            await chain.mint(USDC, client.address, 2 * amount)
        nonce = os.urandom(32)
        match case:
            case "underpaid":
                signature = await payment(
                    served.chain_id, accepts, client.address, by_key(client), 1
                )
            case "overpaid":
                signature = await payment(
                    served.chain_id, accepts, client.address, by_key(client), amount + 1
                )
            case "forged":
                signature = await payment(
                    served.chain_id, accepts, client.address, by_key(fresh_client("not it"))
                )
            case "broke":
                signature = await payment(served.chain_id, accepts, client.address, by_key(client))
            case "permit2":
                extra = {**accepts["extra"], "assetTransferMethod": "permit2"}
                signature = await payment(
                    served.chain_id, {**accepts, "extra": extra}, client.address, by_key(client)
                )
            case "malformed wrapper":
                signature = await payment(
                    served.chain_id, accepts, client.address, as_given(bytes(10) + MAGIC)
                )
            case "expired":
                signature = await payment(
                    served.chain_id, accepts, client.address, by_key(client), valid_before=1
                )
            case "elsewhere":
                signature = await payment(
                    served.chain_id, accepts, client.address, by_key(client), to=TEST_FLOAT.address
                )
            case _:
                signature = await payment(
                    served.chain_id, accepts, client.address, by_key(client), nonce=nonce
                )
                await spend(chain, signature)
        refused = await http.post(
            f"/{served.chain_id}/v1/x402", json=body, headers={"PAYMENT-SIGNATURE": signature}
        )

    assert refused.status_code == 402, refused.text
    response = decoded(refused.headers["PAYMENT-RESPONSE"])
    assert response["success"] is False
    assert response["errorReason"] == reason


async def spend(chain: LocalChain, signature: str) -> None:
    """The authorization in `signature` used by someone else first: sent to USDC directly."""
    payload = decoded(signature)["payload"]
    auth, raw = payload["authorization"], bytes.fromhex(payload["signature"][2:])
    await chain.send(
        await chain.account(3),
        USDC.address,
        "transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,uint8,bytes32,bytes32)",
        [
            "address",
            "address",
            "uint256",
            "uint256",
            "uint256",
            "bytes32",
            "uint8",
            "bytes32",
            "bytes32",
        ],
        [
            auth["from"],
            auth["to"],
            int(auth["value"]),
            int(auth["validAfter"]),
            int(auth["validBefore"]),
            bytes.fromhex(auth["nonce"][2:]),
            raw[64],
            raw[:32],
            raw[32:64],
        ],
    )


async def test_discovery(served: Served) -> None:
    """/.well-known/x402 lists the chain's x402 endpoint, as the root does; OpenAPI describes
    it, with x402scan's payment info."""
    async with served.interface() as http:
        known = (await http.get("/.well-known/x402")).json()
        root = (await http.get("/")).json()
        openapi = (await http.get("/openapi.json")).json()
        assert "paths" in openapi, openapi

    (chain,) = root["chains"]
    assert known == {"version": 1, "resources": [chain["x402"]]}
    assert chain["x402Tokens"] == [USDC.address]
    operation = openapi["paths"]["/{chain}/v1/x402"]["post"]
    assert operation["x-payment-info"]["protocols"] == ["x402"]
    assert operation["x-payment-info"]["price"]["mode"] == "dynamic"


async def test_a_payment_rejected_is_answered_so_again(chain: LocalChain, served: Served) -> None:
    """A payment taken, then its USDC gone before its batch: rejected, and its held request
    answered 402, insufficient_funds, its status failed. Sent again, it's answered so again,
    not as if taken."""
    client = fresh_client("an x402 client whose USDC goes")
    body = balance_call(client)
    async with served.interface() as http:
        accepts = (await challenged(http, served.chain_id, body))["accepts"][0]
        await chain.mint(USDC, client.address, int(accepts["amount"]))
        signature = await payment(served.chain_id, accepts, client.address, by_key(client))
        sent = sending(http, served, body, signature)
        id_ = submission_id(served, body, signature)
        await taken(http, served, id_, sent)
        await chain.set_token_balance(USDC, client.address, 0)
        await served.close_batch()
        paid = await sent
        failed = await http.get(f"/{served.chain_id}/v1/submissions/{id_}")
        again = await http.post(
            f"/{served.chain_id}/v1/x402", json=body, headers={"PAYMENT-SIGNATURE": signature}
        )

    status = failed.json()
    assert (status["status"], status["stage"]) == ("rejected", "failed"), status
    assert "Retry-After" not in failed.headers
    for each in (paid, again):
        assert each.status_code == 402, each.text
        response = decoded(each.headers["PAYMENT-RESPONSE"])
        assert response["errorReason"] == "insufficient_funds"


async def test_eip3009_on_eth_is_a_configuration_error(chain: LocalChain, workerd: str) -> None:
    """ETH has no EIP-3009 transfer: eip3009 on it is refused, by either relay's configuration."""
    payee = to_checksum_address(await chain.account(PAYEE_ACCOUNT))
    raw = msgspec.to_builtins(relay_config(Setup(), chain.url, payee, BUILDER_URL))
    eth = {"address": NATIVE_ASSET, "eip3009": {"name": "Ether", "version": "1"}}
    raw["tokens"] = [*raw["tokens"], eth]
    with pytest.raises(msgspec.ValidationError, match="eip3009"):
        msgspec.convert(raw, ChainConfig)
    async with httpx.AsyncClient(base_url=workerd) as http:
        refused = await http.post(
            f"/__test/{await chain.chain_id()}/configure",
            json={"config": raw, "floatKey": "0x" + TEST_FLOAT.key.hex()},
        )

    assert refused.status_code == 404, refused.text
    assert "eip3009" in refused.json()["error"]["message"]
