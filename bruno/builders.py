"""Sending bundles to builders: eth_sendBundle, signed with an X-Flashbots-Signature for a
builder that wants one.

The signature is the identity's EIP-191 signature of the hex keccak256 of the exact request
body, sent as `address:signature` (Flashbots' convention, which BuilderNet and zbuilder
also take). The identity holds no funds: it's who the builder rate-limits and credits."""

import json
from dataclasses import dataclass
from typing import Any

import httpx
from eth_account.messages import encode_defunct
from eth_account.signers.local import LocalAccount
from eth_utils.crypto import keccak

from .bundle import Mode
from .net.jsonrpc import JsonRpcError

SIGNATURE_HEADER = "X-Flashbots-Signature"
# how long a builder is given to take a bundle: within the slot it's for
BUILDER_TIMEOUT_SECONDS = 4.0


@dataclass(frozen=True, slots=True)
class Builder:
    """A builder's bundle endpoint, the mode it's sent bundles in, and the identity that
    signs them, if it wants one."""

    url: str
    mode: Mode
    identity: LocalAccount | None = None


def flashbots_signature(body: bytes, identity: LocalAccount) -> str:
    """The X-Flashbots-Signature header's value for `body`."""
    digest = "0x" + keccak(body).hex()
    signature = identity.sign_message(encode_defunct(text=digest)).signature
    return f"{identity.address}:0x{bytes(signature).hex()}"


async def send_bundle(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, object],
    identity: LocalAccount | None = None,
) -> object:
    """eth_sendBundle with `params` to the builder at `url`, signed by `identity` if given:
    the builder's result (typically its bundle hash), or JsonRpcError."""
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "eth_sendBundle", "params": [params]}
    ).encode()
    headers = {"content-type": "application/json"}
    if identity is not None:
        headers[SIGNATURE_HEADER] = flashbots_signature(body, identity)
    response = await client.post(
        url, content=body, headers=headers, timeout=BUILDER_TIMEOUT_SECONDS
    )
    response.raise_for_status()
    answer: dict[str, Any] = response.json()
    if (error := answer.get("error")) is not None:
        raise JsonRpcError("eth_sendBundle", error["code"], error["message"], error.get("data"))
    return answer["result"]
