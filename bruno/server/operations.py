"""The operations, the same over REST and JSON-RPC, and the error object a relay error is
answered with."""

import logging
from importlib.metadata import metadata

from eth_utils.address import to_checksum_address

from ..common import BrunoError
from ..evm import NATIVE_ASSET
from ..plan import SimulationFailedError
from ..relay import (
    NoCapacityError,
    Relay,
    UnavailableError,
    UnderpaidError,
    UnknownSubmissionError,
    UnsupportedError,
)
from ..submission import InvalidSubmissionError
from ..tracked import Status
from ..userops import InvalidUserOperationError
from .wire import (
    Code,
    ErrorObject,
    EstimateParams,
    GasEstimateResult,
    QuoteParams,
    QuoteResult,
    SendParams,
    SendResult,
    StatusResult,
    TermsResult,
    data,
    quantity,
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
# the documentation site: the specification, the design, running a relay, the clients
DOCUMENTATION = _URLS["Documentation"]
# what an error that isn't the client's is answered with: what failed stays in the log (a
# node's URL, its key in it, isn't the client's to see)
INTERNAL_ERROR = ErrorObject(Code.INTERNAL_ERROR, "internal error")
# the documents' example quote, on any chain: a call of 100,000 gas and 512 bytes, in ETH
QUOTE_EXAMPLE = QuoteParams(quantity(100_000), quantity(512), (NATIVE_ASSET,))


class InvalidParamsError(BrunoError):
    """A request's field is the client's mistake, beyond what its type says."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field


def terms(relay: Relay) -> TermsResult:
    return TermsResult.of(relay.terms())


async def quote(relay: Relay, params: QuoteParams) -> QuoteResult:
    """The quotes in each token named, once each: no more tokens than the relay accepts."""
    authority = None if params.authority is None else to_checksum_address(params.authority)
    tokens = list(dict.fromkeys(to_checksum_address(t) for t in params.tokens))
    if not tokens:
        raise InvalidParamsError("tokens", "at least one")
    if len(tokens) > len(relay.terms().tokens):
        raise InvalidParamsError("tokens", "more than are accepted")
    quotes = await relay.quote(
        int(params.gas, 16), int(params.call_bytes, 16), authority, tokens, params.payer_of()
    )
    return QuoteResult.of(quotes)


async def estimate_user_operation(relay: Relay, params: EstimateParams) -> GasEstimateResult:
    """The op's gas limits (ERC-7769's eth_estimateUserOperationGas)."""
    op = params.user_operation.user_operation()
    entry_point = to_checksum_address(params.entry_point)
    return GasEstimateResult.of(await relay.estimate_user_operation(op, entry_point))


async def send(relay: Relay, params: SendParams) -> tuple[SendResult, bool]:
    """The submission's id, and whether it's new."""
    accepted = await relay.submit(params.submission())
    return SendResult(id=data(accepted.id)), accepted.new


async def status(relay: Relay, id_: str) -> StatusResult:
    """What's become of a submission, and its stage: the latest and finalized blocks read for
    one that's included, at most once a slot."""
    tracked = relay.status(bytes.fromhex(id_[2:]))
    if tracked.status is not Status.INCLUDED:
        return StatusResult.of(tracked, None)
    return StatusResult.of(tracked, await relay.finality())


def error_object(exc: BrunoError) -> ErrorObject:
    """The error object a relay error is answered with: one that isn't the client's an internal
    error."""
    match exc:
        case InvalidSubmissionError() | InvalidParamsError() | InvalidUserOperationError():
            return ErrorObject(Code.INVALID_PARAMS, str(exc), exc.field)
        case UnderpaidError():
            return ErrorObject(
                Code.UNDERPAID, str(exc), {"token": exc.token, "required": quantity(exc.required)}
            )
        case SimulationFailedError():
            return ErrorObject(
                Code.SIMULATION_FAILED, str(exc), {"revertData": data(exc.revert_data)}
            )
        case UnsupportedError():
            return ErrorObject(Code.UNSUPPORTED, str(exc))
        case NoCapacityError():
            return ErrorObject(
                Code.NO_CAPACITY, str(exc), {"token": exc.token, "required": quantity(exc.required)}
            )
        case UnknownSubmissionError():
            return ErrorObject(Code.UNKNOWN_SUBMISSION, str(exc))
        case UnavailableError():
            return ErrorObject(Code.UNAVAILABLE, str(exc))
        case _:
            logger.error("internal error", exc_info=exc)
            return INTERNAL_ERROR
