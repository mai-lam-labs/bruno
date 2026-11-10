/**
 * x402 over HTTP (x402 v2, its HTTP transport, and `exact` on EVM with EIP-3009): a call paid
 * for by a signed transferWithAuthorization, which the runner makes in the same transaction as
 * the call. As bruno/server/x402.py's: its challenge's description, tags and
 * bazaar extension, and its OpenAPI operation, are written from it (make vectors).
 */

import { hasCode, quantity } from "./chain.ts";
import { type Answer, type Call, ethCall, resultOf } from "./jsonrpc.ts";
import { encodeBytes, selector, word } from "./abi.ts";
import { concatBytes } from "@noble/hashes/utils.js";
import {
  inner,
  signerOf,
  type TransferAuthorization,
  transferDeployment,
  transferDigest,
} from "./eip3009.ts";
import { balanceOfCall } from "./tokens.ts";
import { type Address, checksum, type Hex, hex } from "./evm.ts";
import { chainPath } from "./front.ts";
import type { Quotes, TokenQuote } from "./pricing.ts";
import type { Relay } from "./relay.ts";
import { InvalidSubmissionError, type Submission, submissionId } from "./submission.ts";
import {
  type Extension,
  InvalidParamsError,
  type PaymentPayload,
  paymentPayload,
  type PaymentRequired,
  type PaymentRequirements,
  type SettleResponse,
  type X402Params,
  x402Params,
  x402Quoted,
  type X402Result,
  x402Submission,
} from "./wire.ts";
import openrpcDocument from "./openrpc.json" with { type: "json" };
import exportedDocument from "./x402.json" with { type: "json" };
import { message } from "./refusals.ts";

export const X402_VERSION = 2;
const SCHEME = "exact";
const TITLE = openrpcDocument.info.title;
/** x402 v2's error codes (§9). */
export const INVALID_PAYLOAD = "invalid_payload";
export const INVALID_VERSION = "invalid_x402_version";
export const INVALID_SCHEME = "invalid_scheme";
export const INVALID_NETWORK = "invalid_network";
export const INVALID_REQUIREMENTS = "invalid_payment_requirements";
export const RECIPIENT_MISMATCH = "invalid_exact_evm_payload_recipient_mismatch";
export const VALUE_MISMATCH = "invalid_exact_evm_payload_authorization_value_mismatch";
export const NOT_YET_VALID = "invalid_exact_evm_payload_authorization_valid_after";
export const EXPIRED = "invalid_exact_evm_payload_authorization_valid_before";
export const TRANSACTION_STATE = "invalid_transaction_state";
export const INSUFFICIENT_FUNDS = "insufficient_funds";
export const INVALID_SIGNATURE = "invalid_exact_evm_payload_signature";
/** The only asset transfer method and payment flow taken: EIP-3009, settled with the call. */
const ASSET_TRANSFER_METHOD = "eip3009";
const PAYMENT_FLOW = "authorization";

/** Base64 as Python's b64decode(validate=True) takes it: padded, the base64 alphabet only. */
const BASE64 = /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/;
const MAX_UINT256 = (1n << 256n) - 1n;

/** A payment that isn't taken, with x402's code for why (`reason`). */
export class PaymentRefusedError extends Error {
  constructor(readonly reason: string, message: string) {
    super(message);
  }
}

/** A PAYMENT-SIGNATURE header that isn't a payment payload: why, the caller naming the header. */
export class MalformedPaymentError extends Error {}

export function network(chainId: number): string {
  return `eip155:${chainId}`;
}

export function x402Url(origin: string, chainId: number): string {
  return `${origin}/${chainPath(chainId)}/v1/x402`;
}

export function statusUrl(origin: string, chainId: number, id: Hex): string {
  return `${origin}/${chainPath(chainId)}/v1/submissions/${id}`;
}

/** A header's value: base64 of the JSON. */
export function encoded(value: unknown): string {
  const utf8 = new TextEncoder().encode(JSON.stringify(value));
  return btoa(Array.from(utf8, (b) => String.fromCharCode(b)).join(""));
}

/**
 * A PAYMENT-SIGNATURE header's payment payload, as bruno/server/x402.py decodes it: base64 of
 * its JSON, strictly (padded, the base64 alphabet only, UTF-8), its authorization's numbers each
 * a uint256.
 */
export function payloadOf(header: string): PaymentPayload {
  let raw: unknown;
  try {
    if (!BASE64.test(header)) throw new Error("not base64");
    const binary = Uint8Array.from(atob(header), (c) => c.charCodeAt(0));
    raw = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(binary));
  } catch (e) {
    throw new MalformedPaymentError(e instanceof Error ? e.message : String(e));
  }
  let payload: PaymentPayload;
  try {
    payload = paymentPayload(raw);
  } catch (e) {
    if (e instanceof InvalidParamsError) {
      throw new MalformedPaymentError(e.message);
    }
    throw e;
  }
  const authorization = payload.payload.authorization;
  for (
    const [name, value] of Object.entries({
      value: authorization.value,
      validAfter: authorization.validAfter,
      validBefore: authorization.validBefore,
    })
  ) {
    if (value > MAX_UINT256) {
      throw new MalformedPaymentError(message("x402-uint256", { name }));
    }
  }
  return payload;
}

/** The call in an x402 request's body, as bruno/server/x402.py decodes it: null if there's
 * none, or it doesn't parse. */
export function paramsOf(body: string): X402Params | null {
  try {
    return x402Params(JSON.parse(body));
  } catch {
    return null;
  }
}

/** What to pay for the call in `params` (a reference call's price without one): an `exact` requirement in
 * each token accepted over x402. `least`: a token's amount the payment must be at least. */
export async function challenge(
  relay: Relay,
  url: string,
  params: X402Params | null,
  error: string,
  least: TokenQuote | null = null,
): Promise<PaymentRequired> {
  let quotes: Quotes;
  if (params === null) {
    const reference = exportedDocument.reference;
    quotes = await relay.x402Quote(
      {
        call: null,
        callDataLength: reference.callBytes,
        gas: reference.gas,
        authorizations: [],
        authority: checksum(reference.authority),
      },
      {},
      true,
    );
  } else {
    // checked as the submission's will be when it's taken
    quotes = await relay.x402Quote(x402Quoted(params), params.payer);
  }
  return {
    x402Version: X402_VERSION,
    resource: {
      url,
      description: exportedDocument.description,
      mimeType: "application/json",
      serviceName: TITLE,
      tags: exportedDocument.tags,
    },
    accepts: requirements(relay, quotes, least),
    error,
    extensions: { bazaar: exportedDocument.bazaar },
  };
}

function requirements(
  relay: Relay,
  quotes: Quotes,
  least: TokenQuote | null,
): PaymentRequirements[] {
  return quotes.quotes.map((q) => {
    const amount = least !== null && least.token === q.token && least.amount > q.amount
      ? least.amount
      : q.amount;
    const domain = relay.domains.get(q.token);
    return {
      scheme: SCHEME,
      network: network(relay.chain.chainId),
      amount: amount.toString(),
      asset: q.token,
      payTo: relay.payee,
      maxTimeoutSeconds: relay.x402TimeoutSeconds,
      ...(domain === undefined ? {} : { extra: { name: domain.name, version: domain.version } }),
    };
  });
}

/**
 * x402's code for why a paid submission whose simulation failed is refused: the signature isn't
 * the payer's (a payer with no code: its key's, recovered; one with code: as its ERC-1271
 * isValidSignature answers its inner signature; one not deployed yet, its signature
 * ERC-6492-wrapped, can't be asked); the payer holds less than the value; else the transaction's
 * state. Read only once it has failed, in one batch: a payment that succeeds reads nothing for
 * it.
 */
export async function refusal(relay: Relay, submission: Submission): Promise<string> {
  const transfer = submission.transfer;
  if (transfer === undefined) return TRANSACTION_STATE;
  const domain = relay.domains.get(submission.token);
  if (domain === undefined) return TRANSACTION_STATE;
  const hash = transferDigest(domain, relay.chain.chainId, submission.token, transfer);
  let held: bigint;
  let contract: boolean;
  let answer: Answer | undefined;
  try {
    const [balance, deployed, checked] = await relay.chain.batch([
      balanceOfCall(submission.token, transfer.payer),
      { method: "eth_getCode", params: [transfer.payer, "latest"] },
      isValidSignatureCall(transfer.payer, hash, inner(transfer)),
    ]);
    if (balance === undefined || deployed === undefined) return TRANSACTION_STATE;
    held = quantity(resultOf(balance));
    contract = hasCode(resultOf(deployed));
    answer = checked;
  } catch {
    return TRANSACTION_STATE; // unread: the failure as it is
  }
  // not deployed: nothing to ask without deploying it
  const valid = contract
    ? answer !== undefined && "result" in answer && magic(answer.result)
    : transferDeployment(transfer) !== null ||
      signerOf(domain, relay.chain.chainId, submission.token, transfer) === transfer.payer;
  if (!valid) return INVALID_SIGNATURE;
  if (held < transfer.value) return INSUFFICIENT_FUNDS;
  return TRANSACTION_STATE;
}

/** ERC-1271's isValidSignature(bytes32, bytes), and what it answers for a valid signature. */
const IS_VALID_SIGNATURE = selector("isValidSignature(bytes32,bytes)");

/** eth_call of `account`'s ERC-1271 isValidSignature(hash, signature). */
export function isValidSignatureCall(
  account: Address,
  hash: Uint8Array,
  signature: Uint8Array,
): Call {
  return ethCall(account, concatBytes(IS_VALID_SIGNATURE, hash, word(64n), encodeBytes(signature)));
}

/** Whether an isValidSignature answer is ERC-1271's magic value: its first four bytes. */
function magic(result: unknown): boolean {
  return typeof result === "string" && result.length >= 10 &&
    result.slice(0, 10).toLowerCase() === hex(IS_VALID_SIGNATURE);
}

/** The EIP-3009 transfer `payload` authorizes, its signature as signed. */
export function paymentTransfer(payload: PaymentPayload): TransferAuthorization {
  const a = payload.payload.authorization;
  return {
    payer: a.from,
    to: a.to,
    value: a.value,
    validAfter: a.validAfter,
    validBefore: a.validBefore,
    nonce: a.nonce,
    signature: payload.payload.signature,
  };
}

/** The paid submission `payload` makes of the call in `params`, checked against what's asked: a
 * PaymentRefusedError why not. */
export async function take(
  relay: Relay,
  params: X402Params,
  payload: PaymentPayload,
): Promise<Submission> {
  if (payload.x402Version !== X402_VERSION) {
    throw new PaymentRefusedError(
      INVALID_VERSION,
      message("x402-version", { version: payload.x402Version }),
    );
  }
  const accepted = payload.accepted;
  if (accepted.scheme !== SCHEME) {
    throw new PaymentRefusedError(
      INVALID_SCHEME,
      message("x402-scheme", { scheme: accepted.scheme }),
    );
  }
  if (accepted.network !== network(relay.chain.chainId)) {
    throw new PaymentRefusedError(
      INVALID_NETWORK,
      message("x402-network", {
        network: accepted.network,
        expected: network(relay.chain.chainId),
      }),
    );
  }
  const token = accepted.asset;
  if (!relay.domains.has(token)) {
    throw new PaymentRefusedError(INVALID_REQUIREMENTS, message("x402-token", { token }));
  }
  const authorization = payload.payload.authorization;
  const payee = authorization.to;
  if (payee !== relay.payee || accepted.payTo !== relay.payee) {
    throw new PaymentRefusedError(
      RECIPIENT_MISMATCH,
      message("x402-payee", { payee: relay.payee }),
    );
  }
  const method = accepted.extra?.assetTransferMethod;
  const flow = accepted.extra?.paymentFlow;
  if (
    (method !== undefined && method !== ASSET_TRANSFER_METHOD) ||
    (flow !== undefined && flow !== PAYMENT_FLOW)
  ) {
    throw new PaymentRefusedError(
      INVALID_REQUIREMENTS,
      message("x402-method", { method: ASSET_TRANSFER_METHOD, flow: PAYMENT_FLOW }),
    );
  }
  // exact: the transfer is of the amount accepted, no more and no less
  const amount = accepted.amount;
  if (!/^[0-9]+$/.test(amount) || BigInt(amount) !== authorization.value) {
    throw new PaymentRefusedError(VALUE_MISMATCH, message("x402-value", { amount }));
  }
  const transfer = paymentTransfer(payload);
  let deadline: number;
  try {
    deadline = await relay.x402Deadline(transfer.validAfter, transfer.validBefore);
  } catch (e) {
    if (!(e instanceof InvalidSubmissionError)) throw e;
    throw new PaymentRefusedError(e.field === "validAfter" ? NOT_YET_VALID : EXPIRED, e.message);
  }
  // its estimate again, if it has no gas of its own: one now dearer is underpaid, refused
  const gas = params.gas ?? await relay.x402Gas(x402Quoted(params), params.payer);
  try {
    return x402Submission(params, token, payee, deadline, transfer, gas);
  } catch (e) {
    // the request's own (its deployment), not the payment's
    if (!(e instanceof InvalidSubmissionError) || e.field !== "payment") throw e;
    throw new PaymentRefusedError(INVALID_SIGNATURE, e.message);
  }
}

/** A payment settled: its submission landed, in `transaction`. */
export function settled(
  relay: Relay,
  submission: Submission,
  statusUrl: string,
  transaction: Hex,
): SettleResponse {
  return {
    success: true,
    transaction,
    network: network(relay.chain.chainId),
    ...(submission.transfer === undefined ? {} : { payer: submission.transfer.payer }),
    amount: submission.amount.toString(),
    extensions: { status: statusExtension(statusUrl, hex(submissionId(submission))) },
  };
}

/** Where a taken payment's submission is followed: its status URL and id. */
export function statusExtension(url: string, id: Hex): Extension {
  return { info: { url, id }, schema: exportedDocument.statusSchema };
}

export function refused(relay: Relay, reason: string, payer: Address | null): SettleResponse {
  return {
    success: false,
    transaction: "",
    network: network(relay.chain.chainId),
    errorReason: reason,
    ...(payer === null ? {} : { payer }),
  };
}

/** A paid submission taken: its id, and where its status is. */
export function result(origin: string, relay: Relay, submission: Submission): X402Result {
  const id = hex(submissionId(submission));
  return { id, statusUrl: statusUrl(origin, relay.chain.chainId, id) };
}

/** The OpenAPI operation, its minimum price `minUsd` if known, and the schemas it refers to. */
export function openapiOperation(minUsd: string | null): { operation: unknown; schemas: unknown } {
  const operation = structuredClone(exportedDocument.operation) as {
    post: { "x-payment-info": { price: Record<string, string> } };
  };
  if (minUsd !== null) operation.post["x-payment-info"].price.min = minUsd;
  return { operation, schemas: exportedDocument.schemas };
}
