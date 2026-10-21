"""x402 on anvil, against both relays: a call paid by a signed EIP-3009 authorization of
the local USDC (FiatTokenV2_2), settled by the runner in the call's own transaction. The
challenge, a payment taken and landed, the payments refused, and the treasury keeping the
payee's balance slot set."""

import base64
import json
import os
import time
from typing import Any

import httpx
import pytest
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress, Hash32
from eth_utils.crypto import keccak
from jsonschema import Draft202012Validator

from bruno.eip3009 import digest
from bruno.server.front import chain_path
from bruno.tests.calibur import fresh_client
from bruno.tests.localchain import REVERT_ALWAYS, TEST_FLOAT, USDC, LocalChain
from bruno.tests.served import MAX_DEADLINE_BLOCKS, USDC_DOMAIN, Served
from bruno.tests.test_server import a_gwei_base_fee, approve_router, probe

# the client's call: its own USDC balance read, value 0, which succeeds whatever it holds
CALL_GAS = 50_000
# how long a test's authorizations are valid for
VALID_SECONDS = 600


def balance_call(client: LocalAccount) -> dict[str, object]:
    """A call that succeeds for anyone: the client's USDC balance, read."""
    data = "0x" + (keccak(text="balanceOf(address)")[:4] + bytes(12)).hex() + client.address[2:]
    return {"call": {"to": USDC.address, "data": data}, "gas": hex(CALL_GAS)}


def decoded(header: str) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(base64.b64decode(header))
    return result


def payment(
    chain_id: int,
    accepted: dict[str, Any],
    client: LocalAccount,
    value: int | None = None,
    *,
    to: ChecksumAddress | None = None,
    valid_before: int | None = None,
    nonce: bytes | None = None,
) -> str:
    """A PAYMENT-SIGNATURE: `client`'s authorization of `accepted`'s amount (or `value`) to
    its payTo (or `to`), signed for the local USDC's domain."""
    amount = int(accepted["amount"]) if value is None else value
    payee = to if to is not None else accepted["payTo"]
    before = valid_before if valid_before is not None else int(time.time()) + VALID_SECONDS
    nonce = nonce if nonce is not None else os.urandom(32)
    signed = client.unsafe_sign_hash(
        Hash32(
            digest(
                USDC_DOMAIN, chain_id, USDC.address, client.address, payee, amount, 0, before, nonce
            )
        )
    )
    payload = {
        "x402Version": 2,
        "accepted": accepted,
        "payload": {
            "signature": "0x"
            + (signed.r.to_bytes(32) + signed.s.to_bytes(32) + bytes([signed.v])).hex(),
            "authorization": {
                "from": client.address,
                "to": payee,
                "value": str(amount),
                "validAfter": "0",
                "validBefore": str(before),
                "nonce": "0x" + nonce.hex(),
            },
        },
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


async def challenged(http: httpx.AsyncClient, chain_id: int, body: object) -> dict[str, Any]:
    """The 402's PAYMENT-REQUIRED for `body`, checked to be the response's body too."""
    response = await http.post(f"/{chain_id}/v1/x402", json=body)
    assert response.status_code == 402, response.text
    required = decoded(response.headers["PAYMENT-REQUIRED"])
    assert required == response.json()
    return required


async def authorization_used(chain: LocalChain, client: LocalAccount, nonce: str) -> bool:
    used = await chain.call(
        USDC.address,
        "authorizationState(address,bytes32)",
        ["address", "bytes32"],
        [client.address, bytes.fromhex(nonce[2:])],
    )
    return int.from_bytes(used) == 1


async def land(served: Served) -> None:
    """The batch closed, bundled for the next block, the block built, and settled."""
    await served.close_batch()
    await served.on_block(await served.head())
    await served.builder.build_block()
    await served.on_block(await served.head())


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
    assert accepts["maxTimeoutSeconds"] == MAX_DEADLINE_BLOCKS * 12
    assert accepts["extra"] == {"name": USDC_DOMAIN.name, "version": USDC_DOMAIN.version}
    # the reference call is larger: an authorization, 150,000 gas, 512 bytes
    assert int(probed["accepts"][0]["amount"]) > int(accepts["amount"]) > 0
    for each in (required, probed):
        bazaar = each["extensions"]["bazaar"]
        Draft202012Validator(bazaar["schema"]).validate(bazaar["info"])


async def test_a_payment_is_taken_and_lands(chain: LocalChain, served: Served) -> None:
    """A client holding only USDC, its account never used, pays the challenge's amount: 200,
    acknowledged with no transaction yet and its status URL; the same payment again is the
    same submission. It lands: the payee gains the amount, the authorization is used."""
    client = fresh_client("a paying x402 client")
    body = balance_call(client)
    async with served.interface() as http:
        accepts = (await challenged(http, served.chain_id, body))["accepts"][0]
        amount = int(accepts["amount"])
        await chain.mint(USDC, client.address, amount)
        signature = payment(served.chain_id, accepts, client)
        before = await chain.balance_of(USDC, served.payee)
        url = f"/{served.chain_id}/v1/x402"
        paid = await http.post(url, json=body, headers={"PAYMENT-SIGNATURE": signature})
        again = await http.post(url, json=body, headers={"PAYMENT-SIGNATURE": signature})
        await land(served)
        status = (await http.get(paid.headers["Location"])).json()

    assert paid.status_code == 200, paid.text
    settled = decoded(paid.headers["PAYMENT-RESPONSE"])
    assert settled["success"] is True
    assert settled["transaction"] == ""
    assert settled["payer"] == client.address
    assert settled["extensions"]["status"]["info"]["url"] == paid.json()["statusUrl"]
    assert paid.json()["statusUrl"].endswith(paid.headers["Location"])
    assert again.status_code == 200
    assert again.json()["id"] == paid.json()["id"]
    assert status["status"] == "included", status
    assert await chain.balance_of(USDC, served.payee) - before == amount
    nonce = json.loads(base64.b64decode(signature))["payload"]["authorization"]["nonce"]
    assert await authorization_used(chain, client, nonce)


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
        signature = payment(served.chain_id, accepts, client)
        paid = await http.post(
            f"/{served.chain_id}/v1/x402", json=body, headers={"PAYMENT-SIGNATURE": signature}
        )
        assert paid.status_code == 200, (paid.text, paid.headers.get("PAYMENT-RESPONSE"))
        await land(served)
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
        signature = payment(served.chain_id, accepts, client)
        refused = await http.post(
            f"/{served.chain_id}/v1/x402", json=body, headers={"PAYMENT-SIGNATURE": signature}
        )

    assert refused.status_code == 402
    response = decoded(refused.headers["PAYMENT-RESPONSE"])
    assert response["success"] is False
    assert response["errorReason"] == "invalid_transaction_state"
    nonce = json.loads(base64.b64decode(signature))["payload"]["authorization"]["nonce"]
    assert not await authorization_used(chain, client, nonce)


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("underpaid", "invalid_exact_evm_payload_authorization_value_mismatch"),
        ("expired", "invalid_exact_evm_payload_authorization_valid_before"),
        ("elsewhere", "invalid_exact_evm_payload_recipient_mismatch"),
        ("used", "invalid_transaction_state"),
    ],
)
async def test_payments_refused(chain: LocalChain, served: Served, case: str, reason: str) -> None:
    """A payment of a unit, one already expired, one to another payee, and one whose
    authorization was spent already: each refused, 402, with x402's code."""
    client = fresh_client(f"an x402 client, {case}")
    body = balance_call(client)
    async with served.interface() as http:
        accepts = (await challenged(http, served.chain_id, body))["accepts"][0]
        await chain.mint(USDC, client.address, 2 * int(accepts["amount"]))
        nonce = os.urandom(32)
        match case:
            case "underpaid":
                signature = payment(served.chain_id, accepts, client, 1)
            case "expired":
                signature = payment(served.chain_id, accepts, client, valid_before=1)
            case "elsewhere":
                signature = payment(served.chain_id, accepts, client, to=TEST_FLOAT.address)
            case _:
                signature = payment(served.chain_id, accepts, client, nonce=nonce)
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
    payload = json.loads(base64.b64decode(signature))["payload"]
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


async def test_the_treasury_keeps_a_unit_of_usdc(chain: LocalChain, served: Served) -> None:
    """The float empty, the payee holding a few USDC and its reserve: the treasury converts
    all of it but one base unit, so the payee's balance slot stays set."""
    await a_gwei_base_fee(chain)
    await approve_router(chain, served.payee)
    await chain.set_eth_balance(TEST_FLOAT.address, 0)
    await chain.mint(USDC, served.payee, 5 * 10**6)
    _, reserve = await probe(chain, served)
    await chain.set_eth_balance(served.payee, reserve)
    await served.treasury()
    await served.builder.build_block()

    assert await chain.balance_of(USDC, served.payee) == 1


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
