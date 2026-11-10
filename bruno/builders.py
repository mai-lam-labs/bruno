"""Sending bundles to builders: eth_sendBundle, signed with an X-Flashbots-Signature for a
builder that wants one.

The signature is the identity's EIP-191 signature of the hex keccak256 of the exact request
body, sent as `address:signature` (Flashbots' convention, which BuilderNet and zbuilder
also take). The identity holds no funds: it's who the builder rate-limits and credits."""

import json
from dataclasses import dataclass

import httpx
import msgspec
from eth_account.messages import encode_defunct
from eth_account.signers.local import LocalAccount
from eth_utils.crypto import keccak

from .bundle import Mode
from .units import Seconds

SIGNATURE_HEADER = "X-Flashbots-Signature"
# how long a builder is given to take a bundle: within the slot it's for
BUILDER_TIMEOUT = Seconds(4)


@dataclass(frozen=True, slots=True)
class Builder:
    """A builder's bundle endpoint, the mode it's sent bundles in, and the identity that
    signs them, if it wants one."""

    url: str
    mode: Mode
    identity: LocalAccount | None = None


class BuilderError(Exception):
    """The builder refused the bundle: its HTTP status, or its error answer. Not a BrunoError:
    a builder failing is unexpected, not a client's doing."""


def flashbots_signature(body: bytes, identity: LocalAccount) -> str:
    """The X-Flashbots-Signature header's value for `body`."""
    digest = "0x" + keccak(body).hex()
    signature = identity.sign_message(encode_defunct(text=digest)).signature
    return f"{identity.address}:0x{bytes(signature).hex()}"


class _Error(msgspec.Struct, frozen=True):
    code: int
    message: str


class _Answer(msgspec.Struct, frozen=True):
    """A builder's JSON-RPC answer: its result, or its error."""

    result: object = None
    error: _Error | None = None


async def send_bundle(
    client: httpx.AsyncClient, builder: Builder, params: dict[str, object]
) -> object:
    """eth_sendBundle with `params` to `builder`, signed by its identity if it has one: its
    result, typically the bundle's hash, or BuilderError."""
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "eth_sendBundle", "params": [params]}
    ).encode()
    headers = {"content-type": "application/json"}
    if builder.identity is not None:
        headers[SIGNATURE_HEADER] = flashbots_signature(body, builder.identity)
    response = await client.post(
        builder.url, content=body, headers=headers, timeout=BUILDER_TIMEOUT.seconds
    )
    if not response.is_success:
        raise BuilderError(f"{builder.url}: HTTP {response.status_code}")
    try:
        answer = msgspec.json.decode(response.content, type=_Answer)
    except (msgspec.ValidationError, msgspec.DecodeError) as e:
        raise BuilderError(f"{builder.url}: not an answer: {e}") from e
    # an answer may carry "error": null beside its result: no error
    if answer.error is not None:
        raise BuilderError(f"{builder.url}: {answer.error.code} {answer.error.message}")
    return answer.result
