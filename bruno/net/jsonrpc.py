"""JSON-RPC 2.0 over HTTP: one request, one result or one error; or a batch of requests, each
its own result or error."""

import itertools
from collections.abc import Sequence
from dataclasses import dataclass
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


class BatchError(Exception):
    """A batch's answer isn't one: not a list, or a request unanswered. The node's failure, as
    an HTTP error is."""


@dataclass(frozen=True, slots=True)
class Call:
    """One JSON-RPC call."""

    method: str
    params: list[Any]


@dataclass(frozen=True, slots=True)
class Answer:
    """One call's answer in a batch: its result, or the node's error answer to it."""

    result: Any
    error: JsonRpcError | None = None

    def value(self) -> Any:
        """The result: the error answer raised."""
        if self.error is not None:
            raise self.error
        return self.result


async def batch(
    client: httpx.AsyncClient,
    url: str,
    calls: Sequence[Call],
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[Answer]:
    """`calls` as one batch at `url`: their answers, in the calls' order, matched by id."""
    ids = [next(_ids) for _ in calls]
    requests = [
        {"jsonrpc": "2.0", "id": id_, "method": c.method, "params": c.params}
        for id_, c in zip(ids, calls, strict=True)
    ]
    response = await client.post(url, json=requests, timeout=timeout)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, list):
        raise BatchError("a batch answered with no list")
    by_id: dict[object, dict[str, Any]] = {a.get("id"): a for a in body if isinstance(a, dict)}
    answers: list[Answer] = []
    for id_, c in zip(ids, calls, strict=True):
        answer = by_id.get(id_)
        if answer is None:
            raise BatchError(f"{c.method}: no answer in the batch")
        if (error := answer.get("error")) is not None:
            failed = JsonRpcError(c.method, error["code"], error["message"], error.get("data"))
            answers.append(Answer(None, failed))
        else:
            answers.append(Answer(answer.get("result")))
    return answers
