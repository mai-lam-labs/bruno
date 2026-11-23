"""The operations, the same over REST and JSON-RPC, and the error object a relay error is
answered with."""

import logging
import re
from importlib.metadata import metadata

from eth_utils.address import to_checksum_address

from ..bundler import Bundler
from ..common import BrunoError
from ..evm import NATIVE_ASSET
from ..measurements import UnavailableError
from ..plan import SimulationFailedError
from ..pricing import UnsupportedError
from ..refusals import Refusal, Refused, Wording, message, refused
from ..relay import Accepted, NoCapacityError, Relay, UnderpaidError
from ..submission import InvalidSubmissionError
from ..tracked import Status, UnknownSubmissionError
from ..userops import InvalidUserOperationError
from .wire import (
    HASH_PATTERN,
    Call,
    Code,
    ErrorData,
    ErrorObject,
    GasEstimateResult,
    GasPriceResult,
    QuoteParams,
    QuoteResult,
    SendParams,
    StatusResult,
    TermsResult,
    UserOperationParams,
    UserOperationReceiptJson,
    hex_data,
)

logger = logging.getLogger(__name__)

# the installed distribution's own metadata (pyproject.toml's name, description and
# version), found by the top-level package's name
_METADATA = metadata(__name__.partition(".")[0])
TITLE = _METADATA["Name"]
DESCRIPTION = _METADATA["Summary"]
VERSION = _METADATA["Version"]
# pyproject.toml's project.urls, each "label, url"
_URLS = dict(url.split(", ", 1) for url in _METADATA.get_all("Project-URL") or ())
REPOSITORY = _URLS["Repository"]
# this version's source, its tag in the repository: what the AGPL has a network service offer its
# users
SOURCE = f"{REPOSITORY}/tree/v{VERSION}"
# the documentation site: the specification, the design, running a relay, the clients
DOCUMENTATION = _URLS["Documentation"]
# what an error that isn't the client's is answered with: what failed stays in the log (a
# node's URL, its key in it, isn't the client's to see)
INTERNAL_ERROR = ErrorObject(Code.INTERNAL_ERROR, "internal error")
# what a node failing a request is answered with, the relay unavailable for now: to try again
NODE_FAILED = ErrorObject(
    Code.UNAVAILABLE, message(Refusal.NODE_FAILED), ErrorData.of(Refusal.NODE_FAILED)
)


def node_failed(exc: Exception) -> ErrorObject:
    """NODE_FAILED, for a node's failure (no answer, a page, an error that isn't a revert):
    what failed logged, not answered."""
    logger.warning("a node failed: %s", exc)
    return NODE_FAILED


# the documents' example quote, on any chain: a call to an account without code, its gas
# estimated, in ETH
QUOTE_EXAMPLE = QuoteParams(
    (NATIVE_ASSET,), call=Call(to="0x000000000000000000000000000000000000dEaD", data="0x")
)


class InvalidParamsError(BrunoError):
    """A request's field is the client's mistake, beyond what its type says: `field`, and why
    (`refused`)."""

    def __init__(self, field: str, refused: Refused) -> None:
        super().__init__(f"{field}: {refused}")
        self.field = field
        self.refused = refused


# a hash that isn't one: its shape, as a decoder refuses it
_malformed_hash = refused(Refusal.MALFORMED, why=message(Wording.HASH_MALFORMED))


class NotServedError(BrunoError):
    """A request to a chain this relay doesn't serve, as the path names it."""

    def __init__(self, chain: str) -> None:
        self.refused = refused(Refusal.NOT_SERVED, chain=chain)
        super().__init__(str(self.refused))
        self.chain = chain


class NoX402Error(BrunoError):
    """An x402 request to a chain that takes no x402 payment."""

    def __init__(self, chain_id: int) -> None:
        self.refused = refused(Refusal.NO_X402, chain=chain_id)
        super().__init__(str(self.refused))


def terms(relay: Relay) -> TermsResult:
    return TermsResult.of(relay.terms())


async def quote(relay: Relay, params: QuoteParams) -> QuoteResult:
    """The quotes in each token named, once each: no more tokens than the relay accepts."""
    tokens = list(dict.fromkeys(to_checksum_address(t) for t in params.tokens))
    if not tokens:
        raise InvalidParamsError("tokens", refused(Refusal.TOKENS_NONE))
    if len(tokens) > len(relay.terms().tokens):
        raise InvalidParamsError("tokens", refused(Refusal.TOKENS_TOO_MANY))
    quotes = await relay.quote(params.quoted(), tokens, params.payer_of())
    return QuoteResult.of(quotes)


async def estimate_user_operation(relay: Relay, params: UserOperationParams) -> GasEstimateResult:
    """The op's gas limits (ERC-7769's eth_estimateUserOperationGas)."""
    return GasEstimateResult.of(await Bundler(relay).estimate_user_operation(params.operation()))


async def quote_user_operation(relay: Relay, params: UserOperationParams) -> QuoteResult:
    """What the op, paying by its calls, is to pay in each token (postpaid_quoteUserOperation)."""
    return QuoteResult.of(await Bundler(relay).quote_user_operation(params.operation()))


async def user_operation_gas_price(relay: Relay) -> GasPriceResult:
    """The price an op paying by its fees is asked (pimlico_getUserOperationGasPrice's and
    zd_getUserOperationGasPrice's answer)."""
    return GasPriceResult.of(await Bundler(relay).user_operation_gas_price())


async def send_user_operation(relay: Relay, params: UserOperationParams) -> str:
    """The op taken, its hash (ERC-7769's eth_sendUserOperation)."""
    return hex_data(await Bundler(relay).send_user_operation(params.operation()))


async def user_operation_receipt(relay: Relay, op_hash: str) -> UserOperationReceiptJson | None:
    """ERC-7769's receipt of an op the relay took: None until it's included."""
    if re.fullmatch(HASH_PATTERN, op_hash) is None:
        raise InvalidParamsError("userOpHash", _malformed_hash)
    receipt = await Bundler(relay).user_operation_receipt(bytes.fromhex(op_hash[2:]))
    return None if receipt is None else UserOperationReceiptJson.of(receipt)


async def send(relay: Relay, params: SendParams) -> Accepted:
    """Take the submission in `params`: its id, and whether it's new."""
    return await relay.submit(params.submission())


async def status_of(relay: Relay, id_: str) -> StatusResult:
    """What's become of a submission, and its stage: the latest and finalized blocks read for
    one that's included, at most once a slot."""
    if re.fullmatch(HASH_PATTERN, id_) is None:
        raise InvalidParamsError("id", _malformed_hash)
    tracked = relay.queue.status(bytes.fromhex(id_[2:]))
    if tracked.status is not Status.INCLUDED:
        return StatusResult.of(tracked, None)
    return StatusResult.of(tracked, await relay.finality())


def error_object(exc: BrunoError) -> ErrorObject:
    """The error object a relay error is answered with: one that isn't the client's an internal
    error."""
    match exc:
        case InvalidSubmissionError() | InvalidUserOperationError():
            data = ErrorData.invalid(exc.refused.refusal, exc.field, exc.bound)
            return ErrorObject(Code.INVALID_PARAMS, str(exc), data)
        case InvalidParamsError():
            data = ErrorData.invalid(exc.refused.refusal, exc.field)
            return ErrorObject(Code.INVALID_PARAMS, str(exc), data)
        case UnderpaidError():
            required = ErrorData(exc.refused.refusal, token=exc.token, required=hex(exc.required))
            return ErrorObject(Code.UNDERPAID, str(exc), required)
        case SimulationFailedError():
            reverted = ErrorData(exc.refused.refusal, revert_data=hex_data(exc.revert_data))
            return ErrorObject(Code.SIMULATION_FAILED, str(exc), reverted)
        case UnsupportedError():
            return ErrorObject(Code.UNSUPPORTED, str(exc), ErrorData.of(exc.refused.refusal))
        case NoCapacityError():
            required = ErrorData(exc.refused.refusal, token=exc.token, required=hex(exc.required))
            return ErrorObject(Code.NO_CAPACITY, str(exc), required)
        case NotServedError():
            return ErrorObject(
                Code.METHOD_NOT_FOUND, str(exc), ErrorData(exc.refused.refusal, chain=exc.chain)
            )
        case NoX402Error():
            return ErrorObject(Code.METHOD_NOT_FOUND, str(exc), ErrorData(exc.refused.refusal))
        case UnknownSubmissionError():
            return ErrorObject(Code.UNKNOWN_SUBMISSION, str(exc), ErrorData(exc.refused.refusal))
        case UnavailableError():
            return ErrorObject(Code.UNAVAILABLE, str(exc), ErrorData.of(exc.refused.refusal))
        case _:
            logger.error("internal error", exc_info=exc)
            return INTERNAL_ERROR
