"""The interface's requests and responses, the same for REST and JSON-RPC: Ethereum
JSON-RPC's encoding (hex quantities and data, checksummed addresses), camelCase, as msgspec
Structs, which Litestar decodes, validates and documents. Each converts to or from the
relay's own types."""

from collections.abc import Sequence
from enum import IntEnum
from typing import Annotated, Literal

import msgspec
from eth_account.datastructures import SignedSetCodeAuthorization
from eth_account.typed_transactions.set_code_transaction import Authorization
from eth_keys.datatypes import Signature
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address

from ..chain import Finality
from ..common import Invalid, whole
from ..eip3009 import TransferAuthorization, UnsignedTransfer
from ..erc6492 import Deployment
from ..evm import EncodedCall
from ..pricing import Breakdown, Part, Payer, Quoted, QuotedCall, Quotes
from ..refusals import Category, Refusal, refused
from ..relay import Accepted, Terms
from ..sponsoring import Asked, SponsoredQuotes, ToSign, Unsigned
from ..sponsorship import Sponsorship
from ..submission import (
    Bound,
    InvalidSubmissionError,
    Submission,
    SubmittedCall,
    check_signed,
)
from ..tracked import Finished, Rejection, Tracked
from ..transactions import Decoded, TransactionError, decoded
from ..userops import (
    EIP7702_MARKER,
    GasEstimate,
    InvalidUserOperationError,
    Paymaster,
    UserOperation,
    UserOperationReceipt,
    entry_point_at,
)

ADDRESS_PATTERN = whole("0x[0-9a-fA-F]{40}")
QUANTITY_PATTERN = whole("0x(0|[1-9a-f][0-9a-f]*)")
DATA_PATTERN = whole("0x([0-9a-fA-F]{2})*")
HASH_PATTERN = whole("0x[0-9a-f]{64}")

type Address = Annotated[str, msgspec.Meta(pattern=ADDRESS_PATTERN, description="An address")]
# a uint256's: "0x" and at most 64 digits
QUANTITY_MAX_LENGTH = 66

type Quantity = Annotated[
    str,
    msgspec.Meta(
        pattern=QUANTITY_PATTERN, max_length=QUANTITY_MAX_LENGTH, description="A quantity, as hex"
    ),
]
type Data = Annotated[str, msgspec.Meta(pattern=DATA_PATTERN, description="Bytes, as hex")]
# an op's factory: an address, or EntryPoint v0.8's EIP-7702 marker (ERC-7769)
EIP7702_FACTORY = "0x7702"
type Factory = Annotated[
    str,
    msgspec.Meta(
        pattern=whole(f"{EIP7702_FACTORY}|0x[0-9a-fA-F]{{40}}"),
        description="An address, or 0x7702: an EIP-7702 account (EntryPoint v0.8)",
    ),
]
# a number of at most a word, as hex, leading zeros allowed: a signature's r, s and yParity as
# clients write them, padded (viem's eip7702Auth) or not
type Word = Annotated[
    str, msgspec.Meta(pattern=whole("0x[0-9a-fA-F]{1,64}"), description="A word, as hex")
]
type Hash = Annotated[str, msgspec.Meta(pattern=HASH_PATTERN, description="32 bytes, as hex")]
type Bytes32 = Annotated[
    str, msgspec.Meta(pattern=whole("0x[0-9a-fA-F]{64}"), description="32 bytes, as hex")
]
# the longest signature a payment carries, in bytes: a key's is 65, a contract's (a passkey's
# WebAuthn assertion, say) longer, an ERC-6492 wrapper longer still, never near this
MAX_SIGNATURE_BYTES = 4096
type SignatureHex = Annotated[
    str,
    msgspec.Meta(
        pattern=whole("0x(?:[0-9a-fA-F]{2})+"),
        max_length=2 + 2 * MAX_SIGNATURE_BYTES,
        description="A signature, as hex: a key's 65 bytes, a contract's, or ERC-6492-wrapped",
    ),
]


class Wire(msgspec.Struct, frozen=True, rename="camel", omit_defaults=True):
    pass


class Strict(Wire, frozen=True, forbid_unknown_fields=True):
    """A request of the relay's own, and what's its own within one: every field is read, and
    one it doesn't name is refused, not ignored (a call's `value` would be ETH the client
    thinks is sent). What another standard shapes isn't: a user operation, an EIP-7702
    authorization, an x402 payment."""


def closed(schemas: dict[str, dict[str, object]]) -> None:
    """Mark each Strict type's schema among `schemas`, by its name, as taking no other field:
    Litestar's OpenAPI schemas don't say it."""
    pending: list[type[Strict]] = [Strict]
    while pending:
        kind = pending.pop()
        pending.extend(kind.__subclasses__())
        if kind.__name__ in schemas:
            schemas[kind.__name__]["additionalProperties"] = False


def hex_data(value: bytes) -> str:
    """`value` as JSON-RPC data: 0x-prefixed hex."""
    return "0x" + value.hex()


class TermsResult(Wire, frozen=True):
    payee: Address
    forwarder: Address
    tokens: tuple[Address, ...]
    max_gas: Quantity
    max_call_data_length: Quantity

    @classmethod
    def of(cls, terms: Terms) -> TermsResult:
        return cls(
            payee=terms.payee,
            forwarder=terms.forwarder,
            tokens=terms.tokens,
            max_gas=hex(terms.max_gas),
            max_call_data_length=hex(terms.max_call_bytes),
        )


class DeploymentJson(Strict, frozen=True):
    """An account to deploy before the call, if it has no code: `factory` called with
    `factoryData` (ERC-4337's names)."""

    account: Address
    factory: Address
    factory_data: Data

    def deployment(self) -> Deployment:
        return Deployment(
            to_checksum_address(self.account),
            to_checksum_address(self.factory),
            bytes.fromhex(self.factory_data[2:]),
        )


def payer_of(payer: str | None, deployment: DeploymentJson | None, verify_gas: str | None) -> Payer:
    """What a quote (or an x402 request) is told of its payment's signer."""
    return Payer(
        address=None if payer is None else to_checksum_address(payer),
        deployment=None if deployment is None else deployment.deployment(),
        verify_gas=None if verify_gas is None else int(verify_gas, 16),
    )


class QuotedCallJson(Strict, frozen=True):
    """One of the calls to quote: whole (`to` and `data`), or only its data's length
    (`callDataLength`), both if they agree; the ETH it sends; and its gas, estimated if not
    given (only for a call given whole, and for one of a quote's calls at most)."""

    to: Address | None = None
    data: Data | None = None
    value: Quantity = "0x0"
    call_data_length: Quantity | None = None
    gas: Quantity | None = None

    def quoted(self, field: str) -> QuotedCall:
        """The call to quote, `field` its place in the request: InvalidSubmissionError
        without the call or its length, with only one of `to` and `data`, or with only its
        length and no gas."""
        if (self.to is None) != (self.data is None):
            raise InvalidSubmissionError(field, refused(Refusal.QUOTE_NO_CALL))
        call = None
        if self.to is not None and self.data is not None:
            call = EncodedCall(
                to_checksum_address(self.to), bytes.fromhex(self.data[2:]), int(self.value, 16)
            )
        if call is None and self.call_data_length is None:
            raise InvalidSubmissionError(field, refused(Refusal.QUOTE_NO_CALL))
        if call is None and self.gas is None:
            raise InvalidSubmissionError(f"{field}.gas", refused(Refusal.QUOTE_NO_GAS))
        length = len(call.data) if call is not None else 0
        return QuotedCall(
            call,
            length if self.call_data_length is None else int(self.call_data_length, 16),
            None if self.gas is None else int(self.gas, 16),
        )


# the most calls a request may carry: each is a read or two to plan, and where they're sent
# directly, a transaction of its bundle
MAX_CALLS = 8
# a request's calls, made in order: one at least
type Calls[T] = Annotated[tuple[T, ...], msgspec.Meta(min_length=1, max_length=MAX_CALLS)]
# a sponsorship's calls, which the relay makes before the account's own transactions: none, if
# it has transactions
type SponsoredCalls = Annotated[tuple[SubmittedCallJson, ...], msgspec.Meta(max_length=MAX_CALLS)]


class QuoteParams(Strict, frozen=True):
    """The tokens to quote in, and the calls; their EIP-7702 authorizations, signed
    (`authorizationList`), or the account that will sign one (`authority`); `payer`, the
    account a signed transfer pays from, if one pays (without it, the calls pay for
    themselves), and the gas its signature check takes, if known, only with it; and an account
    to deploy first, if any. A submission's fields, less its payment and deadline."""

    tokens: tuple[Address, ...]
    calls: Calls[QuotedCallJson]
    authorization_list: tuple[AuthorizationJson, ...] = ()
    authority: Address | None = None
    payer: Address | None = None
    deployment: DeploymentJson | None = None
    verify_gas: Quantity | None = None

    def payer_of(self) -> Payer:
        return payer_of(self.payer, self.deployment, self.verify_gas)

    def quoted(self) -> Quoted:
        """What to quote: InvalidSubmissionError with a call that isn't one (QuotedCallJson)."""
        return Quoted(
            tuple(call.quoted(f"calls[{index}]") for index, call in enumerate(self.calls)),
            tuple(a.signed() for a in self.authorization_list),
            None if self.authority is None else to_checksum_address(self.authority),
        )


class PartJson(Wire, frozen=True):
    gas: Quantity
    amount: Quantity

    @classmethod
    def of(cls, part: Part) -> PartJson:
        return cls(hex(part.gas), hex(part.amount))


class BreakdownJson(Wire, frozen=True):
    """A quote in parts, for people to read: its account's setup, the calls together, the ETH
    they send, the payment's transfer and check, and the relay's own, the rest; they sum to
    the amount."""

    setup: PartJson
    call: PartJson
    value: PartJson
    payment: PartJson
    relay: PartJson

    @classmethod
    def of(cls, breakdown: Breakdown) -> BreakdownJson:
        return cls(
            PartJson.of(breakdown.setup),
            PartJson.of(breakdown.call),
            PartJson.of(breakdown.value),
            PartJson.of(breakdown.payment),
            PartJson.of(breakdown.relay),
        )


class TypedField(Wire, frozen=True):
    name: str
    type: str


class TypedDomain(Wire, frozen=True):
    name: str
    version: str
    chain_id: int
    verifying_contract: Address


class TransferMessage(Wire, frozen=True):
    """A transferWithAuthorization's fields, its numbers as decimal strings, as EIP-712's JSON
    writes them."""

    from_: Address = msgspec.field(name="from")
    to: Address
    value: str
    valid_after: str
    valid_before: str
    nonce: Bytes32


# EIP-3009's transferWithAuthorization, and its token's domain, as EIP-712 types them
_TRANSFER_TYPES: dict[str, tuple[TypedField, ...]] = {
    "EIP712Domain": (
        TypedField("name", "string"),
        TypedField("version", "string"),
        TypedField("chainId", "uint256"),
        TypedField("verifyingContract", "address"),
    ),
    "TransferWithAuthorization": (
        TypedField("from", "address"),
        TypedField("to", "address"),
        TypedField("value", "uint256"),
        TypedField("validAfter", "uint256"),
        TypedField("validBefore", "uint256"),
        TypedField("nonce", "bytes32"),
    ),
}


class TransferTypedData(Wire, frozen=True):
    """A transfer for its payer to sign, as EIP-712's JSON (eth_signTypedData_v4's) writes it:
    signed as it is, it's the payment."""

    types: dict[str, tuple[TypedField, ...]]
    primary_type: str
    domain: TypedDomain
    message: TransferMessage

    @classmethod
    def of(cls, transfer: UnsignedTransfer) -> TransferTypedData:
        return cls(
            types=_TRANSFER_TYPES,
            primary_type="TransferWithAuthorization",
            domain=TypedDomain(
                transfer.domain.name, transfer.domain.version, transfer.chain_id, transfer.token
            ),
            message=TransferMessage(
                from_=transfer.payer,
                to=transfer.to,
                value=str(transfer.value),
                valid_after=str(transfer.valid_after),
                valid_before=str(transfer.valid_before),
                nonce=hex_data(transfer.nonce),
            ),
        )


class TokenQuote(Wire, frozen=True):
    """A quote in `token`, broken down, and with the transfer its payer signs, if one pays
    (`typedData`)."""

    token: Address
    amount: Quantity
    breakdown: BreakdownJson | None = None
    typed_data: TransferTypedData | None = None


class QuotedCallGas(Wire, frozen=True):
    """What a quote answers of one of its calls: the gas it's priced for, to give it in the
    submission."""

    gas: Quantity


class QuoteResult(Wire, frozen=True):
    """A quote in each token, valid until `validUntilBlock`, and what it answers of each of
    its calls, in order."""

    valid_until_block: Quantity
    quotes: tuple[TokenQuote, ...]
    calls: tuple[QuotedCallGas, ...] | None = None

    @classmethod
    def of(cls, quotes: Quotes) -> QuoteResult:
        return cls(
            valid_until_block=hex(quotes.valid_until_block),
            quotes=tuple(
                TokenQuote(
                    q.token,
                    hex(q.amount),
                    None if q.breakdown is None else BreakdownJson.of(q.breakdown),
                    None if q.transfer is None else TransferTypedData.of(q.transfer),
                )
                for q in quotes.quotes
            ),
            calls=None if quotes.gas is None else tuple(QuotedCallGas(hex(g)) for g in quotes.gas),
        )


class UserOperationJson(Wire, frozen=True):
    """ERC-7769's UserOperation: its account, nonce, factory and data while it's undeployed (an
    EIP-7702 account's factory 0x7702), call data, gas limits (not read to estimate it), fees
    (zero if its calls pay the relay), signature (the account's stub, to estimate it), and the
    account's EIP-7702 authorization if it brings one; and its paymaster's, if it has one: its
    address, its data, and its limits (left to the estimate if the client doesn't give them)."""

    sender: Address
    nonce: Quantity
    call_data: Data
    signature: Data
    max_fee_per_gas: Quantity = "0x0"
    max_priority_fee_per_gas: Quantity = "0x0"
    factory: Factory | None = None
    factory_data: Data | None = None
    call_gas_limit: Quantity | None = None
    verification_gas_limit: Quantity | None = None
    pre_verification_gas: Quantity | None = None
    paymaster: Address | None = None
    paymaster_verification_gas_limit: Quantity | None = None
    paymaster_post_op_gas_limit: Quantity | None = None
    paymaster_data: Data | None = None
    eip7702_auth: AuthorizationJson | None = None

    def user_operation(self, entry_point: Address) -> UserOperation:
        """The op, for the EntryPoint at `entry_point`: InvalidUserOperationError if none is
        taken there."""
        paymaster = None
        if self.paymaster is not None:
            paymaster = Paymaster(
                address=to_checksum_address(self.paymaster),
                verification_gas_limit=_none_or_int(self.paymaster_verification_gas_limit),
                post_op_gas_limit=_none_or_int(self.paymaster_post_op_gas_limit),
                data=b"" if self.paymaster_data is None else bytes.fromhex(self.paymaster_data[2:]),
            )
        else:
            given = (
                self.paymaster_verification_gas_limit,
                self.paymaster_post_op_gas_limit,
                self.paymaster_data,
            )
            if any(field is not None for field in given):
                raise InvalidUserOperationError("paymaster", refused(Refusal.PAYMASTER_FIELDS))
        authorization = None
        if self.eip7702_auth is not None:
            try:
                authorization = self.eip7702_auth.signed()
            except InvalidSubmissionError as e:
                raise InvalidUserOperationError("eip7702Auth", e.refused) from e
        factory = self.factory
        if factory is not None:
            factory = EIP7702_MARKER if factory == EIP7702_FACTORY else to_checksum_address(factory)
        return UserOperation(
            entry_point=entry_point_at(to_checksum_address(entry_point)),
            sender=to_checksum_address(self.sender),
            nonce=int(self.nonce, 16),
            factory=factory,
            factory_data=b"" if self.factory_data is None else bytes.fromhex(self.factory_data[2:]),
            call_data=bytes.fromhex(self.call_data[2:]),
            call_gas_limit=_zero_if_none(self.call_gas_limit),
            verification_gas_limit=_zero_if_none(self.verification_gas_limit),
            pre_verification_gas=_zero_if_none(self.pre_verification_gas),
            max_fee_per_gas=int(self.max_fee_per_gas, 16),
            max_priority_fee_per_gas=int(self.max_priority_fee_per_gas, 16),
            signature=bytes.fromhex(self.signature[2:]),
            authorization=authorization,
            paymaster=paymaster,
        )


def _none_or_int(value: str | None) -> int | None:
    return None if value is None else int(value, 16)


def _zero_if_none(value: str | None) -> int:
    return 0 if value is None else int(value, 16)


class UserOperationParams(Wire, frozen=True):
    """An op, and the EntryPoint it's for: eth_estimateUserOperationGas's and
    eth_sendUserOperation's two parameters, REST's body."""

    user_operation: UserOperationJson
    entry_point: Address

    def operation(self) -> UserOperation:
        return self.user_operation.user_operation(self.entry_point)


class GasEstimateResult(Wire, frozen=True):
    """eth_estimateUserOperationGas's answer: the op's gas limits, its paymaster's too if it
    has one."""

    pre_verification_gas: Quantity
    verification_gas_limit: Quantity
    call_gas_limit: Quantity
    paymaster_verification_gas_limit: Quantity | None = None
    paymaster_post_op_gas_limit: Quantity | None = None

    @classmethod
    def of(cls, estimate: GasEstimate) -> GasEstimateResult:
        verification = estimate.paymaster_verification_gas_limit
        post_op = estimate.paymaster_post_op_gas_limit
        return cls(
            pre_verification_gas=hex(estimate.pre_verification_gas),
            verification_gas_limit=hex(estimate.verification_gas_limit),
            call_gas_limit=hex(estimate.call_gas_limit),
            paymaster_verification_gas_limit=None if verification is None else hex(verification),
            paymaster_post_op_gas_limit=None if post_op is None else hex(post_op),
        )


class GasPrice(Wire, frozen=True):
    """An op's two fees, as a bundler's gas price methods give them."""

    max_fee_per_gas: Quantity
    max_priority_fee_per_gas: Quantity


class GasPriceResult(Wire, frozen=True):
    """pimlico_getUserOperationGasPrice's and zd_getUserOperationGasPrice's answer: the price an
    op paying by its fees is asked, as both its fees, at each speed alike (the relay sends each
    op to every builder each block, at one price)."""

    slow: GasPrice
    standard: GasPrice
    fast: GasPrice

    @classmethod
    def of(cls, price: int) -> GasPriceResult:
        each = GasPrice(max_fee_per_gas=hex(price), max_priority_fee_per_gas=hex(price))
        return cls(slow=each, standard=each, fast=each)


class UserOperationSent(Wire, frozen=True):
    """An op taken, by its hash: REST's answer to sending one (eth_sendUserOperation's is the
    hash alone)."""

    user_op_hash: Hash


class UserOperationReceiptJson(Wire, frozen=True):
    """ERC-7769's receipt of an op: its UserOperationEvent's fields, the logs its execution
    emitted, and its transaction's receipt, both as the node gave them."""

    user_op_hash: Hash
    entry_point: Address
    sender: Address
    nonce: Quantity
    paymaster: Address
    actual_gas_cost: Quantity
    actual_gas_used: Quantity
    success: bool
    reason: Data
    logs: msgspec.Raw
    receipt: msgspec.Raw

    @classmethod
    def of(cls, receipt: UserOperationReceipt) -> UserOperationReceiptJson:
        return cls(
            user_op_hash=hex_data(receipt.user_op_hash),
            entry_point=receipt.entry_point,
            sender=receipt.sender,
            nonce=hex(receipt.nonce),
            paymaster=receipt.paymaster,
            actual_gas_cost=hex(receipt.actual_gas_cost),
            actual_gas_used=hex(receipt.actual_gas_used),
            success=receipt.success,
            reason=hex_data(receipt.reason),
            logs=msgspec.Raw(msgspec.json.encode(receipt.logs)),
            receipt=msgspec.Raw(msgspec.json.encode(receipt.receipt)),
        )


class SubmittedCallJson(Strict, frozen=True):
    """One of a submission's calls: EIP-5792's call, the ETH it sends, and the gas it's
    given."""

    to: Address
    data: Data
    value: Quantity = "0x0"
    gas: Quantity | None = None

    def encoded(self) -> EncodedCall:
        return EncodedCall(
            to_checksum_address(self.to), bytes.fromhex(self.data[2:]), int(self.value, 16)
        )

    def submitted(self) -> SubmittedCall:
        """The call as a submission makes it."""
        return SubmittedCall(self.encoded(), None if self.gas is None else int(self.gas, 16))


class AuthorizationJson(Wire, frozen=True):
    """An EIP-7702 authorization, as in a type-4 transaction's JSON (its signature's numbers
    padded or not)."""

    chain_id: Quantity
    address: Address
    nonce: Quantity
    y_parity: Word
    r: Word
    s: Word

    def signed(self) -> SignedSetCodeAuthorization:
        chain_id, nonce = int(self.chain_id, 16), int(self.nonce, 16)
        y_parity, r, s = int(self.y_parity, 16), int(self.r, 16), int(self.s, 16)
        address = bytes.fromhex(self.address[2:])
        check_signed(y_parity, r, s, nonce)
        signature = Signature(vrs=(y_parity, r, s))
        return SignedSetCodeAuthorization(
            chain_id=chain_id,
            address=address,
            nonce=nonce,
            y_parity=y_parity,
            r=r,
            s=s,
            signature=signature,
            authorization_hash=Authorization(chain_id, address, nonce).hash(),
        )


class TransferJson(Strict, frozen=True):
    """A payment's EIP-3009 transferWithAuthorization, signed: of the payment's amount, from
    `from` to its payee."""

    from_: Address = msgspec.field(name="from")
    valid_after: Quantity
    valid_before: Quantity
    nonce: Bytes32
    signature: SignatureHex

    def transfer(self, payee: str, amount: str) -> TransferAuthorization:
        return TransferAuthorization(
            payer=to_checksum_address(self.from_),
            to=to_checksum_address(payee),
            value=int(amount, 16),
            valid_after=int(self.valid_after, 16),
            valid_before=int(self.valid_before, 16),
            nonce=bytes.fromhex(self.nonce[2:]),
            signature=bytes.fromhex(self.signature[2:]),
        )


class PaymentJson(Strict, frozen=True):
    token: Address
    payee: Address
    amount: Quantity
    transfer: TransferJson | None = None


class SendParams(Strict, frozen=True):
    """A submission: its calls, each with its gas, and its payment."""

    calls: Calls[SubmittedCallJson]
    payment: PaymentJson
    max_block_number: Quantity
    authorization_list: tuple[AuthorizationJson, ...] = ()
    deployment: DeploymentJson | None = None

    def submission(self) -> Submission:
        return Submission(
            calls=tuple(call.submitted() for call in self.calls),
            authorizations=tuple(a.signed() for a in self.authorization_list),
            token=to_checksum_address(self.payment.token),
            payee=to_checksum_address(self.payment.payee),
            amount=int(self.payment.amount, 16),
            max_block_number=int(self.max_block_number, 16),
            transfer=None
            if self.payment.transfer is None
            else self.payment.transfer.transfer(self.payment.payee, self.payment.amount),
            deployment=None if self.deployment is None else self.deployment.deployment(),
        )


class UnsignedTransactionJson(Strict, frozen=True):
    """A transaction the sponsored account is to sign, as a sponsorship's quote is asked about
    it: its gas estimated if not given, its nonce and fees the quote's to fill in; no `to` for
    a contract creation."""

    to: Address | None = None
    data: Data = "0x"
    value: Quantity = "0x0"
    gas: Quantity | None = None

    def unsigned(self) -> Unsigned:
        return Unsigned(
            to=None if self.to is None else to_checksum_address(self.to),
            data=bytes.fromhex(self.data[2:]),
            value=int(self.value, 16),
            gas=None if self.gas is None else int(self.gas, 16),
        )


def decoded_transaction(raw: str, field: str) -> Decoded:
    """The signed transaction `raw` is, as hex: InvalidSubmissionError naming `field` if it
    isn't one."""
    try:
        return decoded(bytes.fromhex(raw[2:]))
    except TransactionError as e:
        raise InvalidSubmissionError(
            field, refused(Refusal.TRANSACTION_UNDECODABLE, why=str(e))
        ) from e


class SponsorshipQuoteParams(Strict, frozen=True):
    """A sponsorship to quote, and the tokens to quote it in: the `account` sponsored; its own
    transactions, each signed already (its raw bytes) or still to be (an object); its EIP-7702
    authorizations, signed, and the calls the relay makes before them, as a submission's, one
    call's gas estimated if not given; and its deadline, the furthest one if not given."""

    tokens: tuple[Address, ...]
    account: Address
    transactions: tuple[Data | UnsignedTransactionJson, ...] = ()
    authorization_list: tuple[AuthorizationJson, ...] = ()
    calls: SponsoredCalls = ()
    max_block_number: Quantity | None = None

    def asked(self) -> Asked:
        """The sponsorship to quote: InvalidSubmissionError with a transaction that isn't one,
        or anything that isn't the account's."""
        return Asked(
            account=to_checksum_address(self.account),
            authorizations=tuple(a.signed() for a in self.authorization_list),
            calls=tuple(call.submitted() for call in self.calls),
            transactions=tuple(
                decoded_transaction(t, f"transactions[{index}]")
                if isinstance(t, str)
                else t.unsigned()
                for index, t in enumerate(self.transactions)
            ),
            max_block_number=None
            if self.max_block_number is None
            else int(self.max_block_number, 16),
        )


class ToSignJson(Wire, frozen=True):
    """A transaction filled in for its account to sign (EIP-1559's, type 2), as a wallet takes
    it: no `to` for a contract creation."""

    type: Quantity
    chain_id: Quantity
    nonce: Quantity
    data: Data
    value: Quantity
    gas: Quantity
    max_fee_per_gas: Quantity
    max_priority_fee_per_gas: Quantity
    to: Address | None = None

    @classmethod
    def of(cls, to_sign: ToSign) -> ToSignJson:
        return cls(
            type="0x2",
            chain_id=hex(to_sign.chain_id),
            nonce=hex(to_sign.nonce),
            data=hex_data(to_sign.data),
            value=hex(to_sign.value),
            gas=hex(to_sign.gas),
            max_fee_per_gas=hex(to_sign.max_fee_per_gas),
            max_priority_fee_per_gas=hex(to_sign.max_priority_fee_per_gas),
            to=to_sign.to,
        )


class SponsorshipTokenQuote(Wire, frozen=True):
    """What a sponsorship is charged in `token`, and the payment of it its account signs after
    its own transactions, if it has any (`payment`)."""

    token: Address
    amount: Quantity
    payment: ToSignJson | None = None


class SponsorshipQuoteResult(Wire, frozen=True):
    """A sponsorship's quote in each token, valid until `validUntilBlock`: its deadline
    (`maxBlockNumber`), the forwarder it pays, the gas each of its calls is priced for, in
    order, and each of its transactions as filled in to sign, null for one signed already."""

    valid_until_block: Quantity
    max_block_number: Quantity
    forwarder: Address
    transactions: tuple[ToSignJson | None, ...]
    quotes: tuple[SponsorshipTokenQuote, ...]
    calls: tuple[QuotedCallGas, ...]

    @classmethod
    def of(cls, quotes: SponsoredQuotes) -> SponsorshipQuoteResult:
        return cls(
            valid_until_block=hex(quotes.valid_until_block),
            max_block_number=hex(quotes.max_block_number),
            forwarder=quotes.forwarder,
            transactions=tuple(
                None if t is None else ToSignJson.of(t) for t in quotes.transactions
            ),
            quotes=tuple(
                SponsorshipTokenQuote(
                    q.token,
                    hex(q.amount),
                    None if q.payment is None else ToSignJson.of(q.payment),
                )
                for q in quotes.quotes
            ),
            calls=tuple(QuotedCallGas(hex(gas)) for gas in quotes.gas),
        )


class SponsorshipPaymentJson(Strict, frozen=True):
    """A sponsorship's payment: at least `amount` of `token`, to the forwarder at `payee`."""

    token: Address
    payee: Address
    amount: Quantity


class SponsorshipParams(Strict, frozen=True):
    """A sponsorship: the `account` sponsored; its own transactions, each signed, as its raw
    bytes; its EIP-7702 authorizations, signed, and the calls the relay makes before them,
    each with its gas, as a submission's; its payment, to the forwarder; and its deadline."""

    account: Address
    payment: SponsorshipPaymentJson
    max_block_number: Quantity
    transactions: tuple[Data, ...] = ()
    authorization_list: tuple[AuthorizationJson, ...] = ()
    calls: SponsoredCalls = ()

    def sponsorship(self) -> Sponsorship:
        """The sponsorship taken: InvalidSubmissionError with a transaction that isn't one, or
        an authorization or transaction that isn't the account's."""
        transactions = tuple(
            decoded_transaction(raw, f"transactions[{index}]")
            for index, raw in enumerate(self.transactions)
        )
        return Sponsorship(
            account=to_checksum_address(self.account),
            authorizations=tuple(a.signed() for a in self.authorization_list),
            calls=tuple(call.submitted() for call in self.calls),
            transactions=transactions,
            token=to_checksum_address(self.payment.token),
            payee=to_checksum_address(self.payment.payee),
            amount=int(self.payment.amount, 16),
            max_block_number=int(self.max_block_number, 16),
        )


class SendResult(Wire, frozen=True):
    id: Hash

    @classmethod
    def of(cls, accepted: Accepted) -> SendResult:
        return cls(id=hex_data(accepted.id))


class StatusResult(Wire, frozen=True):
    status: str
    # waiting, sending, confirming, done or failed
    stage: str
    attempts: Quantity
    # every transaction of it that landed, in order
    transaction_hashes: tuple[Hash, ...] | None = None
    block_number: Quantity | None = None
    # the blocks from its own to the latest, its own counted: once included
    confirmations: Quantity | None = None
    # why, if it was rejected
    reason: Rejection | None = None
    revert_data: Data | None = None

    @classmethod
    def of(cls, tracked: Tracked | Finished, final: Finality | None) -> StatusResult:
        """`tracked`'s status; `final` the latest and finalized blocks, read if it's included."""
        if isinstance(tracked, Tracked):
            return cls(
                status=tracked.status.value,
                stage=tracked.stage(None).value,
                attempts=hex(tracked.attempts),
            )
        block = tracked.block_number
        confirmations = (
            None if final is None or block is None else hex(max(final.head - block + 1, 0))
        )
        return cls(
            status=tracked.status.value,
            stage=tracked.stage(None if final is None else final.finalized).value,
            attempts=hex(tracked.attempts),
            transaction_hashes=tuple(hex_data(h) for h in tracked.transaction_hashes) or None,
            block_number=None if block is None else hex(block),
            confirmations=confirmations,
            reason=tracked.reason,
            revert_data=None if tracked.revert_data is None else hex_data(tracked.revert_data),
        )


class Code(IntEnum):
    """JSON-RPC 2.0's own error codes, and the relay's."""

    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    UNDERPAID = -32010
    SIMULATION_FAILED = -32011
    UNSUPPORTED = -32012
    NO_CAPACITY = -32013
    UNKNOWN_SUBMISSION = -32014
    UNAVAILABLE = -32015


class ErrorObject(Wire, frozen=True):
    """JSON-RPC's error object; a REST error's body is {"error": this}. Every error the relay
    raises has its data, its refusal its `reason`; JSON-RPC's own (a parse error, a method
    unknown) and an internal error have none."""

    code: Code
    message: str
    data: ErrorData | None = None


class ErrorData(Wire, frozen=True):
    """An error's data: why (`reason`, its refusal), and what its code adds: for invalid params
    or an Invalid Request, the field at fault, as a path into the request's parameter
    (`call.to`, `authorizationList[0]`), named as the interface names it over REST and JSON-RPC
    alike, and if it's over a limit, the `most` it may be and what it `needs`, where that isn't
    what was given; for Unsupported or Unavailable, who can act on it (`category`, the
    refusal's); for underpaid or no capacity, what's required in `token` to be taken; for a
    simulation failed, the revert data whole (`reason` the revert's); for a chain not served,
    the chain, as the path names it."""

    reason: Refusal
    field: str | None = None
    most: Quantity | None = None
    needs: Quantity | None = None
    category: Category | None = None
    token: Address | None = None
    required: Quantity | None = None
    revert_data: Data | None = None
    chain: str | None = None

    @classmethod
    def invalid(cls, reason: Refusal, field: str, bound: Bound | None = None) -> ErrorData:
        """Invalid params naming `field`, and the limit it's over (`bound`), if that's why."""
        if bound is None:
            return cls(reason, field)
        needs = None if bound.needs is None else hex(bound.needs)
        return cls(reason, field, hex(bound.most), needs)

    @classmethod
    def of(cls, reason: Refusal) -> ErrorData:
        """An Unsupported or Unavailable error's: `reason`, and who can act on it."""
        return cls(reason, category=reason.category)


def invalid(code: Code, field: str, why: str) -> ErrorObject:
    """An invalid params or Invalid Request error naming `field`, its type or shape wrong: the
    decoder's own words (`malformed`)."""
    return ErrorObject(code, f"{field}: {why}", ErrorData.invalid(Refusal.MALFORMED, field))


def invalid_value(code: Code, why: str, path: str, whole: str) -> ErrorObject:
    """msgspec's account of a value that isn't its type, `why` at `path` within it: the error
    naming the field, or `whole` if it's the whole value."""
    at = Invalid.at(why, path, whole)
    return invalid(code, at.field, at.why)


def validation_error(code: Code, e: msgspec.ValidationError, whole: str) -> ErrorObject:
    """msgspec's ValidationError `e` as the error naming the field; `whole` if it's the whole
    value."""
    at = Invalid.of(e, whole)
    return invalid(code, at.field, at.why)


class ErrorBody(Wire, frozen=True):
    error: ErrorObject


class RpcRequest(Wire, frozen=True):
    """A JSON-RPC request: its params by position, or by name, which no method here takes;
    "jsonrpc" exactly "2.0", as JSON-RPC 2.0 requires."""

    jsonrpc: Literal["2.0"]
    method: str
    params: tuple[msgspec.Raw, ...] | dict[str, msgspec.Raw] = ()
    id: int | str | None = None


class RpcResponse(msgspec.Struct, frozen=True):
    """A JSON-RPC response: `result` or `error`, the other left out."""

    id: int | str | None
    jsonrpc: str = "2.0"
    result: object | msgspec.UnsetType = msgspec.UNSET
    error: ErrorObject | msgspec.UnsetType = msgspec.UNSET


# x402 v2 (github.com/coinbase/x402, specs/x402-specification-v2.md): its own encoding,
# amounts and times as decimal strings, and its own field names


class X402Params(Strict, frozen=True):
    """The calls an x402 payment is for: the submission less its payment and deadline, which
    the payment brings; one call's gas estimated if not given."""

    calls: Calls[SubmittedCallJson]
    authorization_list: tuple[AuthorizationJson, ...] = ()
    payer: Address | None = None
    deployment: DeploymentJson | None = None
    verify_gas: Quantity | None = None

    def payer_of(self) -> Payer:
        return payer_of(self.payer, self.deployment, self.verify_gas)

    def stated_gas(self) -> tuple[int | None, ...]:
        """Each call's gas, if it states it."""
        return tuple(None if call.gas is None else int(call.gas, 16) for call in self.calls)

    def quoted(self) -> Quoted:
        """The calls as their challenge quotes them."""
        calls = (call.encoded() for call in self.calls)
        return Quoted(
            tuple(
                QuotedCall(call, len(call.data), None if given.gas is None else int(given.gas, 16))
                for call, given in zip(calls, self.calls, strict=True)
            ),
            tuple(a.signed() for a in self.authorization_list),
        )

    def submission(
        self,
        token: ChecksumAddress,
        payee: ChecksumAddress,
        deadline: int,
        transfer: TransferAuthorization,
        gas: Sequence[int | None],
    ) -> Submission:
        """The submission an x402 payment of `token` to `payee` by `transfer` makes of these
        calls, each given its `gas`, included by `deadline`."""
        return Submission(
            calls=tuple(
                SubmittedCall(call.encoded(), given)
                for call, given in zip(self.calls, gas, strict=True)
            ),
            authorizations=tuple(a.signed() for a in self.authorization_list),
            token=token,
            payee=payee,
            amount=transfer.value,
            max_block_number=deadline,
            transfer=transfer,
            deployment=None if self.deployment is None else self.deployment.deployment(),
        )


class TokenDomain(Wire, frozen=True):
    """`exact`'s `extra` on EVM: the token's EIP-712 domain, which the client signs for; and the
    protocol's reserved keys, as a client may echo them: how the payment moves (EIP-3009 by
    default) and when it settles (`authorization` by default)."""

    name: str
    version: str
    asset_transfer_method: str | None = None
    payment_flow: str | None = None


class PaymentRequirements(Wire, frozen=True):
    scheme: str
    network: str
    amount: str
    asset: Address
    pay_to: Address
    max_timeout_seconds: int
    extra: TokenDomain | None = None


class ResourceInfo(Wire, frozen=True):
    url: str
    description: str | None = None
    mime_type: str | None = None
    service_name: str | None = None
    tags: tuple[str, ...] = ()


class Extension(Wire, frozen=True):
    """An extension's data, and the JSON Schema it's validated by."""

    info: dict[str, object]
    schema: dict[str, object]


class PaymentRequired(Wire, frozen=True):
    x402_version: int
    resource: ResourceInfo
    accepts: tuple[PaymentRequirements, ...]
    error: str | None = None
    extensions: dict[str, Extension] | None = None


type DecimalInteger = Annotated[
    str,
    msgspec.Meta(pattern=whole("0|[1-9][0-9]{0,77}"), description="An integer, as decimal"),
]


class Eip3009Authorization(Wire, frozen=True):
    """An EIP-3009 authorization's fields, as `exact` on EVM carries them."""

    from_: Address = msgspec.field(name="from")
    to: Address
    value: DecimalInteger
    valid_after: DecimalInteger
    valid_before: DecimalInteger
    nonce: Bytes32


class ExactEvmPayload(Wire, frozen=True):
    """`exact`'s payload on EVM: the authorization, and its signature: a key's 65 bytes, a
    contract payer's (ERC-1271), or ERC-6492-wrapped, as x402's reference facilitator takes
    them."""

    signature: SignatureHex
    authorization: Eip3009Authorization


class PaymentPayload(Wire, frozen=True):
    x402_version: int
    accepted: PaymentRequirements
    payload: ExactEvmPayload
    resource: ResourceInfo | None = None
    extensions: dict[str, object] | None = None

    def transfer(self) -> TransferAuthorization:
        """The EIP-3009 transfer this payment authorizes, its signature as signed."""
        authorization = self.payload.authorization
        return TransferAuthorization(
            payer=to_checksum_address(authorization.from_),
            to=to_checksum_address(authorization.to),
            value=int(authorization.value),
            valid_after=int(authorization.valid_after),
            valid_before=int(authorization.valid_before),
            nonce=bytes.fromhex(authorization.nonce[2:]),
            signature=bytes.fromhex(self.payload.signature[2:]),
        )


class SettleResponse(Wire, frozen=True):
    success: bool
    transaction: str
    network: str
    error_reason: str | None = None
    payer: Address | None = None
    amount: str | None = None
    extensions: dict[str, Extension] | None = None


class FacilitatorRequest(Wire, frozen=True):
    """What x402's /verify and /settle take: a payment, and what the resource server required
    of it."""

    x402_version: int
    payment_payload: PaymentPayload
    payment_requirements: PaymentRequirements


class VerifyResponse(Wire, frozen=True):
    is_valid: bool
    invalid_reason: str | None = None
    payer: Address | None = None


class SupportedKind(Wire, frozen=True):
    x402_version: int
    scheme: str
    network: str


class SupportedResponse(Wire, frozen=True, omit_defaults=False):
    """x402's /supported: the kinds of payment settled, the extensions implemented, and the
    accounts that send, by chain."""

    kinds: tuple[SupportedKind, ...]
    extensions: tuple[str, ...]
    signers: dict[str, tuple[Address, ...]]


class FeeSplit(Wire, frozen=True, omit_defaults=False):
    """The `bruno-fee` extension's outcome, for the merchant: a payment of `amount` of `asset`
    in parts, each in its base units, decimal. The `fee` Bruno keeps, what the receipt wallet
    keeps (`kept`: one unit, the first time it holds the asset), and the merchant's `payout`,
    which together are the amount."""

    asset: Address
    amount: str
    fee: str
    kept: str
    payout: str


class FacilitatorInfo(Wire, frozen=True):
    """A payout address's facilitator: where its buyers pay on each chain (`payTo`, by CAIP-2
    network), which is what a merchant puts in its payment requirements."""

    payout: Address
    pay_to: dict[str, Address]


class X402Result(Wire, frozen=True):
    """A paid submission taken: its id, and where its status is."""

    id: Hash
    status_url: str


class WellKnownX402(Wire, frozen=True):
    """/.well-known/x402: the x402 endpoints served, for discovery's crawlers."""

    version: int
    resources: tuple[str, ...]
