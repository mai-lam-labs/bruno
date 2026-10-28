/**
 * JSON-RPC 2.0 at POST /{chain}: the postpaid_ methods and ERC-7769's, each an operation of the
 * interface, and rpc.discover's OpenRPC document; any other method refused, reading nothing. As the Python reference's (bruno/server/rpc.py).
 */

import {
  errorObject,
  estimateUserOperation,
  quote,
  send,
  sendUserOperation,
  statusOf,
  terms,
  userOperationGasPrice,
  userOperationReceipt,
} from "./operations.ts";
import openrpcDocument from "./openrpc.json" with { type: "json" };
import type { Address } from "./evm.ts";
import type { Relay } from "./relay.ts";
import { ENTRY_POINT, type UserOperation } from "./userops.ts";
import { address, Code, InvalidParams, object, userOperation } from "./wire.ts";

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
    if (e instanceof MethodNotFound) {
      const error = { code: Code.METHOD_NOT_FOUND, message: `Method not found: ${e.message}` };
      return { jsonrpc: "2.0", id, error };
    }
    return { jsonrpc: "2.0", id, error: errorObject(e) };
  }
}

/** ERC-7769's two parameters: the op, and its EntryPoint. */
function userOperationPair(params: unknown[]): [UserOperation, Address] {
  if (params.length !== 2) {
    throw new InvalidParams(
      "params",
      "two parameters, by position: the user operation and its EntryPoint",
    );
  }
  return [userOperation(params[0]), address(params[1], "entryPoint")];
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
      return { id: (await send(relay, one())).id };
    case "postpaid_getStatus":
      return await statusOf(relay, String(one()));
    case "eth_estimateUserOperationGas":
      return await estimateUserOperation(relay, ...userOperationPair(params));
    case "eth_sendUserOperation":
      return await sendUserOperation(relay, ...userOperationPair(params));
    case "eth_getUserOperationReceipt":
      return await userOperationReceipt(relay, String(one()));
    case "pimlico_getUserOperationGasPrice":
    case "zd_getUserOperationGasPrice":
      if (params.length !== 0) throw new InvalidParams("params", "none");
      return await userOperationGasPrice(relay);
    case "eth_supportedEntryPoints":
      if (params.length !== 0) throw new InvalidParams("params", "none");
      return [ENTRY_POINT];
    case "eth_chainId":
      if (params.length !== 0) throw new InvalidParams("params", "none");
      return `0x${relay.chain.chainId.toString(16)}`;
    default:
      throw new MethodNotFound(method);
  }
}

class MethodNotFound extends Error {}
