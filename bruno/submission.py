"""A client's submission (SPEC.md, Submission): its id, and the checks on its authorizations
that need nothing from the chain."""

from collections.abc import Sequence
from dataclasses import dataclass

from eth_abi.abi import encode
from eth_account.datastructures import SignedSetCodeAuthorization
from eth_keys.exceptions import BadSignature, ValidationError
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from .common import BrunoError
from .eip3009 import TRANSFER_ID_TYPE, TransferAuthorization
from .erc6492 import DEPLOYMENT_ID_TYPE, Deployment, WrapperError
from .evm import SECP256K1_N, EncodedCall
from .refusals import Refusal, message

# the largest nonce an authorization may have: the largest integer a float64 holds exactly,
# read alike by both relays (EIP-7702's bound is under 2**64 - 1; no account's nonce gets near
# this one)
MAX_AUTHORIZATION_NONCE = 2**53 - 1
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


@dataclass(frozen=True, slots=True)
class Bound:
    """The limit a field is over: the `most` it may be, and what it `needs`, where that isn't
    what was given (a transaction's gas, the runner's included)."""

    most: int
    needs: int | None = None


class InvalidSubmissionError(BrunoError):
    """A field of the submission is the client's mistake: `field`, why (`reason`), and the
    limit it's over, if that's why (`bound`)."""

    def __init__(self, field: str, reason: str, bound: Bound | None = None) -> None:
        super().__init__(f"{field}: {reason}")
        self.field = field
        self.reason = reason
        self.bound = bound


@dataclass(frozen=True, slots=True)
class Submission:
    """A client's call, its authorizations, the gas the call is given, and its payment: at
    least `amount` of `token` to `payee`, by block `max_block_number`; paid by a signed
    `transfer` of the amount (EIP-3009), if it comes with one, which the runner makes before the
    call; and an account to deploy first, if it names one. Its transfer's ERC-6492 wrapper, if
    any, is checked as it's made, and at most one account is deployed: InvalidSubmissionError
    otherwise."""

    call: EncodedCall
    authorizations: tuple[SignedSetCodeAuthorization, ...]
    gas: int
    token: ChecksumAddress
    payee: ChecksumAddress
    amount: int
    max_block_number: int
    transfer: TransferAuthorization | None = None
    deployment: Deployment | None = None

    def __post_init__(self) -> None:
        _ = self.to_deploy

    @property
    def to_deploy(self) -> Deployment | None:
        """The account the runner deploys, if any: the submission's own deployment, or its
        transfer's signature's ERC-6492 wrapper's, which must be the same if both are given."""
        if self.transfer is None:
            return self.deployment
        try:
            wrapped = self.transfer.deployment
        except WrapperError as e:
            raise InvalidSubmissionError("payment", str(e)) from e
        if wrapped is None:
            return self.deployment
        if self.deployment is not None and self.deployment != wrapped:
            raise InvalidSubmissionError("deployment", message(Refusal.DEPLOYMENT_NOT_SIGNATURES))
        return wrapped

    @property
    def id(self) -> bytes:
        """keccak256 of the submission ABI-encoded: the same submission, the same id. One paid
        by a transfer has the transfer in place of its deadline, which over x402 follows from
        the transfer's validBefore as of the block it's checked at: the same payment sent again
        is the same submission. Its own deployment, if it names one, is appended."""
        types = list(_SUBMISSION_ID_TYPES)
        values: list[object] = [
            self.call.to,
            self.call.data,
            [
                (a.chain_id, to_checksum_address(a.address), a.nonce, a.y_parity, a.r, a.s)
                for a in self.authorizations
            ],
            self.gas,
            self.token,
            self.payee,
            self.amount,
            self.max_block_number,
        ]
        if self.transfer is not None:
            types[-1] = TRANSFER_ID_TYPE
            values[-1] = self.transfer.encoded()
        if self.deployment is not None:
            types.append(DEPLOYMENT_ID_TYPE)
            values.append(self.deployment.encoded())
        return keccak(encode(types, values))


def authorities(
    authorizations: Sequence[SignedSetCodeAuthorization],
) -> tuple[ChecksumAddress, ...]:
    """Each authorization's signer, recovered: InvalidSubmissionError if one can't be."""
    try:
        return tuple(to_checksum_address(a.authority) for a in authorizations)
    except (BadSignature, ValidationError) as e:
        raise InvalidSubmissionError(
            "authorizationList", message(Refusal.AUTHORIZATION_UNRECOVERABLE, why=str(e))
        ) from e


def check_signed(y_parity: int, r: int, s: int, nonce: int) -> None:
    """An authorization's yParity 0 or 1, its r and s in range with s low, and its nonce at
    most MAX_AUTHORIZATION_NONCE: InvalidSubmissionError otherwise."""
    if y_parity not in (0, 1):
        raise InvalidSubmissionError("authorizationList", message(Refusal.AUTHORIZATION_Y_PARITY))
    if not (0 < r < SECP256K1_N and 0 < s <= SECP256K1_N // 2):
        raise InvalidSubmissionError("authorizationList", message(Refusal.AUTHORIZATION_R_S))
    if nonce > MAX_AUTHORIZATION_NONCE:
        raise InvalidSubmissionError("authorizationList", message(Refusal.AUTHORIZATION_NONCE))


def check_authorizations(
    authorizations: Sequence[SignedSetCodeAuthorization], chain_id: int, most: int
) -> None:
    """At most `most`, each one EIP-7702 would take: its chain id `chain_id` or 0, and its
    signature and nonce as check_signed takes them."""
    if len(authorizations) > most:
        raise InvalidSubmissionError(
            "authorizationList", message(Refusal.AUTHORIZATIONS_OVER, most=most), Bound(most)
        )
    for auth in authorizations:
        if auth.chain_id not in (0, chain_id):
            raise InvalidSubmissionError(
                "authorizationList",
                message(Refusal.AUTHORIZATION_CHAIN, chain_id=auth.chain_id),
            )
        check_signed(auth.y_parity, auth.r, auth.s, auth.nonce)
