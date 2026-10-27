"""x402 conformance, judged by x402's own SDK (the `x402` package), never by our reading of the
spec: its reference client pays through each relay as any x402 user's would, its models parse
everything we answer, and its facilitator-side bazaar code catalogs us from a real payment.
On anvil, against both relays."""

import asyncio
from pathlib import Path
from typing import Any

import httpx
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak
from x402 import x402Client
from x402.extensions.bazaar import extract_discovery_info, validate_and_extract
from x402.http import decode_payment_required_header, decode_payment_response_header
from x402.http.clients.httpx import x402AsyncTransport
from x402.mechanisms.evm import ClientEvmSigner, EthAccountSigner
from x402.mechanisms.evm.default_assets import find_default_asset
from x402.mechanisms.evm.exact import register_exact_evm_client
from x402.mechanisms.evm.types import TypedDataDomain, TypedDataField
from x402.schemas import PaymentPayload, PaymentRequired

from bruno.config import Eip712Domain, load
from bruno.eip3009 import digest
from bruno.server.front import chain_path
from bruno.tests import smartwallet
from bruno.tests.calibur import fresh_client
from bruno.tests.interface import landing
from bruno.tests.localchain import REVERT_ALWAYS, TEST_FLOAT, USDC, LocalChain
from bruno.tests.served import Served
from bruno.tests.transfers import balance_call, by_key, deployment_json, payment

# x402 v2's error codes (§9): every refusal's errorReason must be one of them
X402_ERRORS = {
    "insufficient_funds",
    "invalid_exact_evm_payload_authorization_valid_after",
    "invalid_exact_evm_payload_authorization_valid_before",
    "invalid_exact_evm_payload_authorization_value_mismatch",
    "invalid_exact_evm_payload_signature",
    "invalid_exact_evm_payload_recipient_mismatch",
    "invalid_network",
    "invalid_payload",
    "invalid_payment_requirements",
    "invalid_scheme",
    "unsupported_scheme",
    "invalid_x402_version",
    "invalid_transaction_state",
    "unexpected_verify_error",
    "unexpected_settle_error",
    "settlement_pending",
}
AUTHORIZATION_USED = keccak(text="AuthorizationUsed(address,bytes32)")


def reference_client(signer: ClientEvmSigner, chain_id: int) -> x402Client:
    """x402's client, signing as `signer`, set up as its users set it up: exact on any EVM
    network. The local
    USDC isn't one of its default assets (Ethereum's is: test_ethereum_usdc_is_a_default), so
    it's allowed the way a user allows a token the SDK doesn't know, uncapped (the SDK's
    default cap is $1 a payment)."""
    reference = x402Client()
    register_exact_evm_client(reference, signer, "eip155:*")
    reference.set_spend_controls(
        {
            "allowed_assets": [{"network": f"eip155:{chain_id}", "asset": USDC.address}],
            "max_amount_per_payment": False,
        }
    )
    return reference


def url(served: Served) -> str:
    return f"/{chain_path(served.chain_id)}/v1/x402"


async def test_the_reference_client_pays(chain: LocalChain, served: Served) -> None:
    """x402's reference client, unmodified, POSTs a call: it takes our 402, chooses our
    requirement, signs the EIP-3009 authorization for the domain we name, and resends, waiting
    maxTimeoutSeconds as a client should. Held until settled: 200, and its own model reads our
    PAYMENT-RESPONSE, `transaction` the one that landed: included, the payee gains the amount,
    and the token logs the authorization used, by the client."""
    client = fresh_client("the x402 reference client")
    await chain.mint(USDC, client.address, 10 * 10**6)
    before = await chain.balance_of(USDC, served.payee)
    async with served.interface() as http:
        challenge = await http.post(url(served), json=balance_call(client))
    wait = challenge.json()["accepts"][0]["maxTimeoutSeconds"]
    transport = x402AsyncTransport(reference_client(EthAccountSigner(client), served.chain_id))
    async with httpx.AsyncClient(
        base_url=served.http.base_url, transport=transport, timeout=wait
    ) as paying:
        sent = asyncio.create_task(paying.post(url(served), json=balance_call(client)))
        paid = await landing(served, sent)
    settled = decode_payment_response_header(paid.headers["PAYMENT-RESPONSE"])
    async with served.interface() as http:
        status = (await http.get(paid.headers["Location"])).json()
    receipt = await chain.rpc("eth_getTransactionReceipt", [status["transactionHash"]])
    used = [
        log
        for log in receipt["logs"]
        if bytes.fromhex(log["topics"][0][2:]) == AUTHORIZATION_USED
        and log["address"].lower() == USDC.address.lower()
    ]

    assert paid.status_code == 200, paid.text
    assert settled.success
    assert settled.transaction == status["transactionHash"]
    assert settled.payer is not None and settled.payer.lower() == client.address.lower()
    assert settled.amount is not None
    assert status["status"] == "included", status
    assert await chain.balance_of(USDC, served.payee) - before == int(settled.amount)
    (log,) = used
    assert log["topics"][1][-40:] == client.address[2:].lower()


class UndeployedSmartWalletSigner:
    """x402's ClientEvmSigner as a Coinbase Smart Wallet that isn't deployed yet: it signs
    EIP-3009's transfer as the wallet will check it, ERC-6492-wrapped with its deployment, as
    viem's and CDP's smart accounts do while undeployed."""

    def __init__(self, chain_id: int, wallet: ChecksumAddress, owner: LocalAccount) -> None:
        self._chain_id = chain_id
        self._wallet = wallet
        self._owner = owner

    @property
    def address(self) -> str:
        return self._wallet

    def sign_typed_data(
        self,
        domain: TypedDataDomain,
        types: dict[str, list[TypedDataField]],
        primary_type: str,
        message: dict[str, Any],
    ) -> bytes:
        assert primary_type == "TransferWithAuthorization"
        nonce = message["nonce"]
        hash_ = digest(
            Eip712Domain(domain.name, domain.version),
            self._chain_id,
            to_checksum_address(domain.verifying_contract),
            to_checksum_address(message["from"]),
            to_checksum_address(message["to"]),
            int(message["value"]),
            int(message["validAfter"]),
            int(message["validBefore"]),
            nonce if isinstance(nonce, bytes) else bytes.fromhex(nonce.removeprefix("0x")),
        )
        return smartwallet.sign_undeployed(self._chain_id, self._wallet, self._owner, hash_)


async def test_the_reference_client_pays_from_an_undeployed_smart_wallet(
    chain: LocalChain, served: Served
) -> None:
    """x402's reference client, unmodified, its signer an undeployed Coinbase Smart Wallet's
    (ERC-6492-wrapped), sends Bruno's body declaring its payer and deployment: the first 402 is
    its price, which it pays once (as it always does), and its transaction deploys the wallet,
    pays and makes the call."""
    owner = fresh_client("the x402 reference client's smart wallet owner")
    wallet = await smartwallet.address_of(chain, owner)
    deployment = smartwallet.deployment(wallet, owner)
    body = {**balance_call(owner), "payer": wallet, "deployment": deployment_json(deployment)}
    async with served.interface() as http:
        challenge = await http.post(url(served), json=body)
    # ten times its price: a deployment costs most on Amsterdam (EIP-8037's state gas)
    await chain.mint(USDC, wallet, 10 * int(challenge.json()["accepts"][0]["amount"]))
    before = await chain.balance_of(USDC, served.payee)
    wait = challenge.json()["accepts"][0]["maxTimeoutSeconds"]
    reference = reference_client(
        UndeployedSmartWalletSigner(served.chain_id, wallet, owner), served.chain_id
    )
    async with httpx.AsyncClient(
        base_url=served.http.base_url, transport=x402AsyncTransport(reference), timeout=wait
    ) as paying:
        paid = await landing(served, asyncio.create_task(paying.post(url(served), json=body)))
    settled = decode_payment_response_header(paid.headers["PAYMENT-RESPONSE"])

    assert paid.status_code == 200, paid.text
    assert settled.success
    assert settled.amount is not None
    assert settled.amount == challenge.json()["accepts"][0]["amount"]
    assert await chain.balance_of(USDC, served.payee) - before == int(settled.amount)
    assert await chain.code(wallet) != "0x"


async def test_the_sdk_reads_every_402_and_response(chain: LocalChain, served: Served) -> None:
    """Every 402 we answer parses as x402's PaymentRequired (v2), and every PAYMENT-RESPONSE as
    its SettleResponse, a refusal's errorReason one of x402's codes: the challenge with a call,
    without one, and each refusal (a unit paid, expired, another payee, a call that fails)."""
    reverter = "0x00000000000000000000000000000000000Ba5ed"
    await chain.rpc("anvil_setCode", [reverter, REVERT_ALWAYS])
    client = fresh_client("an x402 client the SDK reads")
    body = balance_call(client)
    failing = {"call": {"to": reverter, "data": "0x"}, "gas": body["gas"]}
    async with served.interface() as http:
        probe = await http.post(url(served))
        challenge = await http.post(url(served), json=body)
        accepts = challenge.json()["accepts"][0]
        await chain.mint(USDC, client.address, 10 * int(accepts["amount"]))
        refusals = [
            await http.post(url(served), json=body, headers={"PAYMENT-SIGNATURE": header})
            for header in (
                await payment(served.chain_id, accepts, client.address, by_key(client), 1),
                await payment(
                    served.chain_id, accepts, client.address, by_key(client), valid_before=1
                ),
                await payment(
                    served.chain_id, accepts, client.address, by_key(client), to=TEST_FLOAT.address
                ),
            )
        ]
        failed_challenge = await http.post(url(served), json=failing)
        failed = await http.post(
            url(served),
            json=failing,
            headers={
                "PAYMENT-SIGNATURE": await payment(
                    served.chain_id,
                    failed_challenge.json()["accepts"][0],
                    client.address,
                    by_key(client),
                )
            },
        )

    for response in (probe, challenge, *refusals, failed):
        assert response.status_code == 402, response.text
        required = response.headers.get("PAYMENT-REQUIRED")
        if required is not None:
            parsed = decode_payment_required_header(required)
            assert isinstance(parsed, PaymentRequired), parsed
            assert parsed.x402_version == 2
        answer = response.headers.get("PAYMENT-RESPONSE")
        if answer is not None:
            settled = decode_payment_response_header(answer)
            assert not settled.success
            assert settled.error_reason in X402_ERRORS, settled.error_reason
    assert all("PAYMENT-RESPONSE" in r.headers for r in (*refusals, failed))


async def test_a_facilitator_would_catalog_us(chain: LocalChain, served: Served) -> None:
    """x402's facilitator-side bazaar code, on a payment its client made against our 402: our
    declaration is valid, and the resource is discovered as ours: its URL, POST, our schema."""
    client = fresh_client("an x402 client a facilitator sees")
    await chain.mint(USDC, client.address, 10 * 10**6)
    async with served.interface() as http:
        challenge = await http.post(url(served), json=balance_call(client))
    required = decode_payment_required_header(challenge.headers["PAYMENT-REQUIRED"])
    assert isinstance(required, PaymentRequired)
    reference = reference_client(EthAccountSigner(client), served.chain_id)
    payload = await reference.create_payment_payload(required)
    assert isinstance(payload, PaymentPayload), payload  # v2: what our 402 asks for
    assert required.extensions is not None
    validated = validate_and_extract(required.extensions["bazaar"])
    discovered = extract_discovery_info(payload, payload.accepted)

    assert validated.valid, validated.errors
    assert discovered is not None
    assert discovered.resource_url.endswith(url(served))
    assert discovered.method == "POST"


def test_ethereum_usdc_is_a_default() -> None:
    """Out of the box, x402's clients pay only in their default assets. Ethereum's USDC is one,
    with the EIP-712 domain our default configuration names, so an unconfigured client pays our
    Ethereum relay. Sepolia's USDC isn't: a user there allows it themselves."""
    chains = load(Path(__file__).parents[2] / "config.default.json").chains
    for chain_id, defaulted in ((1, True), (11_155_111, False)):
        (token,) = (t for t in chains[chain_id].tokens if t.eip3009 is not None)
        default = find_default_asset(token.address, f"eip155:{chain_id}")
        assert (default is not None) == defaulted, chain_id
        if default is not None and token.eip3009 is not None:
            assert (default["name"], default["version"]) == (
                token.eip3009.name,
                token.eip3009.version,
            )
