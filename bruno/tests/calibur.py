"""Calibur, one EIP-7702 delegate a client may provision its account with: its signed batch
(execute(SignedBatchedCall, wrappedSignature)), built and signed as a client would."""

from typing import Any

from eth_abi.abi import encode
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_account.signers.local import LocalAccount
from eth_utils.abi import function_signature_to_4byte_selector
from eth_utils.crypto import keccak

from bruno.evm import EncodedCall
from bruno.tests.deployments import raw_call
from bruno.tests.localchain import CALIBUR

# Calibur's EIP-712 domain (src/EIP712.sol: _domainNameAndVersion), its salt the
# implementation's address with prefix 0 (PrefixedSaltLib.pack), and its root key's hash
# (KeyLib.ROOT_KEY_HASH): the account's own key
CALIBUR_NAME = "Calibur"
CALIBUR_VERSION = "1.0.0"
ROOT_KEY_HASH = bytes(32)
# execute(SignedBatchedCall, bytes wrappedSignature), SignedBatchedCall being
# (BatchedCall(Call[] calls, bool revertOnFailure), nonce, keyHash, executor, deadline)
_SIGNED_BATCHED_CALL = "(((address,uint256,bytes)[],bool),uint256,bytes32,address,uint256)"
_EXECUTE = function_signature_to_4byte_selector(f"execute({_SIGNED_BATCHED_CALL},bytes)")
# EIP-712 types, as Calibur's libraries declare them (SignedBatchedCallLib, BatchedCallLib,
# CallLib)
_TYPES: dict[str, list[dict[str, str]]] = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
        {"name": "salt", "type": "bytes32"},
    ],
    "SignedBatchedCall": [
        {"name": "batchedCall", "type": "BatchedCall"},
        {"name": "nonce", "type": "uint256"},
        {"name": "keyHash", "type": "bytes32"},
        {"name": "executor", "type": "address"},
        {"name": "deadline", "type": "uint256"},
    ],
    "BatchedCall": [
        {"name": "calls", "type": "Call[]"},
        {"name": "revertOnFailure", "type": "bool"},
    ],
    "Call": [
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "data", "type": "bytes"},
    ],
}


def fresh_client(name: str = "a fresh client") -> LocalAccount:
    return Account.from_key(keccak(text=name))


def calibur_batch(
    client: LocalAccount, chain_id: int, calls: list[EncodedCall], nonce: int = 0
) -> EncodedCall:
    """The client's one call: execute on its own account, with its signed batch of `calls`
    (reverting on any failure, by its root key, executor 0, no deadline)."""
    executor = "0x" + "00" * 20
    message: dict[str, Any] = {
        "batchedCall": {
            "calls": [
                {"to": c.to, "value": c.value, "data": bytes.fromhex(c.data[2:])} for c in calls
            ],
            "revertOnFailure": True,
        },
        "nonce": nonce,
        "keyHash": ROOT_KEY_HASH,
        "executor": executor,
        "deadline": 0,
    }
    domain = {
        "name": CALIBUR_NAME,
        "version": CALIBUR_VERSION,
        "chainId": chain_id,
        "verifyingContract": client.address,
        "salt": bytes(12) + bytes.fromhex(CALIBUR[2:]),
    }
    signable = encode_typed_data(
        full_message={
            "types": _TYPES,
            "primaryType": "SignedBatchedCall",
            "domain": domain,
            "message": message,
        }
    )
    signature = bytes(client.sign_message(signable).signature)
    wrapped = encode(["bytes", "bytes"], [signature, b""])
    batch = (
        ([(c.to, c.value, bytes.fromhex(c.data[2:])) for c in calls], True),
        nonce,
        ROOT_KEY_HASH,
        executor,
        0,
    )
    return raw_call(
        client.address, _EXECUTE + encode([_SIGNED_BATCHED_CALL, "bytes"], [batch, wrapped])
    )
