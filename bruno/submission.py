"""A client's submission (SPEC.md, Submission): its id, and the checks on its authorizations
that need nothing from the chain."""

from collections.abc import Sequence
from dataclasses import dataclass

from eth_abi.abi import encode
from eth_account.datastructures import SignedSetCodeAuthorization
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from .common import BrunoError
from .evm import EncodedCall

# secp256k1's order, and EIP-7702's bounds on an authorization: s at most half the order, the
# nonce under 2**64 - 1
_SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_MAX_AUTHORIZATION_NONCE = 2**64 - 1
_SUBMISSION_ID_TYPES = [
    "address",
    "bytes",
    "(uint256,address,uint256,uint8,uint256,uint256)[]",
    "uint256",
    "address",
    "address",
    "uint256",
    "uint256",
]


class InvalidSubmissionError(BrunoError):
    """A field of the submission is the client's mistake."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field


@dataclass(frozen=True, slots=True)
class Submission:
    """A client's call, its authorizations, the gas the call is given, and its payment: at
    least `amount` of `token` to `payee`, by block `max_block_number`."""

    call: EncodedCall
    authorizations: tuple[SignedSetCodeAuthorization, ...]
    gas: int
    token: ChecksumAddress
    payee: ChecksumAddress
    amount: int
    max_block_number: int

    @property
    def id(self) -> bytes:
        """keccak256 of the submission ABI-encoded: the same submission, the same id."""
        return keccak(
            encode(
                _SUBMISSION_ID_TYPES,
                [
                    self.call.to,
                    bytes.fromhex(self.call.data.removeprefix("0x")),
                    [
                        (a.chain_id, to_checksum_address(a.address), a.nonce, a.y_parity, a.r, a.s)
                        for a in self.authorizations
                    ],
                    self.gas,
                    self.token,
                    self.payee,
                    self.amount,
                    self.max_block_number,
                ],
            )
        )


def check_authorizations(
    authorizations: Sequence[SignedSetCodeAuthorization], chain_id: int, most: int
) -> None:
    """At most `most`, each one EIP-7702 would take: its chain id `chain_id` or 0, y_parity 0
    or 1, r and s in range with s low, its nonce under 2**64 - 1."""
    if len(authorizations) > most:
        raise InvalidSubmissionError("authorizationList", f"over {most}")
    for auth in authorizations:
        if auth.chain_id not in (0, chain_id):
            raise InvalidSubmissionError("authorizationList", f"chain id {auth.chain_id}")
        if auth.y_parity not in (0, 1):
            raise InvalidSubmissionError("authorizationList", "yParity is 0 or 1")
        if not (0 < auth.r < _SECP256K1_N and 0 < auth.s <= _SECP256K1_N // 2):
            raise InvalidSubmissionError("authorizationList", "r or s out of range")
        if auth.nonce >= _MAX_AUTHORIZATION_NONCE:
            raise InvalidSubmissionError("authorizationList", "nonce out of range")
