"""The relay's HTTP interface: the same four operations over REST and JSON-RPC, one base
path a chain, /{chain}: the chain's network name ("sepolia") or its chain id.

    REST                              JSON-RPC, POST /{chain}
    GET  /{chain}/v1/config           postpaid_config
    GET  /{chain}/v1/quote            postpaid_quote
    POST /{chain}/v1/submissions      postpaid_sendTransaction
    GET  /{chain}/v1/submissions/{id} postpaid_getStatus

A REST error's body is {"error": <JSON-RPC's error object>}, its HTTP status chosen by the
object's code. The root, /, lists the chains served and the documents: JSON, or a page for a
browser. OpenAPI's document is at /openapi.json, OpenRPC's at /openrpc.json and by
rpc.discover. Any origin may call it (CORS)."""

from collections.abc import Mapping
from typing import Annotated, Any

import msgspec
from litestar import Litestar, MediaType, Request, Response, get, post
from litestar.config.cors import CORSConfig
from litestar.exceptions import NotFoundException, ValidationException
from litestar.openapi import OpenAPIConfig
from litestar.params import JSONBody, PathParameter, QueryParameter, RawBody
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_202_ACCEPTED,
    HTTP_400_BAD_REQUEST,
    HTTP_404_NOT_FOUND,
    HTTP_422_UNPROCESSABLE_ENTITY,
    HTTP_500_INTERNAL_SERVER_ERROR,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from ..common import BrunoError
from ..relay import Relay
from .front import CHAIN_PATTERN, chain_id_of, chain_path, index, page, server
from .operations import DESCRIPTION, TITLE, VERSION, error_object, quote, send, status, terms
from .rpc import answer, openrpc
from .wire import (
    ADDRESS_PATTERN,
    HASH_PATTERN,
    QUANTITY_PATTERN,
    Code,
    ErrorBody,
    ErrorObject,
    IndexResult,
    QuoteParams,
    QuoteResult,
    RpcResponse,
    SendParams,
    SendResult,
    StatusResult,
    TermsResult,
)

_HTTP_STATUS = {
    Code.INVALID_PARAMS: HTTP_400_BAD_REQUEST,
    Code.UNDERPAID: HTTP_422_UNPROCESSABLE_ENTITY,
    Code.SIMULATION_FAILED: HTTP_422_UNPROCESSABLE_ENTITY,
    Code.UNSUPPORTED: HTTP_422_UNPROCESSABLE_ENTITY,
    Code.NO_CAPACITY: HTTP_503_SERVICE_UNAVAILABLE,
    Code.UNKNOWN_SUBMISSION: HTTP_404_NOT_FOUND,
}


def _error_response(exc: BrunoError) -> Response[ErrorBody]:
    error = error_object(exc)
    status_code = _HTTP_STATUS.get(error.code, HTTP_500_INTERNAL_SERVER_ERROR)
    return Response(ErrorBody(error), status_code=status_code)


def _on_error(_: Request[Any, Any, Any], exc: Exception) -> Response[ErrorBody]:
    """A relay error's response; nothing at the path, or a chain not served, pointing to the
    root; or a request Litestar found invalid."""
    if isinstance(exc, BrunoError):
        return _error_response(exc)
    if isinstance(exc, NotFoundException):
        message = "not found" if exc.detail == "Not Found" else exc.detail
        error = ErrorObject(Code.METHOD_NOT_FOUND, f"{message}: see /")
        return Response(ErrorBody(error), status_code=HTTP_404_NOT_FOUND)
    detail = exc.detail if isinstance(exc, ValidationException) else str(exc)
    extra = exc.extra if isinstance(exc, ValidationException) else None
    error = ErrorObject(Code.INVALID_PARAMS, detail, extra)
    return Response(ErrorBody(error), status_code=HTTP_400_BAD_REQUEST)


def _origin(request: Request[Any, Any, Any]) -> str:
    """Where the request was sent: its scheme and host, and any root path."""
    return str(request.base_url).rstrip("/")


def create_app(relays: Mapping[int, Relay]) -> Litestar:
    """The interface to `relays`, by chain id."""

    def relay_for(chain: str) -> Relay:
        chain_id = chain_id_of(chain)
        relay = None if chain_id is None else relays.get(chain_id)
        if relay is None:
            raise NotFoundException(f"chain {chain} isn't served here")
        return relay

    @get("/", include_in_schema=False)
    async def get_index(request: Request[Any, Any, Any]) -> Response[IndexResult | str]:
        """The chains served and the documents: a page for a browser, else JSON."""
        root = index(_origin(request), {c: r.payee for c, r in relays.items()})
        if "text/html" in request.headers.get("accept", ""):
            return Response(page(_origin(request), root), media_type=MediaType.HTML)
        return Response(root)

    @get("/{chain:str}/v1/config", summary="postpaid_config")
    async def get_config(
        chain: Annotated[str, PathParameter(pattern=CHAIN_PATTERN)],
    ) -> TermsResult:
        """Who to pay, in which tokens, and the limits."""
        return terms(relay_for(chain))

    @get("/{chain:str}/v1/quote", summary="postpaid_quote")
    async def get_quote(
        chain: Annotated[str, PathParameter(pattern=CHAIN_PATTERN)],
        gas: Annotated[str, QueryParameter(pattern=QUANTITY_PATTERN)],
        call_bytes: Annotated[str, QueryParameter(name="callBytes", pattern=QUANTITY_PATTERN)],
        tokens: Annotated[list[str], QueryParameter(min_items=1)],
        authority: Annotated[str | None, QueryParameter(pattern=ADDRESS_PATTERN)] = None,
    ) -> QuoteResult:
        """What a call costs in each token: `gas` its own gas, `callBytes` its data's
        length, `authority` the account signing an authorization, if any. `tokens` repeats
        once a token."""
        params = msgspec.convert(
            {"gas": gas, "callBytes": call_bytes, "tokens": tokens, "authority": authority},
            QuoteParams,
        )
        return await quote(relay_for(chain), params)

    @post("/{chain:str}/v1/submissions", summary="postpaid_sendTransaction")
    async def post_submission(
        chain: Annotated[str, PathParameter(pattern=CHAIN_PATTERN)], data: JSONBody[SendParams]
    ) -> Response[SendResult]:
        """Take a submission: 202 if it's new, 200 if it was taken already."""
        relay = relay_for(chain)
        result, new = await send(relay, data)
        return Response(
            result,
            status_code=HTTP_202_ACCEPTED if new else HTTP_200_OK,
            headers={"Location": f"/{chain_path(relay.chain.chain_id)}/v1/submissions/{result.id}"},
        )

    @get("/{chain:str}/v1/submissions/{submission_id:str}", summary="postpaid_getStatus")
    async def get_submission(
        chain: Annotated[str, PathParameter(pattern=CHAIN_PATTERN)],
        submission_id: Annotated[str, PathParameter(pattern=HASH_PATTERN)],
    ) -> StatusResult:
        """What's become of a submission."""
        return status(relay_for(chain), submission_id)

    @get("/openrpc.json", summary="OpenRPC")
    async def get_openrpc(request: Request[Any, Any, Any]) -> dict[str, object]:
        """The JSON-RPC methods' OpenRPC document, a server each chain served; rpc.discover
        answers it with the one chain's."""
        return openrpc([server(_origin(request), chain_id) for chain_id in sorted(relays)])

    @get("/openapi.json", summary="OpenAPI")
    async def get_openapi(request: Request[Any, Any, Any]) -> dict[str, object]:
        """The REST operations' OpenAPI document."""
        return {**request.app.openapi_schema.to_schema(), "servers": [{"url": _origin(request)}]}

    @post(
        "/{chain:str}",
        status_code=HTTP_200_OK,
        media_type=MediaType.JSON,
        summary="JSON-RPC",
    )
    async def json_rpc(
        request: Request[Any, Any, Any],
        chain: Annotated[str, PathParameter(pattern=CHAIN_PATTERN)],
        body: RawBody,
    ) -> RpcResponse:
        """postpaid_config, postpaid_quote, postpaid_sendTransaction and postpaid_getStatus:
        each with the REST operation's request (one parameter) and response. rpc.discover
        answers the OpenRPC document."""
        relay = relay_for(chain)
        return await answer(relay, body, server(_origin(request), relay.chain.chain_id))

    return Litestar(
        route_handlers=[
            get_index,
            get_config,
            get_quote,
            post_submission,
            get_submission,
            get_openrpc,
            get_openapi,
            json_rpc,
        ],
        exception_handlers={
            BrunoError: _on_error,
            ValidationException: _on_error,
            NotFoundException: _on_error,
        },
        cors_config=CORSConfig(
            allow_origins=["*"],
            allow_methods=["GET", "POST"],
            allow_headers=["content-type"],
            expose_headers=["Location"],
        ),
        openapi_config=OpenAPIConfig(title=TITLE, version=VERSION, description=DESCRIPTION),
    )
