"""EIP-3009's transferWithAuthorization: a payment a client signs off-chain, which the runner
makes before the client's call (x402's `exact` scheme on EVM). The calldata, the measurement
of its gas, and the block a signed authorization lasts until."""

import os
from dataclasses import dataclass

from eth_abi.abi import encode
from eth_account import Account
from eth_keys import keys
from eth_keys.exceptions import BadSignature, ValidationError
from eth_typing import ChecksumAddress, Hash32
from eth_utils.crypto import keccak

from .chain import Latest
from .config import Eip712Domain
from .forks import GasSchedule
from .net.jsonrpc import Call

# the (v, r, s) overload, which every EIP-3009 token has
_TRANSFER_WITH_AUTHORIZATION = keccak(
    text="transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,uint8,bytes32,bytes32)"
)[:4]
_TRANSFER_TYPEHASH = keccak(
    text="TransferWithAuthorization(address from,address to,uint256 value,uint256 validAfter,"
    "uint256 validBefore,bytes32 nonce)"
)
_DOMAIN_TYPEHASH = keccak(
    text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)
_NEVER = 2**256 - 1


@dataclass(frozen=True, slots=True)
class TransferAuthorization:
    """`payer`'s signed authorization for `value` of its token to go to `to`, by anyone, once
    (its `nonce`), after `valid_after` and before `valid_before` (Unix seconds)."""

    payer: ChecksumAddress
    to: ChecksumAddress
    value: int
    valid_after: int
    valid_before: int
    nonce: bytes
    v: int
    r: int
    s: int

    def calldata(self) -> bytes:
        """transferWithAuthorization(from, to, value, validAfter, validBefore, nonce, v, r, s)."""
        return _TRANSFER_WITH_AUTHORIZATION + encode(
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
                self.payer,
                self.to,
                self.value,
                self.valid_after,
                self.valid_before,
                self.nonce,
                self.v,
                self.r.to_bytes(32),
                self.s.to_bytes(32),
            ],
        )

    def encoded(
        self,
    ) -> tuple[ChecksumAddress, ChecksumAddress, int, int, int, bytes, int, int, int]:
        """Its fields in order, as a submission's id encodes them."""
        return (
            self.payer,
            self.to,
            self.value,
            self.valid_after,
            self.valid_before,
            self.nonce,
            self.v,
            self.r,
            self.s,
        )


# a submission's id encodes an authorization as this tuple
ID_TYPE = "(address,address,uint256,uint256,uint256,bytes32,uint8,uint256,uint256)"
# the calldata's length: a selector and nine words
CALLDATA_BYTES = 4 + 9 * 32
# secp256k1's group order: a signature's s is at most half of it (EIP-2)
SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def digest(
    domain: Eip712Domain,
    chain_id: int,
    token: ChecksumAddress,
    payer: ChecksumAddress,
    to: ChecksumAddress,
    value: int,
    valid_after: int,
    valid_before: int,
    nonce: bytes,
) -> bytes:
    """What an authorization's signer signs (EIP-712): the token's domain, and the transfer."""
    separator = keccak(
        encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
            [
                _DOMAIN_TYPEHASH,
                keccak(text=domain.name),
                keccak(text=domain.version),
                chain_id,
                token,
            ],
        )
    )
    struct = keccak(
        encode(
            ["bytes32", "address", "address", "uint256", "uint256", "uint256", "bytes32"],
            [_TRANSFER_TYPEHASH, payer, to, value, valid_after, valid_before, nonce],
        )
    )
    return keccak(b"\x19\x01" + separator + struct)


def signer_of(
    domain: Eip712Domain, chain_id: int, token: ChecksumAddress, transfer: TransferAuthorization
) -> ChecksumAddress | None:
    """The account whose key signed `transfer` (ecrecover): None if its signature recovers to
    none. A token that also takes ERC-1271 signatures (FiatToken v2.2) accepts a contract payer
    whose signature recovers to someone else, so a mismatch alone doesn't make it invalid."""
    hash_ = digest(
        domain,
        chain_id,
        token,
        transfer.payer,
        transfer.to,
        transfer.value,
        transfer.valid_after,
        transfer.valid_before,
        transfer.nonce,
    )
    if transfer.s > SECP256K1_N // 2:  # refused by the token's ECDSA (EIP-2), so by any token
        return None
    try:
        signature = keys.Signature(vrs=(transfer.v - 27, transfer.r, transfer.s))
        return signature.recover_public_key_from_msg_hash(hash_).to_checksum_address()
    except BadSignature, ValidationError:
        return None


def measuring(
    domain: Eip712Domain, chain_id: int, token: ChecksumAddress, payee: ChecksumAddress
) -> tuple[Call, bytes]:
    """eth_estimateGas of a zero-value transfer to `payee` authorized by an account made for it,
    with a random nonce (a fixed one, being public, could be spent to break this), never sent;
    and its calldata. Signed for the wrong domain, it fails: the token isn't what the
    configuration says."""
    signer = Account.create()
    nonce = os.urandom(32)
    signed = signer.unsafe_sign_hash(
        Hash32(digest(domain, chain_id, token, signer.address, payee, 0, 0, _NEVER, nonce))
    )
    authorization = TransferAuthorization(
        signer.address, payee, 0, 0, _NEVER, nonce, signed.v, signed.r, signed.s
    )
    data = authorization.calldata()
    return Call("eth_estimateGas", [{"to": token, "data": "0x" + data.hex()}]), data


def measured(estimate: int, calldata: bytes, schedule: GasSchedule) -> int:
    """A measuring estimate's execution: less its intrinsic gas. The zero-value transfer reads
    and writes back both balances, warm; its nonce's slot is new, as every authorization's is."""
    standard, _ = schedule.intrinsic(calldata)
    return estimate - standard


def transfer_gas(measured_gas: int, schedule: GasSchedule, payee_holds: bool) -> int:
    """The most a transfer can take, from the zero-value one's `measured_gas`: the payer's
    balance changed, and the payee's changed too if it `payee_holds` some already, else set
    from zero."""
    payee = schedule.storage_write if payee_holds else schedule.storage_set
    return measured_gas + schedule.storage_write + payee


def last_block_before(latest: Latest, valid_before: int, slot_seconds: int) -> int:
    """The last block expected before `valid_before`, a slot a block after `latest`: at or
    under `latest.number` if there's none."""
    return latest.number + (valid_before - 1 - latest.timestamp) // slot_seconds
