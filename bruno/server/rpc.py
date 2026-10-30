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
from ..userops import ENTRY_POINTS
from .operations import (
    DESCRIPTION,
    INTERNAL_ERROR,
    QUOTE_EXAMPLE,
    TITLE,
    VERSION,
    error_object,
    estimate_user_operation,
    quote,
    quote_user_operation,
    send,
    send_user_operation,
    status,
    terms,
    user_operation_gas_price,
    user_operation_receipt,
)
from .wire import (
    HASH_PATTERN,
    Address,
    Code,
    ErrorObject,
    GasEstimateResult,
    GasPriceResult,
    Hash,
    Quantity,
    QuoteParams,
    QuoteResult,
    RpcRequest,
    RpcResponse,
    SendParams,
    SendResult,
    StatusResult,
    TermsResult,
    UserOperationJson,
    UserOperationParams,
    UserOperationReceiptJson,
    quantity,
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
    """A method's parameter: its name, its type as msgspec reads it, and the value the
    document's example gives it, if it has one."""

    name: str
    kind: object
    example: msgspec.Struct | None = None


@dataclass(frozen=True, slots=True)
class Method:
    """A method: its name, summary, parameters (by position), result, the errors it may answer,
    and what it runs."""

    name: str
    summary: str
    params: tuple[Param, ...]
    result_name: str
    result: object
    errors: tuple[Code, ...]
    run: Callable[[Relay, Params], Awaitable[object]]


async def _config(relay: Relay, params: Params) -> TermsResult:
    _none(params)
    return terms(relay)


async def _quote(relay: Relay, params: Params) -> QuoteResult:
    return await quote(relay, _param(params, QuoteParams))


async def _send(relay: Relay, params: Params) -> SendResult:
    result, _ = await send(relay, _param(params, SendParams))
    return result


def _user_operation(params: Params) -> UserOperationParams:
    """ERC-7769's two parameters: the op, and its EntryPoint."""
    if len(params) != 2:
        raise _invalid("two parameters, by position: the user operation and its EntryPoint")
    try:
        op = msgspec.json.decode(params[0], type=UserOperationJson)
        entry_point = msgspec.json.decode(params[1], type=Address)
    except msgspec.ValidationError as e:
        raise _invalid(str(e)) from e
    return UserOperationParams(op, entry_point)


async def _estimate(relay: Relay, params: Params) -> GasEstimateResult:
    return await estimate_user_operation(relay, _user_operation(params))


async def _quote_user_operation(relay: Relay, params: Params) -> QuoteResult:
    return await quote_user_operation(relay, _user_operation(params))


async def _send_user_operation(relay: Relay, params: Params) -> str:
    return await send_user_operation(relay, _user_operation(params))


async def _user_operation_receipt(relay: Relay, params: Params) -> UserOperationReceiptJson | None:
    return await user_operation_receipt(relay, _param(params, str))


async def _gas_price(relay: Relay, params: Params) -> GasPriceResult:
    _none(params)
    return await user_operation_gas_price(relay)


async def _supported_entry_points(_relay: Relay, params: Params) -> tuple[str, ...]:
    _none(params)
    return tuple(e.address for e in ENTRY_POINTS)


async def _chain_id(relay: Relay, params: Params) -> str:
    _none(params)
    return quantity(relay.chain.chain_id)


async def _status(relay: Relay, params: Params) -> StatusResult:
    id_ = _param(params, str)
    if re.fullmatch(HASH_PATTERN, id_) is None:
        raise _invalid("an id is 32 bytes, as hex")
    return await status(relay, id_)


METHODS = (
    Method(
        "postpaid_config",
        "Who to pay, in which tokens, and the limits",
        (),
        "terms",
        TermsResult,
        (),
        _config,
    ),
    Method(
        "postpaid_quote",
        "What a call costs in each token",
        (Param("quote", QuoteParams, QUOTE_EXAMPLE),),
        "quotes",
        QuoteResult,
        (Code.INVALID_PARAMS, Code.UNSUPPORTED),
        _quote,
    ),
    Method(
        "postpaid_sendTransaction",
        "Take a submission",
        (Param("submission", SendParams),),
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
        (Param("id", Hash),),
        "status",
        StatusResult,
        (Code.INVALID_PARAMS, Code.UNKNOWN_SUBMISSION),
        _status,
    ),
    Method(
        "eth_estimateUserOperationGas",
        "A user operation's gas limits (ERC-7769), paying by its fees or by its calls",
        (Param("userOperation", UserOperationJson), Param("entryPoint", Address)),
        "gas",
        GasEstimateResult,
        (Code.INVALID_PARAMS, Code.SIMULATION_FAILED),
        _estimate,
    ),
    Method(
        "postpaid_quoteUserOperation",
        "What a user operation paying by its calls is to pay, in each token, before it's signed",
        (Param("userOperation", UserOperationJson), Param("entryPoint", Address)),
        "quotes",
        QuoteResult,
        (Code.INVALID_PARAMS, Code.SIMULATION_FAILED, Code.UNSUPPORTED),
        _quote_user_operation,
    ),
    Method(
        "eth_sendUserOperation",
        "Take a user operation (ERC-7769), paying by its fees or by its calls: its hash",
        (Param("userOperation", UserOperationJson), Param("entryPoint", Address)),
        "userOpHash",
        Hash,
        (
            Code.INVALID_PARAMS,
            Code.UNDERPAID,
            Code.SIMULATION_FAILED,
            Code.UNSUPPORTED,
            Code.NO_CAPACITY,
        ),
        _send_user_operation,
    ),
    Method(
        "eth_getUserOperationReceipt",
        "A user operation's receipt (ERC-7769): null until it's included",
        (Param("userOpHash", Hash),),
        "receipt",
        UserOperationReceiptJson | None,
        (Code.INVALID_PARAMS,),
        _user_operation_receipt,
    ),
    Method(
        "pimlico_getUserOperationGasPrice",
        "The price a user operation paying by its fees is asked, as both its fees",
        (),
        "gasPrice",
        GasPriceResult,
        (Code.UNSUPPORTED,),
        _gas_price,
    ),
    Method(
        "zd_getUserOperationGasPrice",
        "The price a user operation paying by its fees is asked, as both its fees",
        (),
        "gasPrice",
        GasPriceResult,
        (Code.UNSUPPORTED,),
        _gas_price,
    ),
    Method(
        "eth_supportedEntryPoints",
        "The EntryPoints taken (ERC-7769): v0.7's",
        (),
        "entryPoints",
        tuple[Address, ...],
        (),
        _supported_entry_points,
    ),
    Method(
        "eth_chainId",
        "The chain's id",
        (),
        "chainId",
        Quantity,
        (),
        _chain_id,
    ),
)
_BY_NAME = {m.name: m for m in METHODS}


def openrpc(servers: Sequence[Mapping[str, str]]) -> dict[str, object]:
    """The OpenRPC document, its `servers` the endpoints it describes."""
    return {**_document(), "servers": list(servers)}


@cache
def _document() -> dict[str, object]:
    """The OpenRPC document but its servers: each method's parameter and result schemas from
    msgspec, with their shared schemas under components, and an example request for each
    method that works on any chain (one whose parameters, if any, each have an example)."""
    kinds: list[object] = []
    for method in METHODS:
        kinds += [param.kind for param in method.params]
        kinds.append(method.result)
    schemas, components = msgspec.json.schema_components(
        kinds, ref_template="#/components/schemas/{name}"
    )
    schema = iter(schemas)
    methods: list[dict[str, object]] = []
    for method in METHODS:
        params = [
            {"name": param.name, "required": True, "schema": next(schema)}
            for param in method.params
        ]
        examples: list[dict[str, object]] = []
        if all(param.example is not None for param in method.params):
            values = [
                {"name": param.name, "value": msgspec.to_builtins(param.example)}
                for param in method.params
            ]
            examples.append({"name": method.name, "params": values})
        methods.append(
            {
                "name": method.name,
                "summary": method.summary,
                "paramStructure": "by-position",
                "params": params,
                "result": {"name": method.result_name, "schema": next(schema)},
                "errors": [{"$ref": f"#/components/errors/{c.name}"} for c in method.errors],
                **({"examples": examples} if examples else {}),
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
