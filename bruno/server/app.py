"""The relay's HTTP interface: the same four operations over REST and JSON-RPC, one base
path a chain, /{chainId}.

    REST                                JSON-RPC, POST /{chainId}
    GET  /{chainId}/v1/config           postpaid_config
    GET  /{chainId}/v1/quote            postpaid_quote
    POST /{chainId}/v1/submissions      postpaid_sendTransaction
    GET  /{chainId}/v1/submissions/{id} postpaid_getStatus

A REST error's body is {"error": <JSON-RPC's error object>}, its HTTP status chosen by the
object's code. OpenAPI's document is at /schema, OpenRPC's at /openrpc.json and by
rpc.discover."""

from collections.abc import Mapping
from typing import Annotated, Any

import msgspec
from litestar import Litestar, MediaType, Request, Response, get, post
from litestar.exceptions import NotFoundException, ValidationException
from litestar.openapi import OpenAPIConfig
from litestar.params import FromPath, JSONBody, PathParameter, QueryParameter, RawBody
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
from .operations import DESCRIPTION, TITLE, VERSION, error_object, quote, send, status, terms
from .rpc import answer, openrpc
from .wire import (
    ADDRESS_PATTERN,
    HASH_PATTERN,
    QUANTITY_PATTERN,
    Code,
    ErrorBody,
    ErrorObject,
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
    """A relay error's response, or a request Litestar found invalid."""
    if isinstance(exc, BrunoError):
        return _error_response(exc)
    detail = exc.detail if isinstance(exc, ValidationException) else str(exc)
    extra = exc.extra if isinstance(exc, ValidationException) else None
    error = ErrorObject(Code.INVALID_PARAMS, detail, extra)
    return Response(ErrorBody(error), status_code=HTTP_400_BAD_REQUEST)


def create_app(relays: Mapping[int, Relay]) -> Litestar:
    """The interface to `relays`, by chain id."""

    def relay_for(chain_id: int) -> Relay:
        relay = relays.get(chain_id)
        if relay is None:
            raise NotFoundException(f"chain {chain_id} isn't served here")
        return relay

    @get("/{chain_id:int}/v1/config", summary="postpaid_config")
    async def get_config(chain_id: FromPath[int]) -> TermsResult:
        """Who to pay, in which tokens, and the limits."""
        return terms(relay_for(chain_id))

    @get("/{chain_id:int}/v1/quote", summary="postpaid_quote")
    async def get_quote(
        chain_id: FromPath[int],
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
        return await quote(relay_for(chain_id), params)

    @post("/{chain_id:int}/v1/submissions", summary="postpaid_sendTransaction")
    async def post_submission(
        chain_id: FromPath[int], data: JSONBody[SendParams]
    ) -> Response[SendResult]:
        """Take a submission: 202 if it's new, 200 if it was taken already."""
        result, new = await send(relay_for(chain_id), data)
        return Response(
            result,
            status_code=HTTP_202_ACCEPTED if new else HTTP_200_OK,
            headers={"Location": f"/{chain_id}/v1/submissions/{result.id}"},
        )

    @get("/{chain_id:int}/v1/submissions/{submission_id:str}", summary="postpaid_getStatus")
    async def get_submission(
        chain_id: FromPath[int],
        submission_id: Annotated[str, PathParameter(pattern=HASH_PATTERN)],
    ) -> StatusResult:
        """What's become of a submission."""
        return status(relay_for(chain_id), submission_id)

    @get("/openrpc.json", summary="OpenRPC")
    async def get_openrpc() -> dict[str, object]:
        """The JSON-RPC methods' OpenRPC document, as rpc.discover answers."""
        return openrpc()

    @post(
        "/{chain_id:int}",
        status_code=HTTP_200_OK,
        media_type=MediaType.JSON,
        summary="JSON-RPC",
    )
    async def json_rpc(chain_id: FromPath[int], body: RawBody) -> RpcResponse:
        """postpaid_config, postpaid_quote, postpaid_sendTransaction and postpaid_getStatus:
        each with the REST operation's request (one parameter) and response. rpc.discover
        answers the OpenRPC document."""
        return await answer(relay_for(chain_id), body)

    return Litestar(
        route_handlers=[
            get_config,
            get_quote,
            post_submission,
            get_submission,
            get_openrpc,
            json_rpc,
        ],
        exception_handlers={BrunoError: _on_error, ValidationException: _on_error},
        openapi_config=OpenAPIConfig(title=TITLE, version=VERSION, description=DESCRIPTION),
    )
