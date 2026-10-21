"""The interface's requests and responses, the same for REST and JSON-RPC: Ethereum
JSON-RPC's encoding (hex quantities and data, checksummed addresses), camelCase, as msgspec
Structs, which Litestar decodes, validates and documents. Each converts to or from the
relay's own types."""

from enum import IntEnum
from typing import Annotated

import msgspec
from eth_account.datastructures import SignedSetCodeAuthorization
from eth_account.typed_transactions.set_code_transaction import Authorization
from eth_keys.datatypes import Signature
from eth_keys.exceptions import BadSignature
from eth_typing import HexStr
from eth_utils.address import to_checksum_address

from ..evm import EncodedCall
from ..maintenance import AccountsStatus
from ..relay import Quotes, Terms
from ..submission import InvalidSubmissionError, Submission
from ..tracked import Tracked
from ..treasury import Snapshot

ADDRESS_PATTERN = "^0x[0-9a-fA-F]{40}$"
QUANTITY_PATTERN = "^0x(0|[1-9a-f][0-9a-f]*)$"
DATA_PATTERN = "^0x([0-9a-fA-F]{2})*$"
HASH_PATTERN = "^0x[0-9a-f]{64}$"

type Address = Annotated[str, msgspec.Meta(pattern=ADDRESS_PATTERN, description="An address")]
type Quantity = Annotated[
    str, msgspec.Meta(pattern=QUANTITY_PATTERN, description="A quantity, as hex")
]
type Data = Annotated[str, msgspec.Meta(pattern=DATA_PATTERN, description="Bytes, as hex")]
type Hash = Annotated[str, msgspec.Meta(pattern=HASH_PATTERN, description="32 bytes, as hex")]


class Wire(msgspec.Struct, frozen=True, rename="camel", omit_defaults=True):
    pass


def quantity(value: int) -> str:
    return hex(value)


def data(value: bytes) -> str:
    return "0x" + value.hex()


class HoldingEntry(Wire, frozen=True):
    """What an account holds of a token (ETH as ERC-7528's address), and its decimals and
    symbol."""

    token: Address
    amount: Quantity
    decimals: int
    symbol: str


class TreasuryEntry(Wire, frozen=True):
    """The chain's money as the treasury's last run found it, at `block` (`at`, Unix
    seconds): the float and each concurrency account, the payee's holdings, the float's
    target and the payee's reserve, how many transactions of the largest gas the float and
    accounts cover, the spend a day measured (none on the first run), and what the run did."""

    at: Quantity
    block: Quantity
    float_balance: Quantity
    accounts: tuple[Quantity, ...]
    payee: tuple[HoldingEntry, ...]
    target: Quantity
    reserve: Quantity
    covers: Quantity
    outcome: str
    spend_a_day: Quantity | None = None

    @classmethod
    def of(cls, snapshot: Snapshot) -> TreasuryEntry:
        return cls(
            at=quantity(snapshot.at),
            block=quantity(snapshot.block),
            float_balance=quantity(snapshot.float_balance),
            accounts=tuple(quantity(a) for a in snapshot.accounts),
            payee=tuple(
                HoldingEntry(h.token, quantity(h.amount), h.decimals, h.symbol)
                for h in snapshot.payee
            ),
            target=quantity(snapshot.target),
            reserve=quantity(snapshot.reserve),
            covers=quantity(snapshot.covers),
            outcome=snapshot.outcome,
            spend_a_day=None if snapshot.spend_a_day is None else quantity(snapshot.spend_a_day),
        )


class AccountsEntry(Wire, frozen=True):
    """The float and each concurrency account's balance and nonce, as last read, at
    `block`."""

    block: Quantity
    float_balance: Quantity
    float_nonce: Quantity
    balances: tuple[Quantity, ...]
    nonces: tuple[Quantity, ...]

    @classmethod
    def of(cls, status: AccountsStatus) -> AccountsEntry:
        return cls(
            block=quantity(status.block),
            float_balance=quantity(status.float_balance),
            float_nonce=quantity(status.float_nonce),
            balances=tuple(quantity(a.balance) for a in status.accounts),
            nonces=tuple(quantity(a.nonce) for a in status.accounts),
        )


class ChainEntry(Wire, frozen=True):
    """A chain served: its id, its network's name if it has one, its interface's URLs, the
    payee, to which donations go too, the float and its concurrency accounts and their
    balances and nonces as last read, its block explorer if it has one, its treasury's last
    run, if it has one, its x402 endpoint and the tokens it takes there, if any, and what's
    wrong with it as last measured: none if all's well."""

    chain_id: int
    json_rpc: str
    rest: str
    payee: Address
    name: str | None = None
    float_account: Address | None = None
    concurrency_accounts: tuple[Address, ...] = ()
    explorer: str | None = None
    accounts_status: AccountsEntry | None = None
    treasury: TreasuryEntry | None = None
    x402: str | None = None
    x402_tokens: tuple[Address, ...] = ()
    problems: tuple[str, ...] = ()


class IndexResult(Wire, frozen=True):
    """The relay's root: what it is, where its documents are, and the chains it serves."""

    title: str
    description: str
    version: str
    spec: str
    repository: str
    openrpc: str
    openapi: str
    chains: tuple[ChainEntry, ...]


class TermsResult(Wire, frozen=True):
    payee: Address
    tokens: tuple[Address, ...]
    max_gas: Quantity
    max_call_bytes: Quantity

    @classmethod
    def of(cls, terms: Terms) -> TermsResult:
        return cls(
            payee=terms.payee,
            tokens=terms.tokens,
            max_gas=quantity(terms.max_gas),
            max_call_bytes=quantity(terms.max_call_bytes),
        )


class QuoteParams(Wire, frozen=True):
    """The call's gas and length, the account that will sign an authorization (none if
    there isn't one), and the tokens to quote in."""

    gas: Quantity
    call_bytes: Quantity
    tokens: tuple[Address, ...]
    authority: Address | None = None


class TokenQuote(Wire, frozen=True):
    token: Address
    amount: Quantity


class QuoteResult(Wire, frozen=True):
    valid_until_block: Quantity
    quotes: tuple[TokenQuote, ...]

    @classmethod
    def of(cls, quotes: Quotes) -> QuoteResult:
        return cls(
            valid_until_block=quantity(quotes.valid_until_block),
            quotes=tuple(TokenQuote(q.token, quantity(q.amount)) for q in quotes.quotes),
        )


class Call(Wire, frozen=True):
    """EIP-5792's call, value 0."""

    to: Address
    data: Data


class AuthorizationJson(Wire, frozen=True):
    """An EIP-7702 authorization, as in a type-4 transaction's JSON."""

    chain_id: Quantity
    address: Address
    nonce: Quantity
    y_parity: Quantity
    r: Quantity
    s: Quantity

    def signed(self) -> SignedSetCodeAuthorization:
        chain_id, nonce = int(self.chain_id, 16), int(self.nonce, 16)
        y_parity, r, s = int(self.y_parity, 16), int(self.r, 16), int(self.s, 16)
        address = bytes.fromhex(self.address[2:])
        try:
            signature = Signature(vrs=(y_parity, r, s))
        except BadSignature as e:
            raise InvalidSubmissionError("authorizationList", str(e)) from e
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


class PaymentJson(Wire, frozen=True):
    token: Address
    payee: Address
    amount: Quantity


class SendParams(Wire, frozen=True):
    call: Call
    gas: Quantity
    payment: PaymentJson
    max_block_number: Quantity
    authorization_list: tuple[AuthorizationJson, ...] = ()

    def submission(self) -> Submission:
        return Submission(
            call=EncodedCall(to_checksum_address(self.call.to), HexStr(self.call.data.lower())),
            authorizations=tuple(a.signed() for a in self.authorization_list),
            gas=int(self.gas, 16),
            token=to_checksum_address(self.payment.token),
            payee=to_checksum_address(self.payment.payee),
            amount=int(self.payment.amount, 16),
            max_block_number=int(self.max_block_number, 16),
        )


class SendResult(Wire, frozen=True):
    id: Hash


class StatusResult(Wire, frozen=True):
    status: str
    attempts: Quantity
    transaction_hash: Hash | None = None
    block_number: Quantity | None = None
    reason: str | None = None
    revert_data: Data | None = None

    @classmethod
    def of(cls, tracked: Tracked) -> StatusResult:
        return cls(
            status=tracked.status.value,
            attempts=quantity(tracked.attempts),
            transaction_hash=None
            if tracked.transaction_hash is None
            else data(tracked.transaction_hash),
            block_number=None if tracked.block_number is None else quantity(tracked.block_number),
            reason=tracked.reason,
            revert_data=None if tracked.revert_data is None else data(tracked.revert_data),
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
    """JSON-RPC's error object; a REST error's body is {"error": this}."""

    code: Code
    message: str
    data: object | None = None


class ErrorBody(Wire, frozen=True):
    error: ErrorObject


class RpcRequest(Wire, frozen=True):
    method: str
    jsonrpc: str = "2.0"
    params: tuple[msgspec.Raw, ...] = ()
    id: int | str | None = None


class RpcResponse(msgspec.Struct, frozen=True):
    """A JSON-RPC response: `result` or `error`, the other left out."""

    id: int | str | None
    jsonrpc: str = "2.0"
    result: object | msgspec.UnsetType = msgspec.UNSET
    error: ErrorObject | msgspec.UnsetType = msgspec.UNSET


# x402 v2 (github.com/coinbase/x402, specs/x402-specification-v2.md): its own encoding,
# amounts and times as decimal strings, and its own field names


class X402Params(Wire, frozen=True):
    """The call an x402 payment is for: the submission less its payment and deadline, which
    the payment brings."""

    call: Call
    gas: Quantity
    authorization_list: tuple[AuthorizationJson, ...] = ()


class TokenDomain(Wire, frozen=True):
    """`exact`'s `extra` on EVM: the token's EIP-712 domain, which the client signs for."""

    name: str
    version: str


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
    str, msgspec.Meta(pattern="^(0|[1-9][0-9]*)$", description="An integer, as decimal")
]


class Eip3009Authorization(Wire, frozen=True):
    """An EIP-3009 authorization's fields, as `exact` on EVM carries them."""

    from_: Address = msgspec.field(name="from")
    to: Address
    value: DecimalInteger
    valid_after: DecimalInteger
    valid_before: DecimalInteger
    nonce: Annotated[str, msgspec.Meta(pattern="^0x[0-9a-fA-F]{64}$")]


class ExactEvmPayload(Wire, frozen=True):
    """`exact`'s payload on EVM: the authorization, and its 65-byte signature."""

    signature: Annotated[str, msgspec.Meta(pattern="^0x[0-9a-fA-F]{130}$")]
    authorization: Eip3009Authorization


class PaymentPayload(Wire, frozen=True):
    x402_version: int
    accepted: PaymentRequirements
    payload: ExactEvmPayload
    resource: ResourceInfo | None = None
    extensions: dict[str, object] | None = None


class SettleResponse(Wire, frozen=True):
    success: bool
    transaction: str
    network: str
    error_reason: str | None = None
    payer: Address | None = None
    amount: str | None = None
    extensions: dict[str, Extension] | None = None


class X402Result(Wire, frozen=True):
    """A paid submission taken: its id, and where its status is."""

    id: Hash
    status_url: str


class WellKnownX402(Wire, frozen=True):
    """/.well-known/x402: the x402 endpoints served, for discovery's crawlers."""

    version: int
    resources: tuple[str, ...]
