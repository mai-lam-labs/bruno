"""EIP-3009 transfers as the tests make them: a payer's authorization of a token, signed by a
key, by a Coinbase Smart Wallet (ERC-1271) or by one not deployed yet (ERC-6492), and written as
a submission's `transfer` or as x402's PAYMENT-SIGNATURE. And the call such a payment pays for,
one that succeeds for anyone."""

import base64
import json
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress, Hash32
from eth_utils.crypto import keccak

from bruno.config import Eip712Domain
from bruno.eip3009 import TransferAuthorization, digest
from bruno.erc6492 import Deployment
from bruno.tests import kernel, smartwallet
from bruno.tests.localchain import USDC, USDC_DOMAIN, LocalChain

# the client's call: its own USDC balance read, value 0, which succeeds whatever it holds
CALL_GAS = 50_000
# its calldata's length: a selector and an address
CALL_BYTES = 36
# how long a test's authorizations are valid for
VALID_SECONDS = 600
# an authorization's validBefore that never comes
NEVER = 2**256 - 1

# what signs a transfer: the payer's signature of its EIP-712 hash
type Signs = Callable[[bytes], Awaitable[bytes]]


def by_key(key: LocalAccount) -> Signs:
    """`key`'s signature of a hash: 65 bytes."""

    async def sign(hash_: bytes) -> bytes:
        return bytes(key.unsafe_sign_hash(Hash32(hash_)).signature)

    return sign


def by_wallet(chain: LocalChain, wallet: ChecksumAddress, owner: LocalAccount) -> Signs:
    """`owner`'s signature of a hash as Coinbase Smart Wallet `wallet` checks it (ERC-1271)."""

    async def sign(hash_: bytes) -> bytes:
        return await smartwallet.sign(chain, wallet, owner, hash_)

    return sign


def by_undeployed_wallet(chain_id: int, wallet: ChecksumAddress, owner: LocalAccount) -> Signs:
    """`owner`'s signature of a hash as Coinbase Smart Wallet `wallet`'s before it's deployed:
    ERC-6492-wrapped with its deployment."""

    async def sign(hash_: bytes) -> bytes:
        return smartwallet.sign_undeployed(chain_id, wallet, owner, hash_)

    return sign


def by_kernel(chain_id: int, account: ChecksumAddress, owner: LocalAccount) -> Signs:
    """`owner`'s signature of a hash as Kernel account `account` checks it (ERC-1271)."""

    async def sign(hash_: bytes) -> bytes:
        return kernel.sign(chain_id, account, owner, hash_)

    return sign


def by_undeployed_kernel(chain_id: int, account: ChecksumAddress, owner: LocalAccount) -> Signs:
    """`owner`'s signature of a hash as Kernel account `account`'s before it's deployed:
    ERC-6492-wrapped with its deployment, through ZeroDev's meta-factory."""

    async def sign(hash_: bytes) -> bytes:
        return kernel.sign_undeployed(chain_id, account, owner, hash_)

    return sign


def as_given(signature: bytes) -> Signs:
    """`signature`, whatever the hash: a payer's that takes any."""

    async def sign(_hash: bytes) -> bytes:
        return signature

    return sign


async def signed(
    sign: Signs,
    domain: Eip712Domain,
    chain_id: int,
    token: ChecksumAddress,
    payer: ChecksumAddress,
    to: ChecksumAddress,
    value: int,
    valid_before: int,
    nonce: bytes | None = None,
) -> TransferAuthorization:
    """`payer`'s authorization of `value` of `token` (its EIP-712 `domain`, on `chain_id`) to
    `to`, valid until `valid_before`, once (its `nonce`, random unless given): signed by
    `sign`."""
    nonce = nonce if nonce is not None else os.urandom(32)
    hash_ = digest(domain, chain_id, token, payer, to, value, 0, valid_before, nonce)
    return TransferAuthorization(payer, to, value, 0, valid_before, nonce, await sign(hash_))


async def usdc_transfer(
    chain_id: int,
    payer: ChecksumAddress,
    to: ChecksumAddress,
    value: int,
    sign: Signs,
    *,
    valid_before: int | None = None,
    nonce: bytes | None = None,
) -> TransferAuthorization:
    """`payer`'s authorization of `value` of the local USDC to `to`, signed by `sign`: valid
    for VALID_SECONDS from now, or until `valid_before`."""
    before = valid_before if valid_before is not None else int(time.time()) + VALID_SECONDS
    return await signed(sign, USDC_DOMAIN, chain_id, USDC.address, payer, to, value, before, nonce)


def transfer_json(transfer: TransferAuthorization) -> dict[str, str]:
    """`transfer` as a submission's payment carries it: its recipient and value are the
    payment's own."""
    return {
        "from": transfer.payer,
        "validAfter": hex(transfer.valid_after),
        "validBefore": hex(transfer.valid_before),
        "nonce": "0x" + transfer.nonce.hex(),
        "signature": "0x" + transfer.signature.hex(),
    }


def deployment_json(deployment: Deployment) -> dict[str, str]:
    """`deployment` as a request names it."""
    return {
        "account": deployment.account,
        "factory": deployment.factory,
        "factoryData": "0x" + deployment.factory_data.hex(),
    }


def payment_header(accepted: dict[str, object], transfer: TransferAuthorization) -> str:
    """A PAYMENT-SIGNATURE: `transfer`, for `accepted`, base64 of its JSON."""
    payload = {
        "x402Version": 2,
        "accepted": accepted,
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
    return base64.b64encode(json.dumps(payload).encode()).decode()


def decoded(header: str) -> dict[str, Any]:
    """An x402 header's JSON (PAYMENT-REQUIRED, PAYMENT-SIGNATURE, PAYMENT-RESPONSE): base64 of
    it."""
    result: dict[str, Any] = json.loads(base64.b64decode(header))
    return result


async def payment(
    chain_id: int,
    accepted: dict[str, Any],
    payer: ChecksumAddress,
    sign: Signs,
    value: int | None = None,
    *,
    to: ChecksumAddress | None = None,
    valid_before: int | None = None,
    nonce: bytes | None = None,
) -> str:
    """A PAYMENT-SIGNATURE: `payer`'s authorization of `accepted`'s amount (or `value`) to its
    payTo (or `to`), of the local USDC, signed by `sign`."""
    amount = int(accepted["amount"]) if value is None else value
    payee = to if to is not None else accepted["payTo"]
    transfer = await usdc_transfer(
        chain_id, payer, payee, amount, sign, valid_before=valid_before, nonce=nonce
    )
    return payment_header(accepted, transfer)


def balance_call(client: LocalAccount) -> dict[str, object]:
    """A call that succeeds for anyone: the client's USDC balance, read."""
    data = "0x" + (keccak(text="balanceOf(address)")[:4] + bytes(12)).hex() + client.address[2:]
    return {"call": {"to": USDC.address, "data": data}, "gas": hex(CALL_GAS)}
