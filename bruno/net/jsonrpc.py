"""JSON-RPC 2.0 over HTTP: one request, one result or one error; or a batch of requests, each
its own result or error."""

import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from ..common import Json

# how long a node is given to answer one request: past it, the next node is asked
RPC_TIMEOUT_SECONDS = 10.0
# the most calls a batch request carries unless a node caps it lower (Node.max_batch): nodes cap
# a batch (geth at 1,000), and a larger batch is sent as several
MAX_BATCH = 100
# how a node says a call or an estimate reverts: EIP-1474's execution error, its data the
# revert data
REVERTED = 3

_ids = itertools.count(1)


@dataclass(frozen=True, slots=True)
class Node:
    """A node: its URL, and the most calls it takes in one batch request (a public node may cap
    it far lower: Unichain's at 10)."""

    url: str
    max_batch: int = MAX_BATCH


class JsonRpcError(Exception):
    """The node answered with an error object. Deliberately not a BrunoError: a node failing
    is unexpected, not a client's doing."""

    def __init__(self, method: str, code: int, message: str, data: object) -> None:
        super().__init__(f"{method}: {code} {message}" + (f" ({data})" if data is not None else ""))
        self.code = code
        self.message = message
        self.data = data


class AnswerError(Exception):
    """What a node sent isn't an answer: not JSON (a proxy's error page, say), a batch's not a
    list, or a request unanswered. The node's failure, as an HTTP error is."""


@dataclass(frozen=True, slots=True)
class Call:
    """One JSON-RPC call."""

    method: str
    params: list[Any]


@dataclass(frozen=True, slots=True)
class Answer:
    """One call's answer: its result, or the node's error answer to it."""

    result: Any
    error: JsonRpcError | None = None

    def value(self) -> Any:
        """The result: the error answer raised."""
        if self.error is not None:
            raise self.error
        return self.result


async def call(
    client: httpx.AsyncClient,
    url: str,
    method: str,
    params: list[Any],
    timeout: float = RPC_TIMEOUT_SECONDS,
) -> Any:
    """`method` with `params` at `url`: its result, or JsonRpcError. Params and results are
    the wire's JSON, dynamic by nature: their typed reading is the caller's."""
    request = {"jsonrpc": "2.0", "id": next(_ids), "method": method, "params": params}
    response = await client.post(url, json=request, timeout=timeout)
    response.raise_for_status()
    return answer_to(method, _json(response)).value()


async def batch(
    client: httpx.AsyncClient,
    url: str,
    calls: Sequence[Call],
    timeout: float = RPC_TIMEOUT_SECONDS,
) -> list[Answer]:
    """`calls` as one batch at `url`: their answers, in the calls' order, matched by id."""
    ids = [next(_ids) for _ in calls]
    requests = [
        {"jsonrpc": "2.0", "id": id_, "method": c.method, "params": c.params}
        for id_, c in zip(ids, calls, strict=True)
    ]
    response = await client.post(url, json=requests, timeout=timeout)
    response.raise_for_status()
    body = _json(response)
    if not isinstance(body, list):
        raise AnswerError("a batch answered with no list")
    by_id = {a.get("id"): a for a in body if isinstance(a, dict)}
    return [answer_to(c.method, by_id.get(id_)) for id_, c in zip(ids, calls, strict=True)]


def answer_to(method: str, body: Json) -> Answer:
    """`method`'s answer: its result, or its error answer; AnswerError if it's neither."""
    if not isinstance(body, dict):
        raise AnswerError(f"{method}: no answer")
    if (error := body.get("error")) is not None:
        if not isinstance(error, dict) or not isinstance(code := error.get("code"), int):
            raise AnswerError(f"{method}: an error that isn't one")
        return Answer(
            None, JsonRpcError(method, code, str(error.get("message")), error.get("data"))
        )
    if "result" not in body:
        raise AnswerError(f"{method}: no result")
    return Answer(body["result"])


def _json(response: httpx.Response) -> Json:
    try:
        return response.json()
    except ValueError as e:
        raise AnswerError("not JSON") from e
