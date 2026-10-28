/**
 * The interface's operations, the same over REST and JSON-RPC (SPEC.md, Interface): the
 * relay's terms, a quote, a submission taken, a submission's status; and an error's JSON-RPC
 * error object. As the Python reference's (bruno/server/operations.py).
 */

import { type Address, type Hex, hex } from "./evm.ts";
import { SimulationFailedError } from "./plan.ts";
import {
  NoCapacityError,
  type Relay,
  UnavailableError,
  UnderpaidError,
  UnknownSubmissionError,
  UnsupportedError,
} from "./relay.ts";
import { InvalidSubmissionError } from "./submission.ts";
import { type Finished, type Stage, stage, type Tracked } from "./tracked.ts";
import { ENTRY_POINT, InvalidUserOperationError, type UserOperation } from "./userops.ts";
import {
  address,
  array,
  Code,
  HASH,
  InvalidParams,
  object,
  payerOf,
  safe,
  submission,
} from "./wire.ts";

export interface ErrorObject {
  code: number;
  message: string;
  data?: unknown;
}

const quantity = (v: bigint | number): string => `0x${v.toString(16)}`;

export function terms(relay: Relay): unknown {
  const t = relay.terms();
  return {
    payee: t.payee,
    tokens: t.tokens,
    maxGas: quantity(t.maxGas),
    maxCallBytes: quantity(t.maxCallBytes),
  };
}

export async function quote(relay: Relay, params: unknown): Promise<unknown> {
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
    payerOf(p),
  );
  return {
    validUntilBlock: quantity(quotes.validUntilBlock),
    quotes: quotes.quotes.map((q) => ({ token: q.token, amount: quantity(q.amount) })),
  };
}

/** An op's gas limits (ERC-7769's eth_estimateUserOperationGas). */
export async function estimateUserOperation(
  relay: Relay,
  op: UserOperation,
  entryPoint: Address,
): Promise<unknown> {
  const estimate = await relay.estimateUserOperation(op, entryPoint);
  return {
    preVerificationGas: quantity(estimate.preVerificationGas),
    verificationGasLimit: quantity(estimate.verificationGasLimit),
    callGasLimit: quantity(estimate.callGasLimit),
  };
}

/** The op taken, its hash (ERC-7769's eth_sendUserOperation). */
export async function sendUserOperation(
  relay: Relay,
  op: UserOperation,
  entryPoint: Address,
): Promise<Hex> {
  return await relay.sendUserOperation(op, entryPoint);
}

/** ERC-7769's receipt of an op the relay took: null until it's included. */
export async function userOperationReceipt(relay: Relay, opHash: string): Promise<unknown> {
  if (!HASH.test(opHash)) throw new InvalidParams("userOpHash", "32 bytes, as hex");
  const r = await relay.userOperationReceipt(opHash as Hex);
  if (r === null) return null;
  return {
    userOpHash: r.userOpHash,
    entryPoint: ENTRY_POINT,
    sender: r.sender,
    nonce: quantity(r.nonce),
    paymaster: r.paymaster,
    actualGasCost: quantity(r.actualGasCost),
    actualGasUsed: quantity(r.actualGasUsed),
    success: r.success,
    reason: hex(r.reason),
    logs: r.logs,
    receipt: r.receipt,
  };
}

/** Take the submission in `params`: its id, and whether it's new. */
export async function send(relay: Relay, params: unknown): Promise<{ id: Hex; isNew: boolean }> {
  return await relay.submit(submission(params));
}

/** What's become of a submission, and its stage: the latest and finalized blocks read for one
 * that's included, at most once a slot. */
export async function statusOf(
  relay: Relay,
  id: string,
): Promise<{ stage: Stage } & Record<string, unknown>> {
  if (!HASH.test(id)) throw new InvalidParams("id", "32 bytes, as hex");
  const t: Tracked | Finished = relay.status(id as Hex);
  if ("submission" in t) {
    return { status: t.status, stage: stage(t, null), attempts: quantity(t.attempts) };
  }
  const final = t.status === "included" ? await relay.finality() : null;
  return {
    status: t.status,
    stage: stage(t, final === null ? null : final.finalized),
    attempts: quantity(t.attempts),
    ...(t.transactionHash === null ? {} : { transactionHash: t.transactionHash }),
    ...(t.blockNumber === null ? {} : { blockNumber: quantity(t.blockNumber) }),
    ...(final === null || t.blockNumber === null
      ? {}
      : { confirmations: quantity(Math.max(final.head - t.blockNumber + 1, 0)) }),
    ...(t.reason === null ? {} : { reason: t.reason }),
    ...(t.revertData === null ? {} : { revertData: t.revertData }),
  };
}

export function errorObject(e: unknown): ErrorObject {
  const message = e instanceof Error ? e.message : String(e);
  if (
    e instanceof InvalidParams || e instanceof InvalidSubmissionError ||
    e instanceof InvalidUserOperationError
  ) {
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
  if (e instanceof UnavailableError) return { code: Code.UNAVAILABLE, message };
  // what failed stays in the log: a node's URL, its key in it, isn't the client's to see
  console.error(e);
  return { code: Code.INTERNAL_ERROR, message: "internal error" };
}
