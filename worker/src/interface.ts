/**
 * The relay's HTTP interface for one chain (SPEC.md, Interface): REST under /{chain}/v1 and
 * JSON-RPC at POST /{chain}, the same requests, responses and errors as the Python
 * reference's server (bruno/server/).
 */

import { type Address, bytes, checksum, type Hex, hex } from "./evm.ts";
import { chainPath, openrpc, server } from "./front.ts";
import { SimulationFailedError } from "./plan.ts";
import {
  NoCapacityError,
  type Relay,
  UnderpaidError,
  UnknownSubmissionError,
  UnsupportedError,
} from "./relay.ts";
import { InvalidSubmissionError, type Submission } from "./submission.ts";
import type { Tracked } from "./tracked.ts";
import type { SignedAuthorization } from "./transactions.ts";

const QUANTITY = /^0x(0|[1-9a-f][0-9a-f]*)$/;
const ADDRESS = /^0x[0-9a-fA-F]{40}$/;
const DATA = /^0x([0-9a-fA-F]{2})*$/;
const HASH = /^0x[0-9a-f]{64}$/;
/** The largest request body read: a submission's call data, as hex, is most of it. */
const MAX_BODY_CHARS = 256 * 1024;
const UINT256 = 1n << 256n;
const UINT64 = 1n << 64n;
const SAFE = BigInt(Number.MAX_SAFE_INTEGER) + 1n;

/** JSON-RPC 2.0's own error codes, and the relay's. */
export const Code = {
  PARSE_ERROR: -32700,
  INVALID_REQUEST: -32600,
  METHOD_NOT_FOUND: -32601,
  INVALID_PARAMS: -32602,
  INTERNAL_ERROR: -32603,
  UNDERPAID: -32010,
  SIMULATION_FAILED: -32011,
  UNSUPPORTED: -32012,
  NO_CAPACITY: -32013,
  UNKNOWN_SUBMISSION: -32014,
} as const;

const HTTP_STATUS: Readonly<Record<number, number>> = {
  [Code.INVALID_PARAMS]: 400,
  [Code.UNDERPAID]: 422,
  [Code.SIMULATION_FAILED]: 422,
  [Code.UNSUPPORTED]: 422,
  [Code.NO_CAPACITY]: 503,
  [Code.UNKNOWN_SUBMISSION]: 404,
};

interface ErrorObject {
  code: number;
  message: string;
  data?: unknown;
}

/** A request that isn't one: the field, and why. */
class InvalidParams extends Error {
  constructor(readonly field: string, message: string) {
    super(`${field}: ${message}`);
  }
}

const quantity = (v: bigint | number): string => `0x${v.toString(16)}`;

/** A request to `/{chainId}/...`, `path` being what follows the chain. */
export async function handle(relay: Relay, request: Request, path: string): Promise<Response> {
  try {
    if (request.method === "POST" && path === "") {
      const discovered = server(new URL(request.url).origin, relay.chain.chainId);
      return json(await answer(relay, await body(request), discovered));
    }
    if (request.method === "GET" && path === "/v1/config") {
      return json(terms(relay));
    }
    if (request.method === "GET" && path === "/v1/quote") {
      const query = new URL(request.url).searchParams;
      const authority = query.get("authority");
      return json(
        await quote(relay, {
          gas: query.get("gas"),
          callBytes: query.get("callBytes"),
          tokens: query.getAll("tokens"),
          ...(authority === null ? {} : { authority }),
        }),
      );
    }
    if (request.method === "POST" && path === "/v1/submissions") {
      const { id, isNew } = await relay.submit(submission(parsed(await body(request))));
      return json({ id }, isNew ? 202 : 200, {
        Location: `/${chainPath(relay.chain.chainId)}/v1/submissions/${id}`,
      });
    }
    const status = /^\/v1\/submissions\/(0x[0-9a-fA-F]*)$/.exec(path);
    if (request.method === "GET" && status !== null) {
      return json(statusOf(relay, status[1] as string));
    }
    return json({ error: { code: Code.METHOD_NOT_FOUND, message: "not found: see /" } }, 404);
  } catch (e) {
    const error = errorObject(e);
    return json({ error }, HTTP_STATUS[error.code] ?? 500);
  }
}

/** The request's body, if it isn't over MAX_BODY_CHARS. */
async function body(request: Request): Promise<string> {
  const declared = Number(request.headers.get("content-length") ?? 0);
  if (declared > MAX_BODY_CHARS) throw new InvalidParams("body", "too large");
  const text = await request.text();
  if (text.length > MAX_BODY_CHARS) throw new InvalidParams("body", "too large");
  return text;
}

function parsed(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    throw new InvalidParams("body", "not JSON");
  }
}

function terms(relay: Relay): unknown {
  const t = relay.terms();
  return {
    payee: t.payee,
    tokens: t.tokens,
    maxGas: quantity(t.maxGas),
    maxCallBytes: quantity(t.maxCallBytes),
  };
}

async function quote(relay: Relay, params: unknown): Promise<unknown> {
  const p = object(params, "quote");
  const tokens = [
    ...new Set(array(p.tokens, "tokens").map((t, i) => address(t, `tokens[${i}]`))),
  ];
  if (tokens.length === 0) throw new InvalidParams("tokens", "at least one");
  if (tokens.length > relay.terms().tokens.length) {
    throw new InvalidParams("tokens", "more than are accepted");
  }
  const quotes = await relay.quote(
    safe(p.gas, "gas"),
    safe(p.callBytes, "callBytes"),
    p.authority === undefined || p.authority === null ? null : address(p.authority, "authority"),
    tokens,
  );
  return {
    validUntilBlock: quantity(quotes.validUntilBlock),
    quotes: quotes.quotes.map((q) => ({ token: q.token, amount: quantity(q.amount) })),
  };
}

function statusOf(relay: Relay, id: string): unknown {
  if (!HASH.test(id)) throw new InvalidParams("id", "32 bytes, as hex");
  const t: Tracked = relay.status(id as Hex);
  return {
    status: t.status,
    attempts: quantity(t.attempts),
    ...(t.transactionHash === null ? {} : { transactionHash: t.transactionHash }),
    ...(t.blockNumber === null ? {} : { blockNumber: quantity(t.blockNumber) }),
    ...(t.reason === null ? {} : { reason: t.reason }),
    ...(t.revertData === null ? {} : { revertData: t.revertData }),
  };
}

function submission(raw: unknown): Submission {
  const s = object(raw, "submission");
  const call = object(s.call, "call");
  const payment = object(s.payment, "payment");
  const authorizations = array(s.authorizationList ?? [], "authorizationList").map(
    (a, i) => authorization(a, `authorizationList[${i}]`),
  );
  return {
    call: { to: address(call.to, "call.to"), data: bytes(data(call.data, "call.data")), value: 0n },
    authorizations,
    gas: safe(s.gas, "gas"),
    token: address(payment.token, "payment.token"),
    payee: address(payment.payee, "payment.payee"),
    amount: quantityOf(payment.amount, "payment.amount", UINT256),
    maxBlockNumber: safe(s.maxBlockNumber, "maxBlockNumber"),
  };
}

function authorization(raw: unknown, at: string): SignedAuthorization {
  const a = object(raw, at);
  return {
    chainId: quantityOf(a.chainId, `${at}.chainId`, UINT256),
    address: address(a.address, `${at}.address`),
    nonce: quantityOf(a.nonce, `${at}.nonce`, UINT64),
    yParity: safe(a.yParity, `${at}.yParity`),
    r: quantityOf(a.r, `${at}.r`, UINT256),
    s: quantityOf(a.s, `${at}.s`, UINT256),
  };
}

// --- JSON-RPC ---

/** A JSON-RPC request's response; rpc.discover's names `discovered` as its server. */
async function answer(
  relay: Relay,
  body: string,
  discovered: { name: string; url: string },
): Promise<unknown> {
  let raw: unknown;
  try {
    raw = JSON.parse(body);
  } catch {
    return { jsonrpc: "2.0", id: null, error: { code: Code.PARSE_ERROR, message: "Parse error" } };
  }
  let request: Record<string, unknown>;
  try {
    request = object(raw, "request");
  } catch {
    return {
      jsonrpc: "2.0",
      id: null,
      error: { code: Code.INVALID_REQUEST, message: "Invalid Request" },
    };
  }
  const id = request.id ?? null;
  const params = Array.isArray(request.params) ? request.params : [];
  try {
    return {
      jsonrpc: "2.0",
      id,
      result: await dispatch(relay, String(request.method), params, discovered),
    };
  } catch (e) {
    return { jsonrpc: "2.0", id, error: errorObject(e) };
  }
}

async function dispatch(
  relay: Relay,
  method: string,
  params: unknown[],
  discovered: { name: string; url: string },
): Promise<unknown> {
  const one = (): unknown => {
    if (params.length !== 1) throw new InvalidParams("params", "one parameter, by position");
    return params[0];
  };
  switch (method) {
    case "rpc.discover":
      return openrpc([discovered]);
    case "postpaid_config":
      if (params.length !== 0) throw new InvalidParams("params", "none");
      return terms(relay);
    case "postpaid_quote":
      return await quote(relay, one());
    case "postpaid_sendTransaction":
      return { id: (await relay.submit(submission(one()))).id };
    case "postpaid_getStatus":
      return statusOf(relay, String(one()));
    default:
      throw new MethodNotFound(method);
  }
}

class MethodNotFound extends Error {}

function errorObject(e: unknown): ErrorObject {
  const message = e instanceof Error ? e.message : String(e);
  if (e instanceof InvalidParams || e instanceof InvalidSubmissionError) {
    return { code: Code.INVALID_PARAMS, message, data: e.field };
  }
  if (e instanceof UnderpaidError) {
    return {
      code: Code.UNDERPAID,
      message,
      data: { token: e.token, required: quantity(e.required) },
    };
  }
  if (e instanceof SimulationFailedError) {
    return {
      code: Code.SIMULATION_FAILED,
      message,
      data: { revertData: hex(e.revertData) },
    };
  }
  if (e instanceof UnsupportedError) return { code: Code.UNSUPPORTED, message };
  if (e instanceof NoCapacityError) {
    return {
      code: Code.NO_CAPACITY,
      message,
      data: { token: e.token, required: quantity(e.required) },
    };
  }
  if (e instanceof UnknownSubmissionError) return { code: Code.UNKNOWN_SUBMISSION, message };
  if (e instanceof MethodNotFound) {
    return { code: Code.METHOD_NOT_FOUND, message: `Method not found: ${message}` };
  }
  // what failed stays in the log: a node's URL, its key in it, isn't the client's to see
  console.error(e);
  return { code: Code.INTERNAL_ERROR, message: "internal error" };
}

// --- reading requests ---

function object(raw: unknown, at: string): Record<string, unknown> {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    throw new InvalidParams(at, "an object");
  }
  return raw as Record<string, unknown>;
}

function array(raw: unknown, at: string): unknown[] {
  if (!Array.isArray(raw)) throw new InvalidParams(at, "a list");
  return raw;
}

/** A hex quantity under `bound`: a uint256's, unless the field's type is narrower. */
function quantityOf(raw: unknown, at: string, bound: bigint = UINT256): bigint {
  if (typeof raw !== "string" || !QUANTITY.test(raw) || raw.length > 66) {
    throw new InvalidParams(at, "a quantity, as hex");
  }
  const value = BigInt(raw);
  if (value >= bound) throw new InvalidParams(at, "out of range");
  return value;
}

/** A hex quantity that's a safe integer: gas, a length, a block number. */
function safe(raw: unknown, at: string): number {
  return Number(quantityOf(raw, at, SAFE));
}

function address(raw: unknown, at: string): Address {
  if (typeof raw !== "string" || !ADDRESS.test(raw)) throw new InvalidParams(at, "an address");
  return checksum(raw);
}

function data(raw: unknown, at: string): Hex {
  if (typeof raw !== "string" || !DATA.test(raw)) throw new InvalidParams(at, "bytes, as hex");
  return raw.toLowerCase() as Hex;
}

export function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}
