"""The four operations, the same over REST and JSON-RPC, and the error object a relay error
is answered with."""

from importlib.metadata import metadata

from eth_utils.address import to_checksum_address

from ..common import BrunoError
from ..relay import (
    InvalidSubmissionError,
    NoCapacityError,
    Relay,
    SimulationFailedError,
    UnderpaidError,
    UnknownSubmissionError,
    UnsupportedError,
)
from .wire import (
    Code,
    ErrorObject,
    QuoteParams,
    QuoteResult,
    SendParams,
    SendResult,
    StatusResult,
    TermsResult,
    data,
    quantity,
)

# the installed distribution's own metadata (pyproject.toml's name, description and
# version), found by the top-level package's name
_METADATA = metadata(__name__.partition(".")[0])
TITLE = _METADATA["Name"]
DESCRIPTION = _METADATA["Summary"]
VERSION = _METADATA["Version"]


def terms(relay: Relay) -> TermsResult:
    return TermsResult.of(relay.terms())


async def quote(relay: Relay, params: QuoteParams) -> QuoteResult:
    authority = None if params.authority is None else to_checksum_address(params.authority)
    quotes = await relay.quote(
        int(params.gas, 16),
        int(params.call_bytes, 16),
        authority,
        [to_checksum_address(t) for t in params.tokens],
    )
    return QuoteResult.of(quotes)


async def send(relay: Relay, params: SendParams) -> tuple[SendResult, bool]:
    """The submission's id, and whether it's new."""
    accepted = await relay.submit(params.submission())
    return SendResult(id=data(accepted.id)), accepted.new


def status(relay: Relay, id_: str) -> StatusResult:
    return StatusResult.of(relay.status(bytes.fromhex(id_[2:])))


def error_object(exc: BrunoError) -> ErrorObject:
    """The error object a relay error is answered with."""
    match exc:
        case InvalidSubmissionError():
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
        case _:
            return ErrorObject(Code.INTERNAL_ERROR, str(exc))
