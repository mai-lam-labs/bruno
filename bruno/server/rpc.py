"""JSON-RPC 2.0 (one request a body, its parameters by position), and its OpenRPC document
(1.3.2), served by rpc.discover. Both come from METHODS: what each method takes, returns and
fails with, and what it runs."""

import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache

import msgspec

from ..common import BrunoError
from ..relay import Relay
from .operations import (
    DESCRIPTION,
    INTERNAL_ERROR,
    TITLE,
    VERSION,
    error_object,
    quote,
    send,
    status,
    terms,
)
from .wire import (
    HASH_PATTERN,
    Code,
    ErrorObject,
    Hash,
    QuoteParams,
    QuoteResult,
    RpcRequest,
    RpcResponse,
    SendParams,
    SendResult,
    StatusResult,
    TermsResult,
)

logger = logging.getLogger(__name__)

OPENRPC_VERSION = "1.3.2"
DISCOVER = "rpc.discover"

# each code's message, as the document lists it
MESSAGES = {
    Code.INVALID_PARAMS: "Invalid params",
    Code.UNDERPAID: "Underpaid at the next block's price",
    Code.SIMULATION_FAILED: "Simulation failed",
    Code.UNSUPPORTED: "Unsupported token or payee",
    Code.NO_CAPACITY: "No capacity: the queue is full of more profitable submissions",
    Code.UNKNOWN_SUBMISSION: "Unknown submission",
}

type Params = tuple[msgspec.Raw, ...]


class _RpcError(Exception):
    def __init__(self, error: ErrorObject) -> None:
        super().__init__(error.message)
        self.error = error


def _invalid(message: str) -> _RpcError:
    return _RpcError(ErrorObject(Code.INVALID_PARAMS, f"Invalid params: {message}"))


def _param[T](params: Params, kind: type[T]) -> T:
    """The method's one parameter, decoded as `kind`."""
    if len(params) != 1:
        raise _invalid("one parameter, by position")
    try:
        return msgspec.json.decode(params[0], type=kind)
    except msgspec.ValidationError as e:
        raise _invalid(str(e)) from e


def _none(params: Params) -> None:
    if params:
        raise _invalid("none")


@dataclass(frozen=True, slots=True)
class Param:
    """A method's parameter: its name, and its type as msgspec reads it."""

    name: str
    kind: object


@dataclass(frozen=True, slots=True)
class Method:
    name: str
    summary: str
    param: Param | None
    result_name: str
    result: object
    errors: tuple[Code, ...]
    run: Callable[[Relay, Params], Awaitable[msgspec.Struct]]


async def _config(relay: Relay, params: Params) -> TermsResult:
    _none(params)
    return terms(relay)


async def _quote(relay: Relay, params: Params) -> QuoteResult:
    return await quote(relay, _param(params, QuoteParams))


async def _send(relay: Relay, params: Params) -> SendResult:
    result, _ = await send(relay, _param(params, SendParams))
    return result


async def _status(relay: Relay, params: Params) -> StatusResult:
    id_ = _param(params, str)
    if re.fullmatch(HASH_PATTERN, id_) is None:
        raise _invalid("an id is 32 bytes, as hex")
    return await status(relay, id_)


METHODS = (
    Method(
        "postpaid_config",
        "Who to pay, in which tokens, and the limits",
        None,
        "terms",
        TermsResult,
        (),
        _config,
    ),
    Method(
        "postpaid_quote",
        "What a call costs in each token",
        Param("quote", QuoteParams),
        "quotes",
        QuoteResult,
        (Code.INVALID_PARAMS, Code.UNSUPPORTED),
        _quote,
    ),
    Method(
        "postpaid_sendTransaction",
        "Take a submission",
        Param("submission", SendParams),
        "id",
        SendResult,
        (
            Code.INVALID_PARAMS,
            Code.UNDERPAID,
            Code.SIMULATION_FAILED,
            Code.UNSUPPORTED,
            Code.NO_CAPACITY,
        ),
        _send,
    ),
    Method(
        "postpaid_getStatus",
        "What's become of a submission",
        Param("id", Hash),
        "status",
        StatusResult,
        (Code.INVALID_PARAMS, Code.UNKNOWN_SUBMISSION),
        _status,
    ),
)
_BY_NAME = {m.name: m for m in METHODS}


def openrpc(servers: Sequence[Mapping[str, str]]) -> dict[str, object]:
    """The OpenRPC document, its `servers` the endpoints it describes."""
    return {**_document(), "servers": list(servers)}


@cache
def _document() -> dict[str, object]:
    """The OpenRPC document but its servers: each method's parameter and result schemas from
    msgspec, with their shared schemas under components."""
    kinds: list[object] = []
    for method in METHODS:
        if method.param is not None:
            kinds.append(method.param.kind)
        kinds.append(method.result)
    schemas, components = msgspec.json.schema_components(
        kinds, ref_template="#/components/schemas/{name}"
    )
    schema = iter(schemas)
    methods: list[dict[str, object]] = []
    for method in METHODS:
        params: list[dict[str, object]] = []
        if method.param is not None:
            params.append({"name": method.param.name, "required": True, "schema": next(schema)})
        methods.append(
            {
                "name": method.name,
                "summary": method.summary,
                "paramStructure": "by-position",
                "params": params,
                "result": {"name": method.result_name, "schema": next(schema)},
                "errors": [{"$ref": f"#/components/errors/{c.name}"} for c in method.errors],
            }
        )
    errors = {c.name: {"code": int(c), "message": m} for c, m in MESSAGES.items()}
    return {
        "openrpc": OPENRPC_VERSION,
        "info": {"title": TITLE, "description": DESCRIPTION, "version": VERSION},
        "methods": methods,
        "components": {"schemas": components, "errors": errors},
    }


async def answer(relay: Relay, body: bytes, discovered: Mapping[str, str]) -> RpcResponse:
    """A JSON-RPC request's response; rpc.discover's names `discovered` as its server."""
    try:
        request = msgspec.json.decode(body, type=RpcRequest)
    except msgspec.ValidationError as e:
        return RpcResponse(None, error=ErrorObject(Code.INVALID_REQUEST, f"Invalid Request: {e}"))
    except msgspec.DecodeError as e:
        return RpcResponse(None, error=ErrorObject(Code.PARSE_ERROR, f"Parse error: {e}"))
    if request.method == DISCOVER:
        return RpcResponse(request.id, result=openrpc([discovered]))
    method = _BY_NAME.get(request.method)
    if method is None:
        error = ErrorObject(Code.METHOD_NOT_FOUND, f"Method not found: {request.method}")
        return RpcResponse(request.id, error=error)
    try:
        return RpcResponse(request.id, result=await method.run(relay, request.params))
    except _RpcError as e:
        return RpcResponse(request.id, error=e.error)
    except BrunoError as e:
        return RpcResponse(request.id, error=error_object(e))
    except Exception:
        logger.exception("internal error")
        return RpcResponse(request.id, error=INTERNAL_ERROR)
