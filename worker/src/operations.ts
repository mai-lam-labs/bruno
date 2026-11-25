/**
 * The interface's operations, the same over REST and JSON-RPC (SPEC.md, Interface): the
 * relay's terms, a quote, a submission taken, a submission's status; and an error's JSON-RPC
 * error object. As bruno/server/operations.py's.
 */

import { Bundler } from "./bundler.ts";
import { type Address, type Hex, hex, hexQuantity } from "./evm.ts";
import { SimulationFailedError } from "./plan.ts";
import { type Part, type TokenQuote, UnsupportedError } from "./pricing.ts";
import { type Accepted, NoCapacityError, type Relay, UnderpaidError } from "./relay.ts";
import { UnavailableError } from "./measurements.ts";
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
  askedOf,
  Code,
  type ErrorData,
  errorDataOf,
  type ErrorObject,
  HASH,
  invalidData,
  InvalidParamsError,
  object,
  payerOf,
  quotedCall,
  sponsorship,
  submission,
  transferTypedData,
} from "./wire.ts";
import type { SponsoredQuotes, ToSign } from "./sponsoring.ts";
import { message, type Refused, refused } from "./refusals.ts";

export function terms(relay: Relay): unknown {
  const t = relay.terms();
  return {
    payee: t.payee,
    forwarder: t.forwarder,
    tokens: t.tokens,
    maxGas: hexQuantity(t.maxGas),
    maxCallDataLength: hexQuantity(t.maxCallBytes),
  };
}

/** The tokens a quote's params `p` name, once each: InvalidParamsError with none, or more than
 * the relay accepts. */
function tokensOf(relay: Relay, p: Record<string, unknown>): Address[] {
  const tokens = [
    ...new Set(array(p.tokens, "tokens").map((t, i) => address(t, `tokens[${i}]`))),
  ];
  if (tokens.length === 0) throw new InvalidParamsError("tokens", refused("tokens-none"));
  if (tokens.length > relay.terms().tokens.length) {
    throw new InvalidParamsError("tokens", refused("tokens-too-many"));
  }
  return tokens;
}

export async function quote(relay: Relay, params: unknown): Promise<unknown> {
  const p = object(params, "quote");
  const tokens = tokensOf(relay, p);
  const quotes = await relay.quote(quotedCall(p), tokens, payerOf(p));
  return {
    validUntilBlock: hexQuantity(quotes.validUntilBlock),
    quotes: quotes.quotes.map(tokenQuote),
    ...(quotes.gas === undefined ? {} : { gas: hexQuantity(quotes.gas) }),
  };
}

/** A sponsorship's quote in each token named, as a call's: what its account is to sign. */
export async function quoteSponsorship(relay: Relay, params: unknown): Promise<unknown> {
  const p = object(params, "sponsorship");
  const tokens = tokensOf(relay, p);
  return sponsorshipQuoteResult(await relay.quoteSponsorship(askedOf(p), tokens));
}

/** A transaction filled in for its account to sign (EIP-1559's, type 2), as a wallet takes it. */
function toSignJson(t: ToSign): Record<string, unknown> {
  return {
    type: "0x2",
    chainId: hexQuantity(t.chainId),
    nonce: hexQuantity(t.nonce),
    data: hex(t.data),
    value: hexQuantity(t.value),
    gas: hexQuantity(t.gas),
    maxFeePerGas: hexQuantity(t.maxFeePerGas),
    maxPriorityFeePerGas: hexQuantity(t.maxPriorityFeePerGas),
    ...(t.to === null ? {} : { to: t.to }),
  };
}

/** A sponsorship's quote, as REST and JSON-RPC answer it (bruno/server/wire.py's
 * SponsorshipQuoteResult). */
function sponsorshipQuoteResult(quotes: SponsoredQuotes): unknown {
  return {
    validUntilBlock: hexQuantity(quotes.validUntilBlock),
    maxBlockNumber: hexQuantity(quotes.maxBlockNumber),
    forwarder: quotes.forwarder,
    transactions: quotes.transactions.map((t) => t === null ? null : toSignJson(t)),
    quotes: quotes.quotes.map((q) => ({
      token: q.token,
      amount: hexQuantity(q.amount),
      ...(q.payment === null ? {} : { payment: toSignJson(q.payment) }),
    })),
    ...(quotes.gas === null ? {} : { gas: hexQuantity(quotes.gas) }),
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
  if (!HASH.test(opHash)) {
    throw new InvalidParamsError(
      "userOpHash",
      refused("malformed", { why: message("hash-malformed") }),
    );
  }
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

/** Take the sponsorship in `params`: its id, and whether it's new. */
export async function sponsor(relay: Relay, params: unknown): Promise<Accepted> {
  return await relay.sponsor(sponsorship(params));
}

/** What's become of a submission, and its stage: the latest and finalized blocks read for one
 * that's included, at most once a slot. */
export async function statusOf(
  relay: Relay,
  id: string,
): Promise<{ stage: Stage } & Record<string, unknown>> {
  if (!HASH.test(id)) {
    throw new InvalidParamsError("id", refused("malformed", { why: message("hash-malformed") }));
  }
  const t: Tracked | Finished = relay.queue.status(id as Hex);
  if ("submission" in t) {
    return { status: t.status, stage: stage(t, null), attempts: hexQuantity(t.attempts) };
  }
  const final = t.status === "included" ? await relay.finality() : null;
  return {
    status: t.status,
    stage: stage(t, final === null ? null : final.finalized),
    attempts: hexQuantity(t.attempts),
    ...(t.transactionHashes.length === 0 ? {} : { transactionHashes: t.transactionHashes }),
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
  readonly refused: Refused;

  constructor(readonly chain: string) {
    const why = refused("not-served", { chain });
    super(String(why));
    this.refused = why;
  }
}

/** An x402 request to a chain that takes no x402 payment. */
export class NoX402Error extends Error {
  readonly refused: Refused;

  constructor(chainId: number) {
    const why = refused("no-x402", { chain: chainId });
    super(String(why));
    this.refused = why;
  }
}

/** What a node failing a request is answered with, the relay unavailable for now, to try again
 * (no answer, a page, an error that isn't a revert): what failed logged, not answered. */
export function nodeFailed(e: Error): ErrorObject {
  console.warn(`a node failed: ${e.message}`);
  const data = errorDataOf("node-failed");
  return { code: Code.UNAVAILABLE, message: message("node-failed"), data };
}

export function errorObject(e: unknown): ErrorObject {
  const message = e instanceof Error ? e.message : String(e);
  if (e instanceof InvalidSubmissionError || e instanceof InvalidUserOperationError) {
    const data = invalidData(e.refused.refusal, e.field, e.bound);
    return { code: Code.INVALID_PARAMS, message, data };
  }
  if (e instanceof InvalidParamsError) {
    return { code: Code.INVALID_PARAMS, message, data: invalidData(e.refused.refusal, e.field) };
  }
  if (e instanceof UnderpaidError) {
    const data: ErrorData = {
      reason: e.refused.refusal,
      token: e.token,
      required: hexQuantity(e.required),
    };
    return { code: Code.UNDERPAID, message, data };
  }
  if (e instanceof SimulationFailedError) {
    const data: ErrorData = { reason: e.refused.refusal, revertData: hex(e.revertData) };
    return { code: Code.SIMULATION_FAILED, message, data };
  }
  if (e instanceof UnsupportedError) {
    return { code: Code.UNSUPPORTED, message, data: errorDataOf(e.refused.refusal) };
  }
  if (e instanceof NoCapacityError) {
    const data: ErrorData = {
      reason: e.refused.refusal,
      token: e.token,
      required: hexQuantity(e.required),
    };
    return { code: Code.NO_CAPACITY, message, data };
  }
  if (e instanceof NotServedError) {
    const data: ErrorData = { reason: e.refused.refusal, chain: e.chain };
    return { code: Code.METHOD_NOT_FOUND, message, data };
  }
  if (e instanceof NoX402Error) {
    const data: ErrorData = { reason: e.refused.refusal };
    return { code: Code.METHOD_NOT_FOUND, message, data };
  }
  if (e instanceof UnknownSubmissionError) {
    const data: ErrorData = { reason: e.refused.refusal };
    return { code: Code.UNKNOWN_SUBMISSION, message, data };
  }
  if (e instanceof UnavailableError) {
    return { code: Code.UNAVAILABLE, message, data: errorDataOf(e.refused.refusal) };
  }
  if (e instanceof ChainError || e instanceof JsonRpcError) return nodeFailed(e);
  // what failed stays in the log: a node's URL, its key in it, isn't the client's to see
  console.error(e);
  return { code: Code.INTERNAL_ERROR, message: "internal error" };
}
