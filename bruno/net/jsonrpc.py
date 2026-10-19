"""JSON-RPC 2.0 over HTTP: one request, one result or one error."""

import itertools
from typing import Any

import httpx

# an engineering bound: a node that hasn't answered a single call in this long is down
DEFAULT_TIMEOUT_SECONDS = 30.0

_ids = itertools.count(1)


class JsonRpcError(Exception):
    """The node answered with an error object. Deliberately not a BrunoError: a node failing
    is unexpected, not a client's doing."""

    def __init__(self, method: str, code: int, message: str, data: object) -> None:
        super().__init__(f"{method}: {code} {message}" + (f" ({data})" if data is not None else ""))
        self.code = code
        self.message = message
        self.data = data


async def call(
    client: httpx.AsyncClient,
    url: str,
    method: str,
    params: list[Any],
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Any:
    """`method` with `params` at `url`: its result, or JsonRpcError. Params and results are
    the wire's JSON, dynamic by nature: their typed reading is the caller's."""
    request = {"jsonrpc": "2.0", "id": next(_ids), "method": method, "params": params}
    response = await client.post(url, json=request, timeout=timeout)
    response.raise_for_status()
    body: dict[str, Any] = response.json()
    if (error := body.get("error")) is not None:
        raise JsonRpcError(method, error["code"], error["message"], error.get("data"))
    return body["result"]
