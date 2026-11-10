/**
 * JSON-RPC 2.0 at POST /{chain}: the postpaid_ methods and ERC-7769's, each an operation of the
 * interface, and rpc.discover's OpenRPC document; any other method refused, reading nothing. As bruno/server/rpc.py's.
 */

import { hexQuantity } from "./evm.ts";
import {
  errorObject,
  estimateUserOperation,
  quote,
  quoteUserOperation,
  send,
  sendUserOperation,
  statusOf,
  terms,
  userOperationGasPrice,
  userOperationReceipt,
} from "./operations.ts";
import openrpcDocument from "./openrpc.json" with { type: "json" };
import type { Relay } from "./relay.ts";
import { ENTRY_POINTS, type UserOperation } from "./userops.ts";
import { address, Code, invalid, InvalidParamsError, userOperation } from "./wire.ts";
import { message } from "./refusals.ts";

/** The OpenRPC document, its `servers` the endpoints it describes. */
export function openrpc(servers: readonly { name: string; url: string }[]): unknown {
  return { ...openrpcDocument, servers };
}

/** A JSON-RPC request's response; rpc.discover's names `discovered` as its server. */
export async function answer(
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
  const invalidRequest = (field: string, why: string): unknown => ({
    jsonrpc: "2.0",
    id: null,
    error: invalid(Code.INVALID_REQUEST, field, why),
  });
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return invalidRequest("request", "an object");
  }
  const request = raw as Record<string, unknown>;
  // JSON-RPC 2.0's: "jsonrpc" exactly "2.0", a method named, an id a string, a number or null
  if (request.jsonrpc !== "2.0") return invalidRequest("jsonrpc", '"2.0"');
  if (typeof request.method !== "string") return invalidRequest("method", "a string");
  const id = request.id ?? null;
  if (id !== null && typeof id !== "string" && !Number.isSafeInteger(id)) {
    return invalidRequest("id", "a string, an integer or null");
  }
  // params by position, an array; by name, an object, which no method here takes, so none given
  // (bruno/server/rpc.py's answer); anything else, no request
  const given = request.params;
  if (given !== undefined && (given === null || typeof given !== "object")) {
    return invalidRequest("params", "a list, or an object");
  }
  const params = Array.isArray(given) ? given : [];
  try {
    return {
      jsonrpc: "2.0",
      id,
      result: await dispatch(relay, request.method, params, discovered),
    };
  } catch (e) {
    if (e instanceof MethodNotFoundError) {
      const error = { code: Code.METHOD_NOT_FOUND, message: `Method not found: ${e.message}` };
      return { jsonrpc: "2.0", id, error };
    }
    return { jsonrpc: "2.0", id, error: errorObject(e) };
  }
}

/** ERC-7769's two parameters: the op, and its EntryPoint. */
function userOperationOf(params: unknown[]): UserOperation {
  if (params.length !== 2) {
    throw new InvalidParamsError(
      "params",
      message("params-operation"),
    );
  }
  return userOperation(params[0], address(params[1], "entryPoint"));
}

async function dispatch(
  relay: Relay,
  method: string,
  params: unknown[],
  discovered: { name: string; url: string },
): Promise<unknown> {
  const one = (): unknown => {
    if (params.length !== 1) throw new InvalidParamsError("params", message("params-one"));
    return params[0];
  };
  // a hash, as text: checked as one where it's read
  const hash = (name: string): string => {
    const param = one();
    if (typeof param !== "string") throw new InvalidParamsError(name, message("hash-malformed"));
    return param;
  };
  switch (method) {
    case "rpc.discover":
      return openrpc([discovered]);
    case "postpaid_config":
      if (params.length !== 0) throw new InvalidParamsError("params", message("params-none"));
      return terms(relay);
    case "postpaid_quote":
      return await quote(relay, one());
    case "postpaid_sendTransaction":
      return { id: (await send(relay, one())).id };
    case "postpaid_getStatus":
      return await statusOf(relay, hash("id"));
    case "eth_estimateUserOperationGas":
      return await estimateUserOperation(relay, userOperationOf(params));
    case "postpaid_quoteUserOperation":
      return await quoteUserOperation(relay, userOperationOf(params));
    case "eth_sendUserOperation":
      return await sendUserOperation(relay, userOperationOf(params));
    case "eth_getUserOperationReceipt":
      return await userOperationReceipt(relay, hash("userOpHash"));
    case "pimlico_getUserOperationGasPrice":
    case "zd_getUserOperationGasPrice":
      if (params.length !== 0) throw new InvalidParamsError("params", message("params-none"));
      return await userOperationGasPrice(relay);
    case "eth_supportedEntryPoints":
      if (params.length !== 0) throw new InvalidParamsError("params", message("params-none"));
      return ENTRY_POINTS.map((e) => e.address);
    case "eth_chainId":
      if (params.length !== 0) throw new InvalidParamsError("params", message("params-none"));
      return hexQuantity(relay.chain.chainId);
    default:
      throw new MethodNotFoundError(method);
  }
}

class MethodNotFoundError extends Error {}
