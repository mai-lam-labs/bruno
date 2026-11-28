"""A sponsorship: transactions an account signed and can't pay the gas for, which the relay
sends after its own calls and the ETH the account needs, and before a transaction calling the
forwarder for the payment (docs/development/plans/sponsorship.md). What's taken and stored of
one, beside a Submission, and its id. It's planned as a submission is (plan.py): its calls
classified alike, its payment known by the forwarder's check."""

from collections.abc import Sequence
from dataclasses import dataclass

from eth_abi.abi import encode
from eth_account.datastructures import SignedSetCodeAuthorization
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from .eip3009 import TransferAuthorization
from .erc6492 import Deployment
from .refusals import Refusal, refused
from .submission import CALL_ID_TYPE, InvalidSubmissionError, SubmittedCall, authorities
from .transactions import Decoded

_SPONSORSHIP_ID_TYPES = [
    "address",
    "(uint256,address,uint256,uint8,uint256,uint256)[]",
    f"{CALL_ID_TYPE}[]",
    "bytes[]",
    "address",
    "address",
    "uint256",
    "uint256",
]


def owned(
    account: ChecksumAddress,
    calls: int,
    authorizations: Sequence[SignedSetCodeAuthorization],
    senders: Sequence[ChecksumAddress | None],
) -> None:
    """What's sponsored is `account`'s: there's a call (`calls` of them) or a transaction, and
    every authorization and every signed transaction (`senders`, each one's, None for one not
    signed yet) is its own. Its calls are the relay's to make, to anything, classified as a
    submission's are. InvalidSubmissionError otherwise, naming the field."""
    if not calls and not senders:
        raise InvalidSubmissionError("transactions", refused(Refusal.SPONSORED_NOTHING))
    given: list[tuple[str, ChecksumAddress | None]] = [
        (f"authorizationList[{index}]", authority)
        for index, authority in enumerate(authorities(authorizations))
    ]
    given.extend((f"transactions[{index}]", sender) for index, sender in enumerate(senders))
    for field, its in given:
        if its is not None and its != account:
            raise InvalidSubmissionError(
                field, refused(Refusal.SPONSORED_NOT_ACCOUNTS, given=its, account=account)
            )


@dataclass(frozen=True, slots=True)
class Sponsorship:
    """An `account`'s sponsorship: the `authorizations` it signed, which the relay's first
    transaction carries; the `calls` the relay makes first, each with the gas it's given, as
    a submission's; the account's own `transactions`, as signed, in order; and its payment, at
    least `amount` of `token` to the forwarder at `payee`, by block `max_block_number`. Every
    authorization and transaction is the account's, the transactions' nonces are consecutive,
    and there's a call or a transaction: InvalidSubmissionError otherwise."""

    account: ChecksumAddress
    authorizations: tuple[SignedSetCodeAuthorization, ...]
    calls: tuple[SubmittedCall, ...]
    transactions: tuple[Decoded, ...]
    token: ChecksumAddress
    payee: ChecksumAddress
    amount: int
    max_block_number: int

    def __post_init__(self) -> None:
        owned(
            self.account,
            len(self.calls),
            self.authorizations,
            [t.signed.sender for t in self.transactions],
        )
        following: int | None = None
        for index, transaction in enumerate(self.transactions):
            nonce = transaction.signed.nonce
            if following is not None and nonce != following:
                raise InvalidSubmissionError(
                    f"transactions[{index}]",
                    refused(Refusal.SPONSORED_NONCE, nonce=nonce, following=following),
                )
            following = nonce + 1

    @property
    def transfer(self) -> TransferAuthorization | None:
        """None: a sponsorship's payment is the account's own transaction, or its code's."""
        return None

    @property
    def to_deploy(self) -> Deployment | None:
        """None: a sponsorship deploys nothing."""
        return None

    @property
    def funding(self) -> int:
        """The ETH the account's transactions need, which the relay sends it before them: each
        one's gas at its max fee, and its value. What a transaction sends the forwarder isn't
        funded: the payment would be in its own price, which no amount covers.
        The account may pay out of anything else it's sent: the check asks for more than was
        sent, whatever it's paid from."""
        return sum(
            t.signed.gas * t.signed.max_fee_per_gas + (0 if t.to == self.payee else t.signed.value)
            for t in self.transactions
        )

    @property
    def id(self) -> bytes:
        """keccak256 of the sponsorship ABI-encoded: the same sponsorship, the same id."""
        values: list[object] = [
            self.account,
            [
                (a.chain_id, to_checksum_address(a.address), a.nonce, a.y_parity, a.r, a.s)
                for a in self.authorizations
            ],
            [call.encoded() for call in self.calls],
            [t.signed.raw for t in self.transactions],
            self.token,
            self.payee,
            self.amount,
            self.max_block_number,
        ]
        return keccak(encode(_SPONSORSHIP_ID_TYPES, values))
