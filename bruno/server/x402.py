"""x402 over HTTP (x402 v2, its HTTP transport, and `exact` on EVM with EIP-3009): a call
paid for by a signed transferWithAuthorization, which the runner makes in the same
transaction as the call. The relay is its own facilitator: nothing is settled apart from the
call, so a payment is spent only if the call succeeds and is included.

    POST /{chain}/v1/x402                the call; 402 with what to pay
    POST /{chain}/v1/x402 + PAYMENT-SIGNATURE   held until included: 200, its transaction

A request without a body, or with one that doesn't parse, is answered 402 too, priced for a
reference call, so that discovery's probes reach the challenge."""

import base64
from functools import cache

import httpx
import msgspec
from eth_abi.abi import encode
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from ..chain import ChainError
from ..common import BrunoError
from ..eip3009 import signer_of, transfer_digest
from ..evm import has_code
from ..net import jsonrpc
from ..relay import Quotes, Relay
from ..submission import InvalidSubmissionError, Submission, authorities
from ..tokens import balance_of_call
from .front import chain_path
from .operations import TITLE
from .wire import (
    Call,
    Extension,
    PaymentPayload,
    PaymentRequired,
    PaymentRequirements,
    ResourceInfo,
    SettleResponse,
    TokenDomain,
    X402Params,
    X402Result,
    data,
)

X402_VERSION = 2
_MAX_UINT256 = 2**256 - 1
SCHEME = "exact"
# the call a challenge without one is priced for: one EIP-7702 authorization, from an account
# that exists, and a call of 150,000 gas and 512 bytes
REFERENCE_GAS = 150_000
REFERENCE_CALL_BYTES = 512
REFERENCE_AUTHORITY: ChecksumAddress = to_checksum_address(
    "0x000000000000000000000000000000000000dEaD"
)
DESCRIPTION = (
    "Sends one EVM call and pays its gas, paid by an EIP-3009 authorization that's settled in "
    "the same transaction as the call: only if the call succeeds and is included. Post the "
    "call (to, data, its gas, an EIP-7702 authorization if any) for its price. Without one, "
    "the price is a reference call's: one authorization, 150,000 gas, 512 bytes of data."
)
TAGS = ("gas", "relay", "ethereum", "eip-7702", "gasless")
# where the bazaar extension's example status URL is
EXAMPLE_ORIGIN = "https://relay.example"
# x402 v2's error codes (§9)
INVALID_PAYLOAD = "invalid_payload"
INVALID_VERSION = "invalid_x402_version"
INVALID_SCHEME = "invalid_scheme"
INVALID_NETWORK = "invalid_network"
INVALID_REQUIREMENTS = "invalid_payment_requirements"
RECIPIENT_MISMATCH = "invalid_exact_evm_payload_recipient_mismatch"
VALUE_MISMATCH = "invalid_exact_evm_payload_authorization_value_mismatch"
NOT_YET_VALID = "invalid_exact_evm_payload_authorization_valid_after"
EXPIRED = "invalid_exact_evm_payload_authorization_valid_before"
TRANSACTION_STATE = "invalid_transaction_state"
INSUFFICIENT_FUNDS = "insufficient_funds"
INVALID_SIGNATURE = "invalid_exact_evm_payload_signature"
# the only asset transfer method and payment flow taken: EIP-3009, settled with the call
ASSET_TRANSFER_METHOD = "eip3009"
PAYMENT_FLOW = "authorization"


class PaymentRefusedError(BrunoError):
    """A payment that isn't taken, with x402's code for why (`reason`)."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class MalformedPaymentError(BrunoError):
    """A PAYMENT-SIGNATURE header that isn't a payment payload."""


def network(chain_id: int) -> str:
    """The chain's CAIP-2 identifier."""
    return f"eip155:{chain_id}"


def x402_url(origin: str, chain_id: int) -> str:
    return f"{origin}/{chain_path(chain_id)}/v1/x402"


def params_of(body: bytes) -> X402Params | None:
    """The call in a request's body: None if there's none, or it doesn't parse."""
    try:
        return msgspec.json.decode(body, type=X402Params)
    except msgspec.DecodeError, msgspec.ValidationError:
        return None


def payload_of(header: str) -> PaymentPayload:
    """A PAYMENT-SIGNATURE header's payment payload: base64 of its JSON, strictly (padded, the
    base64 alphabet only), its authorization's numbers each a uint256."""
    try:
        payload = msgspec.json.decode(base64.b64decode(header, validate=True), type=PaymentPayload)
    except (ValueError, msgspec.DecodeError, msgspec.ValidationError) as e:
        # ValueError: binascii's, or a header that isn't ASCII
        raise MalformedPaymentError(f"PAYMENT-SIGNATURE: {e}") from e
    authorization = payload.payload.authorization
    for name, value in (
        ("value", authorization.value),
        ("validAfter", authorization.valid_after),
        ("validBefore", authorization.valid_before),
    ):
        if int(value) > _MAX_UINT256:
            raise MalformedPaymentError(f"PAYMENT-SIGNATURE: {name} isn't a uint256")
    return payload


def encoded(value: PaymentRequired | SettleResponse) -> str:
    """A header's value: base64 of the JSON."""
    return base64.b64encode(msgspec.json.encode(value)).decode()


async def challenge(
    relay: Relay,
    url: str,
    params: X402Params | None,
    error: str | None = None,
    least: tuple[ChecksumAddress, int] | None = None,
) -> PaymentRequired:
    """What to pay for the call in `params` (a reference call's price without one): an `exact`
    requirement in each token accepted over x402. `least`: a token's amount the payment must
    be at least, if more than its quote."""
    if params is None:
        quotes = await relay.x402_quote(
            REFERENCE_GAS, REFERENCE_CALL_BYTES, REFERENCE_AUTHORITY, existing=True
        )
    else:
        most = relay.config.max_authorizations
        if len(params.authorization_list) > most:
            raise InvalidSubmissionError("authorizationList", f"over {most}")
        authority = next(
            iter(authorities([a.signed() for a in params.authorization_list[:1]])), None
        )
        quotes = await relay.x402_quote(
            int(params.gas, 16),
            len(bytes.fromhex(params.call.data[2:])),
            authority,
            params.payer_of(),
        )
    return PaymentRequired(
        x402_version=X402_VERSION,
        resource=ResourceInfo(
            url=url,
            description=DESCRIPTION,
            mime_type="application/json",
            service_name=TITLE,
            tags=TAGS,
        ),
        accepts=_requirements(relay, quotes, least),
        error=error,
        extensions={"bazaar": bazaar()},
    )


def _requirements(
    relay: Relay, quotes: Quotes, least: tuple[ChecksumAddress, int] | None
) -> tuple[PaymentRequirements, ...]:
    requirements: list[PaymentRequirements] = []
    for q in quotes.quotes:
        amount = q.amount if least is None or least[0] != q.token else max(q.amount, least[1])
        domain = relay.domains[q.token]
        requirements.append(
            PaymentRequirements(
                scheme=SCHEME,
                network=network(relay.chain.chain_id),
                amount=str(amount),
                asset=q.token,
                pay_to=relay.payee,
                max_timeout_seconds=relay.x402_timeout_seconds,
                extra=TokenDomain(domain.name, domain.version),
            )
        )
    return tuple(requirements)


async def take(relay: Relay, params: X402Params, payload: PaymentPayload) -> Submission:
    """The paid submission `payload` makes of the call in `params`, checked against what's
    asked: raise PaymentRefusedError why not."""
    if payload.x402_version != X402_VERSION:
        raise PaymentRefusedError(INVALID_VERSION, f"x402Version {payload.x402_version}")
    accepted = payload.accepted
    if accepted.scheme != SCHEME:
        raise PaymentRefusedError(INVALID_SCHEME, f"scheme {accepted.scheme}")
    if accepted.network != network(relay.chain.chain_id):
        raise PaymentRefusedError(INVALID_NETWORK, f"network {accepted.network}")
    token = to_checksum_address(accepted.asset)
    if token not in relay.domains:
        raise PaymentRefusedError(INVALID_REQUIREMENTS, f"{token} isn't accepted over x402")
    authorization = payload.payload.authorization
    payee = to_checksum_address(authorization.to)
    if payee != relay.payee or to_checksum_address(accepted.pay_to) != relay.payee:
        raise PaymentRefusedError(RECIPIENT_MISMATCH, f"the payee is {relay.payee}")
    extra = accepted.extra
    method = None if extra is None else extra.asset_transfer_method
    flow = None if extra is None else extra.payment_flow
    if method not in (None, ASSET_TRANSFER_METHOD) or flow not in (None, PAYMENT_FLOW):
        raise PaymentRefusedError(
            INVALID_REQUIREMENTS, f"only {ASSET_TRANSFER_METHOD}, {PAYMENT_FLOW} is taken"
        )
    # exact: the transfer is of the amount accepted, no more and no less
    amount = accepted.amount
    if not (amount.isascii() and amount.isdecimal()) or int(amount) != int(authorization.value):
        raise PaymentRefusedError(VALUE_MISMATCH, f"value isn't the amount {amount}")
    transfer = payload.transfer()
    try:
        deadline = await relay.x402_deadline(transfer.valid_after, transfer.valid_before)
    except InvalidSubmissionError as e:
        reason = NOT_YET_VALID if e.field == "validAfter" else EXPIRED
        raise PaymentRefusedError(reason, str(e)) from e
    try:
        return params.submission(token, payee, deadline, transfer)
    except InvalidSubmissionError as e:
        if e.field != "payment":  # the request's own (its deployment), not the payment's
            raise
        raise PaymentRefusedError(INVALID_SIGNATURE, str(e)) from e


async def refusal(relay: Relay, submission: Submission) -> str:
    """x402's code for why a paid submission whose simulation failed is refused: the signature
    isn't the payer's (a payer with no code: its key's, recovered; one with code: as its
    ERC-1271 isValidSignature answers its inner signature; one not deployed yet, its signature
    ERC-6492-wrapped, can't be asked); the payer holds less than the value; else the
    transaction's state. Read only once it has failed, in one batch: a payment that succeeds
    reads nothing for it."""
    transfer = submission.transfer
    if transfer is None:
        return TRANSACTION_STATE
    domain = relay.domains[submission.token]
    hash_ = transfer_digest(domain, relay.chain.chain_id, submission.token, transfer)
    try:
        balance, code, answer = await relay.chain.batch(
            [
                balance_of_call(submission.token, transfer.payer),
                jsonrpc.Call("eth_getCode", [transfer.payer, "latest"]),
                is_valid_signature_call(transfer.payer, hash_, transfer.inner),
            ]
        )
        held = int(balance.value(), 16)
        contract = has_code(code.value())
    except ChainError, jsonrpc.JsonRpcError, httpx.HTTPError, TypeError, ValueError:
        return TRANSACTION_STATE  # unread: the failure as it is
    if contract:
        valid = answer.error is None and _magic(answer.value())
    elif transfer.deployment is not None:
        valid = True  # not deployed: nothing to ask without deploying it
    else:
        valid = signer_of(domain, relay.chain.chain_id, submission.token, transfer) == (
            transfer.payer
        )
    if not valid:
        return INVALID_SIGNATURE
    if held < transfer.value:
        return INSUFFICIENT_FUNDS
    return TRANSACTION_STATE


# ERC-1271: isValidSignature(bytes32, bytes), and what it answers for a valid signature
_IS_VALID_SIGNATURE = keccak(text="isValidSignature(bytes32,bytes)")[:4]
_ERC1271_MAGIC = _IS_VALID_SIGNATURE


def is_valid_signature_call(account: str, hash_: bytes, signature: bytes) -> jsonrpc.Call:
    """eth_call of `account`'s ERC-1271 isValidSignature(hash_, signature)."""
    data = _IS_VALID_SIGNATURE + encode(["bytes32", "bytes"], [hash_, signature])
    return jsonrpc.Call("eth_call", [{"to": account, "data": "0x" + data.hex()}, "latest"])


def _magic(answer: str) -> bool:
    """Whether an isValidSignature answer is ERC-1271's magic value: its first four bytes."""
    return len(answer) >= 10 and bytes.fromhex(answer[2:10]) == _ERC1271_MAGIC


def settled(
    relay: Relay, submission: Submission, status_url: str, transaction: str
) -> SettleResponse:
    """A payment settled: its submission's `transaction` included, which made the transfer and
    the call; its status (`status_url`) follows it on to finality."""
    transfer = submission.transfer
    return SettleResponse(
        success=True,
        transaction=transaction,
        network=network(relay.chain.chain_id),
        payer=None if transfer is None else transfer.payer,
        amount=str(submission.amount),
        extensions={"status": status_extension(status_url, data(submission.id))},
    )


def refused(relay: Relay, reason: str, payer: str | None = None) -> SettleResponse:
    return SettleResponse(
        success=False,
        transaction="",
        network=network(relay.chain.chain_id),
        error_reason=reason,
        payer=payer,
    )


# the status extension's schema: a status URL, and the submission's id
STATUS_SCHEMA: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"url": {"type": "string"}, "id": {"type": "string"}},
    "required": ["url", "id"],
}


def status_extension(url: str, id_: str) -> Extension:
    """Where a taken payment's submission is followed: its status URL and id."""
    return Extension(info={"url": url, "id": id_}, schema=STATUS_SCHEMA)


def exported() -> dict[str, object]:
    """What the Worker serves as the Python does (make vectors): the description, tags and
    bazaar extension of its challenge, the status extension's schema, and the OpenAPI operation
    without its minimum price, which the Worker reads from its configuration."""
    operation, schemas = openapi_operation(None)
    return {
        "description": DESCRIPTION,
        "tags": list(TAGS),
        "reference": {
            "gas": REFERENCE_GAS,
            "callBytes": REFERENCE_CALL_BYTES,
            "authority": REFERENCE_AUTHORITY,
        },
        "bazaar": msgspec.to_builtins(bazaar()),
        "statusSchema": STATUS_SCHEMA,
        "operation": operation,
        "schemas": schemas,
    }


@cache
def bazaar() -> Extension:
    """The `bazaar` extension: how the endpoint is called, for discovery's catalogs. The same
    for every chain: the Worker serves it as exported (make vectors)."""
    body_schema, defs = _body_schema()
    example = msgspec.to_builtins(
        X402Params(call=Call(REFERENCE_AUTHORITY, "0x"), gas=hex(REFERENCE_GAS))
    )
    status = f"{EXAMPLE_ORIGIN}/ethereum/v1/submissions/0x{'00' * 32}"
    return Extension(
        info={
            "input": {"type": "http", "method": "POST", "bodyType": "json", "body": example},
            "output": {"type": "json", "example": {"id": "0x" + "00" * 32, "statusUrl": status}},
        },
        schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "input": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "const": "http"},
                        "method": {"type": "string", "enum": ["POST"]},
                        "bodyType": {"type": "string", "enum": ["json"]},
                        "body": body_schema,
                    },
                    "required": ["type", "method", "bodyType", "body"],
                    "additionalProperties": False,
                },
                "output": {
                    "type": "object",
                    "properties": {"type": {"type": "string"}, "example": {"type": "object"}},
                    "required": ["type"],
                },
            },
            "required": ["input"],
            "$defs": defs,
        },
    )


@cache
def _body_schema() -> tuple[dict[str, object], dict[str, object]]:
    """The request body's JSON Schema, and the definitions it refers to (`#/$defs/...`)."""
    (schema,), defs = msgspec.json.schema_components([X402Params], ref_template="#/$defs/{name}")
    return dict(schema), dict(defs)


def result(origin: str, relay: Relay, submission: Submission) -> X402Result:
    id_ = data(submission.id)
    return X402Result(id=id_, status_url=status_url(origin, relay.chain.chain_id, id_))


def status_url(origin: str, chain_id: int, id_: str) -> str:
    return f"{origin}/{chain_path(chain_id)}/v1/submissions/{id_}"


def _any_json(kind: type) -> dict[str, object]:
    """JSON Schema for an extension's `info` values: any JSON."""
    if kind is object:  # an empty schema would read as none: msgspec wants something
        return {"description": "Any JSON value"}
    raise NotImplementedError(f"no JSON Schema for {kind}")


def openapi_operation(min_usd: str | None) -> tuple[dict[str, object], dict[str, object]]:
    """The x402 operation as OpenAPI describes it, with x402scan's `x-payment-info` (its price
    dynamic, at least `min_usd` if known); and the schemas it refers to, for the document's
    components."""
    (body, _, _), components = msgspec.json.schema_components(
        [X402Params, PaymentRequired, X402Result],
        schema_hook=_any_json,
        ref_template="#/components/schemas/{name}",
    )
    price: dict[str, str] = {"mode": "dynamic", "currency": "USD"}
    if min_usd is not None:
        price["min"] = min_usd
    json = "application/json"
    operation: dict[str, object] = {
        "post": {
            "summary": "x402",
            "description": DESCRIPTION,
            "parameters": [
                {
                    "name": "chain",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string"},
                    "description": "The chain's id, or its network's name",
                },
                {
                    "name": "PAYMENT-SIGNATURE",
                    "in": "header",
                    "required": False,
                    "schema": {"type": "string"},
                    "description": "x402's payment payload, base64 of its JSON",
                },
            ],
            "requestBody": {"required": False, "content": {json: {"schema": body}}},
            "responses": {
                "200": {
                    "description": "Settled: its transaction included (PAYMENT-RESPONSE)",
                    "content": {json: {"schema": {"$ref": "#/components/schemas/X402Result"}}},
                },
                "402": {
                    "description": "What to pay (PAYMENT-REQUIRED); why a payment wasn't taken",
                    "content": {json: {"schema": {"$ref": "#/components/schemas/PaymentRequired"}}},
                },
            },
            "x-payment-info": {"protocols": ["x402"], "price": price},
        }
    }
    return operation, dict(components)
