/**
 * JSON-RPC 2.0 over HTTP: one request, one result or one error; or a batch of requests, each its
 * own result or error (bruno/jsonrpc.py).
 */

import { hex } from "./evm.ts";
import { millisOf, Seconds } from "./units.ts";

/** How long a node is given to answer one request: past it, the next node is asked. */
export const RPC_TIMEOUT = Seconds(10);
/** The most calls a batch request carries unless a node caps it lower (Node.maxBatch): nodes cap
 * a batch (geth at 1,000), and a larger batch is sent as several. */
export const MAX_BATCH = 100;
/** How a node says a call or an estimate reverts: EIP-1474's execution error, its data the
 * revert data. */
export const REVERTED = 3;

let ids = 0;

/** A node: its URL, and the most calls it takes in one batch request (a public node may cap it
 * far lower: Unichain's at 10). */
export interface Node {
  readonly url: string;
  readonly maxBatch: number;
}

/**
 * A node answered with an error object: a revert included. Its `message` says which call and
 * code; `nodeMessage` is the node's own words (Python's JsonRpcError.message).
 */
export class JsonRpcError extends Error {
  constructor(
    readonly method: string,
    readonly code: number,
    readonly nodeMessage: string,
    readonly data: unknown,
  ) {
    super(`${method}: ${code} ${nodeMessage}`);
  }
}

/** What a node sent isn't an answer: not JSON (a proxy's error page, say), a batch's not a list,
 * or a request unanswered. The node's failure, as an HTTP error is. */
export class AnswerError extends Error {}

/** A node answered with an HTTP error status. */
export class StatusError extends Error {
  constructor(readonly status: number) {
    super(`HTTP ${status}`);
  }
}

/** One JSON-RPC call. */
export interface Call {
  readonly method: string;
  readonly params: readonly unknown[];
}

/** eth_call of `data` to `to`, at `block`. */
export function ethCall(to: string, data: Uint8Array, block = "latest"): Call {
  return { method: "eth_call", params: [{ to, data: hex(data) }, block] };
}

/** One call's answer: its result, or the node's error answer to it. */
export type Answer = { readonly result: unknown } | { readonly error: JsonRpcError };

/** An answer's result: its error answer raised. */
export function resultOf(answer: Answer): unknown {
  if ("error" in answer) throw answer.error;
  return answer.result;
}

/** `method` with `params` at `url`: its result, or JsonRpcError. */
export async function call(
  url: string,
  method: string,
  params: readonly unknown[],
): Promise<unknown> {
  const request = { jsonrpc: "2.0", id: ++ids, method, params };
  return resultOf(answerTo(method, await post(url, request)));
}

/** `calls` as one batch at `url`: their answers, in the calls' order, matched by id. */
export async function batch(url: string, calls: readonly Call[]): Promise<Answer[]> {
  const requests = calls.map((c) => ({ jsonrpc: "2.0", id: ++ids, ...c }));
  const body = await post(url, requests);
  if (!Array.isArray(body)) throw new AnswerError("a batch answered with no list");
  const byId = new Map<unknown, unknown>();
  for (const item of body) {
    if (typeof item === "object" && item !== null) {
      byId.set((item as Record<string, unknown>).id, item);
    }
  }
  return requests.map((r) => answerTo(r.method, byId.get(r.id)));
}

/** `method`'s answer: its result, or its error answer; AnswerError if it's neither. */
export function answerTo(method: string, body: unknown): Answer {
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    throw new AnswerError(`${method}: no answer`);
  }
  const answer = body as Record<string, unknown>;
  if (answer.error !== undefined && answer.error !== null) {
    const error = answer.error as Record<string, unknown>;
    if (typeof error !== "object" || !Number.isInteger(error.code)) {
      throw new AnswerError(`${method}: an error that isn't one`);
    }
    return {
      error: new JsonRpcError(method, error.code as number, String(error.message), error.data),
    };
  }
  if (!("result" in answer)) throw new AnswerError(`${method}: no result`);
  return { result: answer.result };
}

async function post(url: string, payload: unknown): Promise<unknown> {
  const response = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(millisOf(RPC_TIMEOUT)),
  });
  if (!response.ok) throw new StatusError(response.status);
  try {
    return await response.json();
  } catch {
    throw new AnswerError("not JSON");
  }
}
