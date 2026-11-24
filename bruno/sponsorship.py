"""A sponsorship: transactions an account signed and can't pay the gas for, which the relay
sends between a transaction of its own funding the account and one calling the forwarder for the
payment (docs/development/plans/sponsorship.md). What's taken and stored of one, beside a
Submission, and its id."""

from dataclasses import dataclass

from eth_abi.abi import encode
from eth_account.datastructures import SignedSetCodeAuthorization
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from .evm import EncodedCall
from .refusals import Refusal, refused
from .submission import InvalidSubmissionError, authorities
from .transactions import Decoded

_SPONSORSHIP_ID_TYPES = [
    "address",
    "(uint256,address,uint256,uint8,uint256,uint256)[]",
    "bytes",
    "uint256",
    "bytes[]",
    "address",
    "address",
    "uint256",
    "uint256",
]


@dataclass(frozen=True, slots=True)
class Sponsorship:
    """An `account`'s sponsorship: the `authorizations` it signed, which the relay's first
    transaction carries; the `call` that transaction makes into the account's code, if any,
    given `gas`; the account's own `transactions`, as signed, in order; and its payment, at
    least `amount` of `token` to the forwarder at `payee`, by block `max_block_number`. Every
    authorization and transaction is the account's, the call is to it, the transactions'
    nonces are consecutive, and there's a call or a transaction: InvalidSubmissionError
    otherwise."""

    account: ChecksumAddress
    authorizations: tuple[SignedSetCodeAuthorization, ...]
    call: EncodedCall | None
    gas: int
    transactions: tuple[Decoded, ...]
    token: ChecksumAddress
    payee: ChecksumAddress
    amount: int
    max_block_number: int

    def __post_init__(self) -> None:
        if self.call is None and not self.transactions:
            raise InvalidSubmissionError("sponsored", refused(Refusal.SPONSORED_NOTHING))
        if self.call is not None:
            self._its("sponsored.call", self.call.to)
        for index, authority in enumerate(authorities(self.authorizations)):
            self._its(f"sponsored.authorizationList[{index}]", authority)
        following: int | None = None
        for index, transaction in enumerate(self.transactions):
            field = f"sponsored.transactions[{index}]"
            self._its(field, transaction.signed.sender)
            nonce = transaction.signed.nonce
            if following is not None and nonce != following:
                raise InvalidSubmissionError(
                    field, refused(Refusal.SPONSORED_NONCE, nonce=nonce, following=following)
                )
            following = nonce + 1

    def _its(self, field: str, account: ChecksumAddress) -> None:
        """`field` is the sponsored account's: InvalidSubmissionError if it's `account`'s, some
        other."""
        if account != self.account:
            raise InvalidSubmissionError(
                field, refused(Refusal.SPONSORED_NOT_ACCOUNTS, given=account, account=self.account)
            )

    @property
    def funding(self) -> int:
        """The ETH the account's transactions need, which the relay's first transaction sends
        it: each one's gas at its max fee, and its value. What a transaction sends the
        forwarder isn't funded: the payment would be in its own price, which no amount covers.
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
            b"" if self.call is None else self.call.data,
            self.gas,
            [t.signed.raw for t in self.transactions],
            self.token,
            self.payee,
            self.amount,
            self.max_block_number,
        ]
        return keccak(encode(_SPONSORSHIP_ID_TYPES, values))
