/**
 * The interface's operations, the same over REST and JSON-RPC (SPEC.md, Interface): the
 * relay's terms, a quote, a submission taken, a submission's status; and an error's JSON-RPC
 * error object. As bruno/server/operations.py's.
 */

import { Bundler } from "./bundler.ts";
import { type Hex, hex, hexQuantity } from "./evm.ts";
import { SimulationFailedError } from "./plan.ts";
import { type Part, type TokenQuote, unsupportedCategory, UnsupportedError } from "./pricing.ts";
import { type Accepted, NoCapacityError, type Relay, UnderpaidError } from "./relay.ts";
import { unavailabilityCategory, UnavailableError } from "./measurements.ts";
import { InvalidSubmissionError } from "./submission.ts";
import {
  type Finished,
  type Stage,
  stage,
  type Tracked,
  UnknownSubmissionError,
} from "./tracked.ts";
import { InvalidUserOperationError, type UserOperation } from "./userops.ts";
import { ChainError } from "./chain.ts";
import { JsonRpcError } from "./jsonrpc.ts";
import {
  address,
  array,
  Code,
  type ErrorObject,
  HASH,
  type InvalidField,
  InvalidParamsError,
  type NotServed,
  object,
  payerOf,
  quotedCall,
  type Reason,
  submission,
  transferTypedData,
} from "./wire.ts";
import { message } from "./refusals.ts";

export function terms(relay: Relay): unknown {
  const t = relay.terms();
  return {
    payee: t.payee,
    tokens: t.tokens,
    maxGas: hexQuantity(t.maxGas),
    maxCallDataLength: hexQuantity(t.maxCallBytes),
  };
}

export async function quote(relay: Relay, params: unknown): Promise<unknown> {
  const p = object(params, "quote");
  const tokens = [
    ...new Set(array(p.tokens, "tokens").map((t, i) => address(t, `tokens[${i}]`))),
  ];
  if (tokens.length === 0) throw new InvalidParamsError("tokens", message("tokens-none"));
  if (tokens.length > relay.terms().tokens.length) {
    throw new InvalidParamsError("tokens", message("tokens-too-many"));
  }
  const quotes = await relay.quote(quotedCall(p), tokens, payerOf(p));
  return {
    validUntilBlock: hexQuantity(quotes.validUntilBlock),
    quotes: quotes.quotes.map(tokenQuote),
    ...(quotes.gas === undefined ? {} : { gas: hexQuantity(quotes.gas) }),
  };
}

/** A token's quote as the wire writes it: broken down, and the transfer its payer signs as
 * EIP-712's JSON (bruno/server/wire.py's TokenQuote). */
function tokenQuote(q: TokenQuote): Record<string, unknown> {
  const part = (p: Part) => ({ gas: hexQuantity(p.gas), amount: hexQuantity(p.amount) });
  const b = q.breakdown;
  const t = q.transfer;
  return {
    token: q.token,
    amount: hexQuantity(q.amount),
    ...(b === undefined ? {} : {
      breakdown: {
        setup: part(b.setup),
        call: part(b.call),
        payment: part(b.payment),
        relay: part(b.relay),
      },
    }),
    ...(t === undefined ? {} : { typedData: transferTypedData(t) }),
  };
}

/** An op's gas limits (ERC-7769's eth_estimateUserOperationGas). */
export async function estimateUserOperation(
  relay: Relay,
  op: UserOperation,
): Promise<unknown> {
  const estimate = await new Bundler(relay).estimateUserOperation(op);
  const answer: Record<string, string> = {
    preVerificationGas: hexQuantity(estimate.preVerificationGas),
    verificationGasLimit: hexQuantity(estimate.verificationGasLimit),
    callGasLimit: hexQuantity(estimate.callGasLimit),
  };
  if (estimate.paymasterVerificationGasLimit !== null) {
    answer.paymasterVerificationGasLimit = hexQuantity(estimate.paymasterVerificationGasLimit);
  }
  if (estimate.paymasterPostOpGasLimit !== null) {
    answer.paymasterPostOpGasLimit = hexQuantity(estimate.paymasterPostOpGasLimit);
  }
  return answer;
}

/** What an op, paying by its calls, is to pay in each token (postpaid_quoteUserOperation). */
export async function quoteUserOperation(relay: Relay, op: UserOperation): Promise<unknown> {
  const quotes = await new Bundler(relay).quoteUserOperation(op);
  return {
    validUntilBlock: hexQuantity(quotes.validUntilBlock),
    quotes: quotes.quotes.map((q) => ({ token: q.token, amount: hexQuantity(q.amount) })),
  };
}

/** The price an op paying by its fees is asked, as both its fees, at each speed alike
 * (pimlico_getUserOperationGasPrice's and zd_getUserOperationGasPrice's answer). */
export async function userOperationGasPrice(relay: Relay): Promise<unknown> {
  const price = hexQuantity(await new Bundler(relay).userOperationGasPrice());
  const each = { maxFeePerGas: price, maxPriorityFeePerGas: price };
  return { slow: each, standard: each, fast: each };
}

/** The op taken, its hash (ERC-7769's eth_sendUserOperation). */
export async function sendUserOperation(
  relay: Relay,
  op: UserOperation,
): Promise<Hex> {
  return await new Bundler(relay).sendUserOperation(op);
}

/** ERC-7769's receipt of an op the relay took: null until it's included. */
export async function userOperationReceipt(relay: Relay, opHash: string): Promise<unknown> {
  if (!HASH.test(opHash)) throw new InvalidParamsError("userOpHash", message("hash-malformed"));
  const r = await new Bundler(relay).userOperationReceipt(opHash as Hex);
  if (r === null) return null;
  return {
    userOpHash: r.userOpHash,
    entryPoint: r.entryPoint,
    sender: r.sender,
    nonce: hexQuantity(r.nonce),
    paymaster: r.paymaster,
    actualGasCost: hexQuantity(r.actualGasCost),
    actualGasUsed: hexQuantity(r.actualGasUsed),
    success: r.success,
    reason: hex(r.reason),
    logs: r.logs,
    receipt: r.receipt,
  };
}

/** Take the submission in `params`: its id, and whether it's new. */
export async function send(relay: Relay, params: unknown): Promise<Accepted> {
  return await relay.submit(submission(params));
}

/** What's become of a submission, and its stage: the latest and finalized blocks read for one
 * that's included, at most once a slot. */
export async function statusOf(
  relay: Relay,
  id: string,
): Promise<{ stage: Stage } & Record<string, unknown>> {
  if (!HASH.test(id)) throw new InvalidParamsError("id", message("hash-malformed"));
  const t: Tracked | Finished = relay.queue.status(id as Hex);
  if ("submission" in t) {
    return { status: t.status, stage: stage(t, null), attempts: hexQuantity(t.attempts) };
  }
  const final = t.status === "included" ? await relay.finality() : null;
  return {
    status: t.status,
    stage: stage(t, final === null ? null : final.finalized),
    attempts: hexQuantity(t.attempts),
    ...(t.transactionHash === null ? {} : { transactionHash: t.transactionHash }),
    ...(t.blockNumber === null ? {} : { blockNumber: hexQuantity(t.blockNumber) }),
    ...(final === null || t.blockNumber === null
      ? {}
      : { confirmations: hexQuantity(Math.max(final.head - t.blockNumber + 1, 0)) }),
    ...(t.reason === null ? {} : { reason: t.reason }),
    ...(t.revertData === null ? {} : { revertData: t.revertData }),
  };
}

/** A request to a chain this relay doesn't serve, as the path names it. */
export class NotServedError extends Error {
  constructor(readonly chain: string) {
    super(message("not-served", { chain }));
  }
}

/** What a node failing a request is answered with, the relay unavailable for now, to try again
 * (no answer, a page, an error that isn't a revert): what failed logged, not answered. */
export function nodeFailed(e: Error): ErrorObject {
  console.warn(`a node failed: ${e.message}`);
  const data: Reason = { category: "retry", reason: "node-failed" };
  return { code: Code.UNAVAILABLE, message: message("node-failed"), data };
}

export function errorObject(e: unknown): ErrorObject {
  const message = e instanceof Error ? e.message : String(e);
  if (e instanceof InvalidSubmissionError || e instanceof InvalidUserOperationError) {
    const bound = e.bound;
    const data: InvalidField = bound === undefined ? { field: e.field } : {
      field: e.field,
      most: hexQuantity(bound.most),
      ...(bound.needs === undefined ? {} : { needs: hexQuantity(bound.needs) }),
    };
    return { code: Code.INVALID_PARAMS, message, data };
  }
  if (e instanceof InvalidParamsError) {
    return { code: Code.INVALID_PARAMS, message, data: { field: e.field } satisfies InvalidField };
  }
  if (e instanceof UnderpaidError) {
    return {
      code: Code.UNDERPAID,
      message,
      data: { token: e.token, required: hexQuantity(e.required) },
    };
  }
  if (e instanceof SimulationFailedError) {
    return {
      code: Code.SIMULATION_FAILED,
      message,
      data: { revertData: hex(e.revertData) },
    };
  }
  if (e instanceof UnsupportedError) {
    const data: Reason = { category: unsupportedCategory(e.reason), reason: e.reason };
    return { code: Code.UNSUPPORTED, message, data };
  }
  if (e instanceof NoCapacityError) {
    return {
      code: Code.NO_CAPACITY,
      message,
      data: { token: e.token, required: hexQuantity(e.required) },
    };
  }
  if (e instanceof NotServedError) {
    return { code: Code.METHOD_NOT_FOUND, message, data: { chain: e.chain } satisfies NotServed };
  }
  if (e instanceof UnknownSubmissionError) return { code: Code.UNKNOWN_SUBMISSION, message };
  if (e instanceof UnavailableError) {
    const data: Reason = { category: unavailabilityCategory(e.reason), reason: e.reason };
    return { code: Code.UNAVAILABLE, message, data };
  }
  if (e instanceof ChainError || e instanceof JsonRpcError) return nodeFailed(e);
  // what failed stays in the log: a node's URL, its key in it, isn't the client's to see
  console.error(e);
  return { code: Code.INTERNAL_ERROR, message: "internal error" };
}
