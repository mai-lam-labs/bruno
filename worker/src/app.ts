/**
 * The relay's HTTP interface for one chain (SPEC.md, Interface): REST under /{chain}/v1, x402 at
 * /{chain}/v1/x402 and JSON-RPC at POST /{chain}, the same requests, responses and errors as the
 * Python reference's server (bruno/server/app.py).
 */

import type { Hex } from "./evm.ts";
import { chainPath, server } from "./front.ts";
import { errorObject, estimateUserOperation, quote, send, statusOf, terms } from "./operations.ts";
import { SimulationFailedError } from "./plan.ts";
import { NoCapacityError, type Relay, UnderpaidError, UnsupportedError } from "./relay.ts";
import { answer } from "./rpc.ts";
import type { Submission } from "./submission.ts";
import { Code, estimateParams, InvalidParams } from "./wire.ts";
import {
  challenge,
  encoded,
  INVALID_REQUIREMENTS,
  MalformedPaymentError,
  paramsOf,
  payloadOf,
  PaymentRefusedError,
  refusal,
  refused,
  settled,
  statusUrl,
  take,
  TRANSACTION_STATE,
  VALUE_MISMATCH,
  x402Url,
} from "./x402.ts";

/** The largest request body read: a submission's call data, as hex, is most of it. */
const MAX_BODY_CHARS = 256 * 1024;

const HTTP_STATUS: Readonly<Record<number, number>> = {
  [Code.INVALID_PARAMS]: 400,
  [Code.UNDERPAID]: 422,
  [Code.SIMULATION_FAILED]: 422,
  [Code.UNSUPPORTED]: 422,
  [Code.NO_CAPACITY]: 503,
  [Code.UNKNOWN_SUBMISSION]: 404,
  [Code.UNAVAILABLE]: 503,
};

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
      const optional = (name: string): Record<string, string> => {
        const value = query.get(name);
        return value === null ? {} : { [name]: value };
      };
      const deployment = ["account", "factory", "factoryData"].map((f) => query.get(f));
      if (deployment.some((f) => f !== null) && deployment.includes(null)) {
        throw new InvalidParams("deployment", "a deployment is account, factory and factoryData");
      }
      const [account, factory, factoryData] = deployment;
      return json(
        await quote(relay, {
          gas: query.get("gas"),
          callBytes: query.get("callBytes"),
          tokens: query.getAll("tokens"),
          ...optional("authority"),
          ...optional("payer"),
          ...optional("verifyGas"),
          ...(account === null ? {} : { deployment: { account, factory, factoryData } }),
        }),
      );
    }
    if (request.method === "POST" && path === "/v1/submissions") {
      const { id, isNew } = await send(relay, parsed(await body(request)));
      return json({ id }, isNew ? 202 : 200, {
        Location: `/${chainPath(relay.chain.chainId)}/v1/submissions/${id}`,
      });
    }
    if (request.method === "POST" && path === "/v1/user-operations/gas") {
      const { op, entryPoint } = estimateParams(parsed(await body(request)));
      return json(await estimateUserOperation(relay, op, entryPoint));
    }
    if (request.method === "POST" && path === "/v1/x402" && relay.domains.size > 0) {
      return await x402(relay, request);
    }
    const status = /^\/v1\/submissions\/(0x[0-9a-fA-F]*)$/.exec(path);
    if (request.method === "GET" && status !== null) {
      // Retry-After: when its stage could next change, a slot, until it's done or failed
      const result = await statusOf(relay, status[1] as string);
      const final = result.stage === "done" || result.stage === "failed";
      return json(result, 200, final ? {} : { "Retry-After": String(relay.chain.slotSeconds) });
    }
    return json({ error: { code: Code.METHOD_NOT_FOUND, message: "not found: see /" } }, 404);
  } catch (e) {
    const error = errorObject(e);
    return json({ error }, HTTP_STATUS[error.code] ?? 500);
  }
}

/** x402: the call in the body, 402 with what to pay; paid (PAYMENT-SIGNATURE), held until its
 * transaction is included, 200 with it, or until it's rejected or expired, 402. Its status URL
 * (Location's) follows it meanwhile. */
async function x402(relay: Relay, request: Request): Promise<Response> {
  const origin = new URL(request.url).origin;
  const chainId = relay.chain.chainId;
  const url = x402Url(origin, chainId);
  const call = paramsOf(await body(request));
  const header = request.headers.get("payment-signature");
  if (call === null || header === null) {
    const why = call === null ? "the body isn't a call" : "PAYMENT-SIGNATURE is required";
    return paymentRequired(await challenge(relay, url, call, why));
  }
  let payload;
  try {
    payload = payloadOf(header);
  } catch (e) {
    if (!(e instanceof MalformedPaymentError)) throw e;
    return json(
      { error: { code: Code.INVALID_PARAMS, message: e.message, data: "PAYMENT-SIGNATURE" } },
      400,
    );
  }
  const payer = payload.authorization.from;
  let id: Hex;
  let submission: Submission | null = null;
  try {
    submission = await take(relay, call, payload);
    // the same call paid again while the first is pending (a client that timed out and signed
    // again): held for the first, this authorization never used
    const earlier = relay.pendingLike(submission);
    id = earlier === null ? (await relay.submit(submission)).id : earlier.id;
  } catch (e) {
    if (e instanceof PaymentRefusedError) {
      const required = await challenge(relay, url, call, e.message);
      return paymentRequired(required, refused(relay, e.reason, payer));
    }
    if (e instanceof UnderpaidError || e instanceof NoCapacityError) {
      const least = { token: e.token, amount: e.required };
      const required = await challenge(relay, url, call, e.message, least);
      return paymentRequired(required, refused(relay, VALUE_MISMATCH, payer));
    }
    if (e instanceof UnsupportedError) {
      const required = await challenge(relay, url, call, e.message);
      return paymentRequired(required, refused(relay, INVALID_REQUIREMENTS, payer));
    }
    if (e instanceof SimulationFailedError) {
      const reason = submission === null ? TRANSACTION_STATE : await refusal(relay, submission);
      return json({ error: errorObject(e) }, 402, {
        "PAYMENT-RESPONSE": encoded(refused(relay, reason, payer)),
      });
    }
    throw e;
  }
  const tracked = await relay.outcome(id);
  const at = statusUrl(origin, chainId, id);
  if (tracked.status !== "included" || tracked.transactionHash === null) {
    const why = `${tracked.status}: ${tracked.reason ?? "its deadline passed"}`;
    const required = await challenge(relay, url, call, why);
    // it stopped simulating: why, if it's known
    const failed = tracked.revertData !== null;
    const reason = failed ? await refusal(relay, tracked.submission) : TRANSACTION_STATE;
    return paymentRequired(required, refused(relay, reason, payer));
  }
  return json({ id, statusUrl: at }, 200, {
    "PAYMENT-RESPONSE": encoded(settled(relay, tracked.submission, at, tracked.transactionHash)),
    Location: at,
  });
}

/** 402: what to pay, in PAYMENT-REQUIRED and the body; why a payment wasn't taken, if one was
 * refused. */
function paymentRequired(required: unknown, response: unknown = null): Response {
  const headers: Record<string, string> = { "PAYMENT-REQUIRED": encoded(required) };
  if (response !== null) headers["PAYMENT-RESPONSE"] = encoded(response);
  return json(required, 402, headers);
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

export function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}
