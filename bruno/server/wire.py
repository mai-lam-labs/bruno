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
from ..relay import Quotes, Terms
from ..submission import InvalidSubmissionError, Submission
from ..tracked import Tracked

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


class ChainEntry(Wire, frozen=True):
    """A chain served: its id, its network's name if it has one, its interface's URLs, and
    the payee, to which donations go too."""

    chain_id: int
    json_rpc: str
    rest: str
    payee: Address
    name: str | None = None


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
