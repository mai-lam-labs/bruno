/**
 * x402 over HTTP (x402 v2, its HTTP transport, and `exact` on EVM with EIP-3009): a call paid
 * for by a signed transferWithAuthorization, which the runner makes in the same transaction as
 * the call. A port of the Python reference (bruno/server/x402.py): its challenge's description,
 * tags and bazaar extension, and its OpenAPI operation, are the Python's (make vectors).
 */

import type { TransferAuthorization } from "./eip3009.ts";
import { type Address, bytes, checksum, type Hex, hex } from "./evm.ts";
import { chainPath } from "./front.ts";
import type { Quotes, Relay } from "./relay.ts";
import { InvalidSubmissionError, type Submission, submissionId } from "./submission.ts";
import { authority as recovered, type SignedAuthorization } from "./transactions.ts";
import openrpcDocument from "./openrpc.json" with { type: "json" };
import exportedDocument from "./x402.json" with { type: "json" };

export const X402_VERSION = 2;
const SCHEME = "exact";
const TITLE = openrpcDocument.info.title;
/** x402 v2's error codes (§9). */
export const INVALID_VERSION = "invalid_x402_version";
export const INVALID_SCHEME = "invalid_scheme";
export const INVALID_NETWORK = "invalid_network";
export const INVALID_REQUIREMENTS = "invalid_payment_requirements";
export const RECIPIENT_MISMATCH = "invalid_exact_evm_payload_recipient_mismatch";
export const VALUE_MISMATCH = "invalid_exact_evm_payload_authorization_value_mismatch";
export const NOT_YET_VALID = "invalid_exact_evm_payload_authorization_valid_after";
export const EXPIRED = "invalid_exact_evm_payload_authorization_valid_before";
export const TRANSACTION_STATE = "invalid_transaction_state";

const ADDRESS = /^0x[0-9a-fA-F]{40}$/;
const DECIMAL = /^(0|[1-9][0-9]*)$/;
const NONCE = /^0x[0-9a-fA-F]{64}$/;
const SIGNATURE = /^0x[0-9a-fA-F]{130}$/;

/** A payment that isn't taken, with x402's code for why (`reason`). */
export class PaymentRefusedError extends Error {
  constructor(readonly reason: string, message: string) {
    super(message);
  }
}

/** A PAYMENT-SIGNATURE header that isn't a payment payload. */
export class MalformedPaymentError extends Error {}

/** The call an x402 payment is for. */
export interface X402Call {
  readonly call: { readonly to: Address; readonly data: Uint8Array };
  readonly gas: number;
  readonly authorizations: readonly SignedAuthorization[];
}

/** What a client pays with: the requirement it accepted, its authorization and signature. */
export interface PaymentPayload {
  readonly x402Version: number;
  readonly accepted: {
    readonly scheme: string;
    readonly network: string;
    readonly asset: Address;
    readonly payTo: Address;
  };
  readonly authorization: {
    readonly from: Address;
    readonly to: Address;
    readonly value: bigint;
    readonly validAfter: bigint;
    readonly validBefore: bigint;
    readonly nonce: Uint8Array;
  };
  readonly signature: Uint8Array;
}

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

/** A PAYMENT-SIGNATURE header's payment payload: base64 of its JSON. */
export function payloadOf(header: string): PaymentPayload {
  let raw: unknown;
  try {
    const binary = atob(header);
    raw = JSON.parse(new TextDecoder().decode(Uint8Array.from(binary, (c) => c.charCodeAt(0))));
  } catch (e) {
    throw new MalformedPaymentError(`PAYMENT-SIGNATURE: ${e}`);
  }
  const p = record(raw, "payment");
  const accepted = record(p.accepted, "accepted");
  const payload = record(p.payload, "payload");
  const a = record(payload.authorization, "payload.authorization");
  if (typeof p.x402Version !== "number") throw new MalformedPaymentError("x402Version");
  return {
    x402Version: p.x402Version,
    accepted: {
      scheme: text(accepted.scheme, "accepted.scheme"),
      network: text(accepted.network, "accepted.network"),
      asset: addressOf(accepted.asset, "accepted.asset"),
      payTo: addressOf(accepted.payTo, "accepted.payTo"),
    },
    authorization: {
      from: addressOf(a.from, "authorization.from"),
      to: addressOf(a.to, "authorization.to"),
      value: decimal(a.value, "authorization.value"),
      validAfter: decimal(a.validAfter, "authorization.validAfter"),
      validBefore: decimal(a.validBefore, "authorization.validBefore"),
      nonce: bytes(matching(a.nonce, NONCE, "authorization.nonce") as Hex),
    },
    signature: bytes(matching(payload.signature, SIGNATURE, "payload.signature") as Hex),
  };
}

/** What to pay for `call` (a reference call's price without one): an `exact` requirement in
 * each token accepted over x402. `least`: a token's amount the payment must be at least. */
export async function challenge(
  relay: Relay,
  url: string,
  call: X402Call | null,
  error: string,
  least: { token: Address; amount: bigint } | null = null,
): Promise<unknown> {
  let quotes: Quotes;
  if (call === null) {
    const reference = exportedDocument.reference;
    quotes = await relay.x402Quote(
      reference.gas,
      reference.callBytes,
      checksum(reference.authority),
      true,
    );
  } else {
    let authority: Address | null = null;
    const first = call.authorizations[0];
    if (first !== undefined) {
      try {
        authority = recovered(first);
      } catch (e) {
        throw new InvalidSubmissionError("authorizationList", `unrecoverable: ${e}`);
      }
    }
    quotes = await relay.x402Quote(call.gas, call.call.data.length, authority);
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
    accepts: quotes.quotes.map((q) => {
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
    }),
    error,
    extensions: { bazaar: exportedDocument.bazaar },
  };
}

/** The paid submission `payload` makes of `call`, checked against what's asked. */
export async function take(
  relay: Relay,
  call: X402Call,
  payload: PaymentPayload,
): Promise<Submission> {
  if (payload.x402Version !== X402_VERSION) {
    throw new PaymentRefusedError(INVALID_VERSION, `x402Version ${payload.x402Version}`);
  }
  const accepted = payload.accepted;
  if (accepted.scheme !== SCHEME) {
    throw new PaymentRefusedError(INVALID_SCHEME, `scheme ${accepted.scheme}`);
  }
  if (accepted.network !== network(relay.chain.chainId)) {
    throw new PaymentRefusedError(INVALID_NETWORK, `network ${accepted.network}`);
  }
  const token = accepted.asset;
  if (!relay.domains.has(token)) {
    throw new PaymentRefusedError(INVALID_REQUIREMENTS, `${token} isn't accepted over x402`);
  }
  const a = payload.authorization;
  if (a.to !== relay.payee || accepted.payTo !== relay.payee) {
    throw new PaymentRefusedError(RECIPIENT_MISMATCH, `the payee is ${relay.payee}`);
  }
  const v = payload.signature[64] as number;
  const transfer: TransferAuthorization = {
    payer: a.from,
    to: a.to,
    value: a.value,
    validAfter: a.validAfter,
    validBefore: a.validBefore,
    nonce: a.nonce,
    v: v >= 27 ? v : v + 27,
    r: BigInt(hex(payload.signature.slice(0, 32))),
    s: BigInt(hex(payload.signature.slice(32, 64))),
  };
  let deadline: number;
  try {
    deadline = await relay.x402Deadline(transfer.validAfter, transfer.validBefore);
  } catch (e) {
    if (!(e instanceof InvalidSubmissionError)) throw e;
    throw new PaymentRefusedError(e.field === "validAfter" ? NOT_YET_VALID : EXPIRED, e.message);
  }
  return {
    call: { to: call.call.to, data: call.call.data, value: 0n },
    authorizations: call.authorizations,
    gas: call.gas,
    token,
    payee: a.to,
    amount: transfer.value,
    maxBlockNumber: deadline,
    transfer,
  };
}

/** A payment taken: acknowledged, nothing broadcast yet. */
export function settled(relay: Relay, submission: Submission, url: string): unknown {
  return {
    success: true,
    transaction: "",
    network: network(relay.chain.chainId),
    ...(submission.transfer === undefined ? {} : { payer: submission.transfer.payer }),
    amount: submission.amount.toString(),
    extensions: {
      status: {
        info: { url, id: hex(submissionId(submission)) },
        schema: exportedDocument.statusSchema,
      },
    },
  };
}

export function refused(relay: Relay, reason: string, payer: Address | null): unknown {
  return {
    success: false,
    transaction: "",
    network: network(relay.chain.chainId),
    errorReason: reason,
    ...(payer === null ? {} : { payer }),
  };
}

/** The OpenAPI operation, its minimum price `minUsd` if known, and the schemas it refers to. */
export function openapiOperation(minUsd: string | null): { operation: unknown; schemas: unknown } {
  const operation = structuredClone(exportedDocument.operation) as {
    post: { "x-payment-info": { price: Record<string, string> } };
  };
  if (minUsd !== null) operation.post["x-payment-info"].price.min = minUsd;
  return { operation, schemas: exportedDocument.schemas };
}

function record(raw: unknown, at: string): Record<string, unknown> {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    throw new MalformedPaymentError(`${at}: an object`);
  }
  return raw as Record<string, unknown>;
}

function text(raw: unknown, at: string): string {
  if (typeof raw !== "string") throw new MalformedPaymentError(`${at}: a string`);
  return raw;
}

function matching(raw: unknown, pattern: RegExp, at: string): string {
  if (typeof raw !== "string" || !pattern.test(raw)) {
    throw new MalformedPaymentError(`${at}: malformed`);
  }
  return raw;
}

function addressOf(raw: unknown, at: string): Address {
  return checksum(matching(raw, ADDRESS, at));
}

function decimal(raw: unknown, at: string): bigint {
  return BigInt(matching(raw, DECIMAL, at));
}
