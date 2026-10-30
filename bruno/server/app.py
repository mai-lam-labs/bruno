"""The relay's HTTP interface: the same four operations over REST and JSON-RPC, one base
path a chain, /{chain}: the chain's network name ("sepolia") or its chain id.

    REST                              JSON-RPC, POST /{chain}
    GET  /{chain}/v1/config           postpaid_config
    GET  /{chain}/v1/quote            postpaid_quote
    POST /{chain}/v1/submissions      postpaid_sendTransaction
    GET  /{chain}/v1/submissions/{id} postpaid_getStatus

and, REST only, a call paid over x402 (bruno.server.x402), on a chain that takes it:

    POST /{chain}/v1/x402             402 with what to pay; with PAYMENT-SIGNATURE, held
                                      until its transaction is included (200) or fails (402)

A REST error's body is {"error": <JSON-RPC's error object>}, its HTTP status chosen by the
object's code. The root, /, lists the chains served and the documents: JSON, or a page for a
browser. OpenAPI's document is at /openapi.json, OpenRPC's at /openrpc.json and by
rpc.discover. Any origin may call it (CORS)."""

import logging
from collections.abc import Mapping
from typing import Annotated, Any

import httpx
import msgspec
from eth_utils.address import to_checksum_address
from litestar import Litestar, MediaType, Request, Response, get, post
from litestar.config.cors import CORSConfig
from litestar.exceptions import HTTPException, NotFoundException, ValidationException
from litestar.openapi import OpenAPIConfig
from litestar.openapi.datastructures import ResponseSpec
from litestar.openapi.spec import Example
from litestar.params import JSONBody, PathParameter, QueryParameter, RawBody
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_202_ACCEPTED,
    HTTP_400_BAD_REQUEST,
    HTTP_404_NOT_FOUND,
    HTTP_413_REQUEST_ENTITY_TOO_LARGE,
    HTTP_422_UNPROCESSABLE_ENTITY,
    HTTP_500_INTERNAL_SERVER_ERROR,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from ..accounts import derive
from ..chain import ChainError
from ..common import BrunoError
from ..net.jsonrpc import JsonRpcError
from ..plan import SimulationFailedError
from ..relay import NoCapacityError, Relay, UnderpaidError, UnsupportedError
from ..submission import Submission
from ..tracked import Stage, Status
from ..treasury import Treasury
from .front import CHAIN_PATTERN, ChainShown, chain_id_of, chain_path, index, page, server
from .operations import (
    DESCRIPTION,
    INTERNAL_ERROR,
    QUOTE_EXAMPLE,
    TITLE,
    VERSION,
    error_object,
    estimate_user_operation,
    node_failed,
    quote,
    quote_user_operation,
    send,
    send_user_operation,
    status,
    terms,
    user_operation_receipt,
)
from .rpc import answer, openrpc
from .wire import (
    ADDRESS_PATTERN,
    DATA_PATTERN,
    HASH_PATTERN,
    QUANTITY_PATTERN,
    Code,
    ErrorBody,
    ErrorObject,
    GasEstimateResult,
    IndexResult,
    PaymentRequired,
    QuoteParams,
    QuoteResult,
    RpcResponse,
    SendParams,
    SendResult,
    SettleResponse,
    StatusResult,
    TermsResult,
    UserOperationParams,
    UserOperationReceiptJson,
    UserOperationSent,
    WellKnownX402,
    X402Result,
    data,
)
from .x402 import (
    INVALID_REQUIREMENTS,
    TRANSACTION_STATE,
    VALUE_MISMATCH,
    MalformedPaymentError,
    PaymentRefusedError,
    challenge,
    encoded,
    openapi_operation,
    params_of,
    payload_of,
    refusal,
    refused,
    result,
    settled,
    take,
    x402_url,
)

logger = logging.getLogger(__name__)

_PAYMENT_REQUIRED = 402
# the largest request body read: a submission's call data, as hex, is most of it
MAX_BODY_BYTES = 256 * 1024

_HTTP_STATUS = {
    Code.INVALID_PARAMS: HTTP_400_BAD_REQUEST,
    Code.UNDERPAID: HTTP_422_UNPROCESSABLE_ENTITY,
    Code.SIMULATION_FAILED: HTTP_422_UNPROCESSABLE_ENTITY,
    Code.UNSUPPORTED: HTTP_422_UNPROCESSABLE_ENTITY,
    Code.NO_CAPACITY: HTTP_503_SERVICE_UNAVAILABLE,
    Code.UNKNOWN_SUBMISSION: HTTP_404_NOT_FOUND,
    Code.UNAVAILABLE: HTTP_503_SERVICE_UNAVAILABLE,
}


# what each REST error status means (SPEC.md, Interface), as an operation's OpenAPI document has
# it: its body the relay's error object, in place of Litestar's own validation error
_ERRORS = {
    HTTP_400_BAD_REQUEST: "Invalid params",
    HTTP_404_NOT_FOUND: "No such path, the chain isn't served, or no such submission",
    HTTP_422_UNPROCESSABLE_ENTITY: "Underpaid, simulation failed, or an unsupported token or payee",
    HTTP_503_SERVICE_UNAVAILABLE: "No capacity, or the chain can't be served now",
}


def _errors(*statuses: int) -> dict[int, ResponseSpec]:
    """The OpenAPI responses of an operation answering `statuses` as errors."""
    return {
        s: ResponseSpec(ErrorBody, description=_ERRORS[s], generate_examples=False)
        for s in statuses
    }


def _error_response(exc: BrunoError) -> Response[ErrorBody]:
    error = error_object(exc)
    status_code = _HTTP_STATUS.get(error.code, HTTP_500_INTERNAL_SERVER_ERROR)
    return Response(ErrorBody(error), status_code=status_code)


def _on_error(_: Request[Any, Any, Any], exc: Exception) -> Response[ErrorBody]:
    """A relay error's response; nothing at the path, or a chain not served, pointing to the
    root; or a request Litestar found invalid or couldn't read."""
    if isinstance(exc, BrunoError):
        return _error_response(exc)
    if isinstance(exc, NotFoundException):
        message = "not found" if exc.detail == "Not Found" else exc.detail
        error = ErrorObject(Code.METHOD_NOT_FOUND, f"{message}: see /")
        return Response(ErrorBody(error), status_code=HTTP_404_NOT_FOUND)
    detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
    extra = exc.extra if isinstance(exc, HTTPException) else None
    error = ErrorObject(Code.INVALID_PARAMS, detail, extra)
    return Response(ErrorBody(error), status_code=HTTP_400_BAD_REQUEST)


def _on_too_large(_: Request[Any, Any, Any], __: Exception) -> Response[ErrorBody]:
    """A body over MAX_BODY_BYTES: the client's mistake, as any malformed request is."""
    error = ErrorObject(Code.INVALID_PARAMS, "body: too large", "body")
    return Response(ErrorBody(error), status_code=HTTP_400_BAD_REQUEST)


def _on_node_failure(_: Request[Any, Any, Any], exc: Exception) -> Response[ErrorBody]:
    """A node failing a request: unavailable for now (503), to try again; what failed only
    logged."""
    return Response(ErrorBody(node_failed(exc)), status_code=HTTP_503_SERVICE_UNAVAILABLE)


def _on_internal(_: Request[Any, Any, Any], exc: Exception) -> Response[ErrorBody]:
    """Anything else: an internal error, what failed only logged."""
    logger.error("internal error", exc_info=exc)
    return Response(ErrorBody(INTERNAL_ERROR), status_code=HTTP_500_INTERNAL_SERVER_ERROR)


def _payment_required(
    required: PaymentRequired, response: SettleResponse | None = None
) -> Response[PaymentRequired | X402Result | ErrorBody]:
    """402: what to pay, in PAYMENT-REQUIRED and the body; and why a payment wasn't taken, if
    one was refused."""
    headers = {"PAYMENT-REQUIRED": encoded(required)}
    if response is not None:
        headers["PAYMENT-RESPONSE"] = encoded(response)
    return Response(required, status_code=_PAYMENT_REQUIRED, headers=headers)


def _origin(request: Request[Any, Any, Any]) -> str:
    """Where the request was sent: its scheme and host, and any root path."""
    return str(request.base_url).rstrip("/")


def create_app(
    relays: Mapping[int, Relay], treasuries: Mapping[int, Treasury] | None = None
) -> Litestar:
    """The interface to `relays`, by chain id; the root shows `treasuries`' last runs."""
    treasuries = treasuries or {}

    def shown(chain_id: int, relay: Relay) -> ChainShown:
        """What the root shows of the chain: nothing read from it."""
        treasury = treasuries.get(chain_id)
        return ChainShown(
            payee=relay.payee,
            float_account=relay.float_account.address,
            accounts=tuple(
                derive(relay.float_account, i).address for i in range(relay.config.accounts)
            ),
            status=relay.accounts_status,
            snapshot=None if treasury is None else treasury.snapshot,
            x402_tokens=tuple(relay.domains),
            problems=relay.problems,
        )

    def relay_for(chain: str) -> Relay:
        chain_id = chain_id_of(chain)
        relay = None if chain_id is None else relays.get(chain_id)
        if relay is None:
            raise NotFoundException(f"chain {chain} isn't served here")
        return relay

    @get("/", include_in_schema=False)
    async def get_index(request: Request[Any, Any, Any]) -> Response[IndexResult | str]:
        """The chains served and the documents: a page for a browser, else JSON."""
        root = index(_origin(request), {c: shown(c, r) for c, r in relays.items()})
        if "text/html" in request.headers.get("accept", ""):
            return Response(page(_origin(request), root), media_type=MediaType.HTML)
        return Response(root)

    @get(
        "/{chain:str}/v1/config",
        summary="postpaid_config",
        responses=_errors(HTTP_400_BAD_REQUEST, HTTP_404_NOT_FOUND, HTTP_503_SERVICE_UNAVAILABLE),
    )
    async def get_config(
        chain: Annotated[str, PathParameter(pattern=CHAIN_PATTERN)],
    ) -> TermsResult:
        """Who to pay, in which tokens, and the limits."""
        return terms(relay_for(chain))

    @get(
        "/{chain:str}/v1/quote",
        summary="postpaid_quote",
        responses=_errors(
            HTTP_400_BAD_REQUEST,
            HTTP_404_NOT_FOUND,
            HTTP_422_UNPROCESSABLE_ENTITY,
            HTTP_503_SERVICE_UNAVAILABLE,
        ),
    )
    async def get_quote(
        chain: Annotated[str, PathParameter(pattern=CHAIN_PATTERN)],
        gas: Annotated[
            str,
            QueryParameter(pattern=QUANTITY_PATTERN, examples=[Example(value=QUOTE_EXAMPLE.gas)]),
        ],
        call_bytes: Annotated[
            str,
            QueryParameter(
                name="callBytes",
                pattern=QUANTITY_PATTERN,
                examples=[Example(value=QUOTE_EXAMPLE.call_bytes)],
            ),
        ],
        tokens: Annotated[
            list[str],
            QueryParameter(min_items=1, examples=[Example(value=list(QUOTE_EXAMPLE.tokens))]),
        ],
        authority: Annotated[str | None, QueryParameter(pattern=ADDRESS_PATTERN)] = None,
        payer: Annotated[
            str | None,
            QueryParameter(
                pattern=ADDRESS_PATTERN,
                description="The account a signed transfer pays from (its `from`): given, the "
                "payment is that transfer; not given, the call pays for itself",
            ),
        ] = None,
        verify_gas: Annotated[
            str | None,
            QueryParameter(
                name="verifyGas",
                pattern=QUANTITY_PATTERN,
                description="The gas the payer's signature check takes, if known: only with "
                "`payer`",
            ),
        ] = None,
        account: Annotated[str | None, QueryParameter(pattern=ADDRESS_PATTERN)] = None,
        factory: Annotated[str | None, QueryParameter(pattern=ADDRESS_PATTERN)] = None,
        factory_data: Annotated[
            str | None, QueryParameter(name="factoryData", pattern=DATA_PATTERN)
        ] = None,
    ) -> QuoteResult:
        """What a call costs in each token: `gas` its own gas, `callBytes` its data's
        length, `authority` the account signing an authorization, if any. `tokens` repeats
        once a token. `payer`: the account a signed transfer pays from, if one pays (without
        it, the call pays for itself); `verifyGas` the gas its signature check takes, if known,
        only with `payer`. `account`, `factory` and `factoryData`: an account to deploy
        first."""
        deployment = (account, factory, factory_data)
        if any(f is not None for f in deployment) and None in deployment:
            raise ValidationException("a deployment is account, factory and factoryData")
        try:
            params = msgspec.convert(
                {
                    "gas": gas,
                    "callBytes": call_bytes,
                    "tokens": tokens,
                    "authority": authority,
                    "payer": payer,
                    "verifyGas": verify_gas,
                    "deployment": None
                    if account is None
                    else {"account": account, "factory": factory, "factoryData": factory_data},
                },
                QuoteParams,
            )
        except msgspec.ValidationError as e:
            raise ValidationException(str(e)) from e
        return await quote(relay_for(chain), params)

    @post(
        "/{chain:str}/v1/submissions",
        summary="postpaid_sendTransaction",
        status_code=HTTP_202_ACCEPTED,
        responses={
            HTTP_200_OK: ResponseSpec(
                SendResult, description="Taken already", generate_examples=False
            ),
            **_errors(
                HTTP_400_BAD_REQUEST,
                HTTP_404_NOT_FOUND,
                HTTP_422_UNPROCESSABLE_ENTITY,
                HTTP_503_SERVICE_UNAVAILABLE,
            ),
        },
    )
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

    @post(
        "/{chain:str}/v1/user-operations/gas",
        summary="eth_estimateUserOperationGas",
        status_code=HTTP_200_OK,
        responses=_errors(
            HTTP_400_BAD_REQUEST,
            HTTP_404_NOT_FOUND,
            HTTP_422_UNPROCESSABLE_ENTITY,
            HTTP_503_SERVICE_UNAVAILABLE,
        ),
    )
    async def post_user_operation_gas(
        chain: Annotated[str, PathParameter(pattern=CHAIN_PATTERN)],
        data: JSONBody[UserOperationParams],
    ) -> GasEstimateResult:
        """A user operation's gas limits (ERC-7769's eth_estimateUserOperationGas), its fees
        zero and its own calls paying the relay."""
        return await estimate_user_operation(relay_for(chain), data)

    @post(
        "/{chain:str}/v1/user-operations/quote",
        summary="postpaid_quoteUserOperation",
        status_code=HTTP_200_OK,
        responses=_errors(
            HTTP_400_BAD_REQUEST,
            HTTP_404_NOT_FOUND,
            HTTP_422_UNPROCESSABLE_ENTITY,
            HTTP_503_SERVICE_UNAVAILABLE,
        ),
    )
    async def post_user_operation_quote(
        chain: Annotated[str, PathParameter(pattern=CHAIN_PATTERN)],
        data: JSONBody[UserOperationParams],
    ) -> QuoteResult:
        """What a user operation, its fees zero and its own calls paying the relay, is to pay
        in each token: its payment a placeholder, before it's signed."""
        return await quote_user_operation(relay_for(chain), data)

    @post(
        "/{chain:str}/v1/user-operations",
        summary="eth_sendUserOperation",
        status_code=HTTP_200_OK,
        responses=_errors(
            HTTP_400_BAD_REQUEST,
            HTTP_404_NOT_FOUND,
            HTTP_422_UNPROCESSABLE_ENTITY,
            HTTP_503_SERVICE_UNAVAILABLE,
        ),
    )
    async def post_user_operation(
        chain: Annotated[str, PathParameter(pattern=CHAIN_PATTERN)],
        data: JSONBody[UserOperationParams],
    ) -> UserOperationSent:
        """Take a user operation (ERC-7769's eth_sendUserOperation), its fees zero and its own
        calls paying the relay: its hash."""
        return UserOperationSent(await send_user_operation(relay_for(chain), data))

    @get(
        "/{chain:str}/v1/user-operations/{op_hash:str}",
        summary="eth_getUserOperationReceipt",
        responses=_errors(HTTP_400_BAD_REQUEST, HTTP_404_NOT_FOUND, HTTP_503_SERVICE_UNAVAILABLE),
    )
    async def get_user_operation(
        chain: Annotated[str, PathParameter(pattern=CHAIN_PATTERN)],
        op_hash: Annotated[str, PathParameter(pattern=HASH_PATTERN)],
    ) -> UserOperationReceiptJson | None:
        """A user operation's receipt (ERC-7769's eth_getUserOperationReceiptJson): null until it's
        included."""
        return await user_operation_receipt(relay_for(chain), op_hash)

    @get(
        "/{chain:str}/v1/submissions/{submission_id:str}",
        summary="postpaid_getStatus",
        responses=_errors(HTTP_400_BAD_REQUEST, HTTP_404_NOT_FOUND, HTTP_503_SERVICE_UNAVAILABLE),
    )
    async def get_submission(
        chain: Annotated[str, PathParameter(pattern=CHAIN_PATTERN)],
        submission_id: Annotated[str, PathParameter(pattern=HASH_PATTERN)],
    ) -> Response[StatusResult]:
        """What's become of a submission, and its stage. Retry-After: when its stage could
        next change, a slot, until it's done or failed."""
        relay = relay_for(chain)
        result = await status(relay, submission_id)
        final = result.stage in (Stage.DONE, Stage.FAILED)
        headers = {} if final else {"Retry-After": str(relay.chain.slot_seconds)}
        return Response(result, headers=headers)

    @post("/{chain:str}/v1/x402", include_in_schema=False, status_code=HTTP_200_OK)
    async def post_x402(
        request: Request[Any, Any, Any],
        chain: Annotated[str, PathParameter(pattern=CHAIN_PATTERN)],
        body: RawBody,
    ) -> Response[PaymentRequired | X402Result | ErrorBody]:
        """x402: the call in the body, 402 with what to pay; paid (PAYMENT-SIGNATURE), held
        until its transaction is included, 200 with it, or until it's rejected or expired, 402.
        Its status URL (Location's) follows it meanwhile."""
        relay = relay_for(chain)
        if not relay.domains:
            raise NotFoundException(f"chain {chain} takes no x402 payments")
        origin = _origin(request)
        url = x402_url(origin, relay.chain.chain_id)
        params = params_of(body)
        header = request.headers.get("payment-signature")
        if params is None or header is None:
            why = "the body isn't a call" if params is None else "PAYMENT-SIGNATURE is required"
            return _payment_required(await challenge(relay, url, params, why))
        try:
            payload = payload_of(header)
        except MalformedPaymentError as e:
            error = ErrorObject(Code.INVALID_PARAMS, str(e), "PAYMENT-SIGNATURE")
            return Response(ErrorBody(error), status_code=HTTP_400_BAD_REQUEST)
        payer = to_checksum_address(payload.payload.authorization.from_)
        submission: Submission | None = None
        try:
            submission = await take(relay, params, payload)
            # the same call paid again while the first is pending (a client that timed out and
            # signed again): held for the first, this authorization never used
            earlier = relay.pending_like(submission)
            # what's answered of: this one, or the first; held here, as a finished submission
            # keeps only its status
            held = submission if earlier is None else earlier.submission
            id_ = (await relay.submit(submission)).id if earlier is None else held.id
        except PaymentRefusedError as e:
            required = await challenge(relay, url, params, str(e))
            return _payment_required(required, refused(relay, e.reason, payer))
        except (UnderpaidError, NoCapacityError) as e:
            required = await challenge(relay, url, params, str(e), (e.token, e.required))
            return _payment_required(required, refused(relay, VALUE_MISMATCH, payer))
        except UnsupportedError as e:
            required = await challenge(relay, url, params, str(e))
            return _payment_required(required, refused(relay, INVALID_REQUIREMENTS, payer))
        except SimulationFailedError as e:
            reason = TRANSACTION_STATE if submission is None else await refusal(relay, submission)
            return Response(
                ErrorBody(error_object(e)),
                status_code=_PAYMENT_REQUIRED,
                headers={"PAYMENT-RESPONSE": encoded(refused(relay, reason, payer))},
            )
        tracked = await relay.outcome(id_)
        taken = result(origin, relay, held)
        if tracked.status is not Status.INCLUDED or tracked.transaction_hash is None:
            why = f"{tracked.status.value}: {tracked.reason or 'its deadline passed'}"
            required = await challenge(relay, url, params, why)
            failed = tracked.revert_data is not None  # it stopped simulating: why, if it's known
            reason = await refusal(relay, held) if failed else TRANSACTION_STATE
            return _payment_required(required, refused(relay, reason, payer))
        transaction = data(tracked.transaction_hash)
        return Response(
            taken,
            status_code=HTTP_200_OK,
            headers={
                "PAYMENT-RESPONSE": encoded(settled(relay, held, taken.status_url, transaction)),
                "Location": taken.status_url,
            },
        )

    @get("/.well-known/x402", include_in_schema=False)
    async def get_well_known_x402(request: Request[Any, Any, Any]) -> WellKnownX402:
        """Each chain's x402 endpoint, for discovery."""
        origin = _origin(request)
        return WellKnownX402(
            version=1,
            resources=tuple(
                x402_url(origin, chain_id)
                for chain_id, relay in sorted(relays.items())
                if relay.domains
            ),
        )

    @get("/openrpc.json", summary="OpenRPC")
    async def get_openrpc(request: Request[Any, Any, Any]) -> dict[str, object]:
        """The JSON-RPC methods' OpenRPC document, a server each chain served; rpc.discover
        answers it with the one chain's."""
        return openrpc([server(_origin(request), chain_id) for chain_id in sorted(relays)])

    @get("/openapi.json", summary="OpenAPI")
    async def get_openapi(request: Request[Any, Any, Any]) -> dict[str, object]:
        """The REST operations' OpenAPI document, x402's among them."""
        document = request.app.openapi_schema.to_schema()
        if any(r.domains for r in relays.values()):
            fees = [r.config.min_fee for r in relays.values() if r.domains]
            least = min((f.amount for f in fees if f is not None), default=None)
            operation, schemas = openapi_operation(None if least is None else str(least))
            document["paths"]["/{chain}/v1/x402"] = operation
            components = document.setdefault("components", {}).setdefault("schemas", {})
            for name, schema in schemas.items():
                components.setdefault(name, schema)
        return {**document, "servers": [{"url": _origin(request)}]}

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
            post_user_operation_gas,
            post_user_operation_quote,
            post_user_operation,
            get_user_operation,
            get_submission,
            post_x402,
            get_well_known_x402,
            get_openrpc,
            get_openapi,
            json_rpc,
        ],
        exception_handlers={
            BrunoError: _on_error,
            ValidationException: _on_error,
            NotFoundException: _on_error,
            HTTP_400_BAD_REQUEST: _on_error,
            HTTP_413_REQUEST_ENTITY_TOO_LARGE: _on_too_large,
            ChainError: _on_node_failure,
            JsonRpcError: _on_node_failure,
            httpx.HTTPError: _on_node_failure,
            HTTP_500_INTERNAL_SERVER_ERROR: _on_internal,
        },
        request_max_body_size=MAX_BODY_BYTES,
        cors_config=CORSConfig(
            allow_origins=["*"],
            allow_methods=["GET", "POST"],
            allow_headers=["content-type", "payment-signature"],
            expose_headers=["Location", "PAYMENT-REQUIRED", "PAYMENT-RESPONSE"],
        ),
        openapi_config=OpenAPIConfig(title=TITLE, version=VERSION, description=DESCRIPTION),
    )
