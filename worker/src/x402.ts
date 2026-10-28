/**
 * x402 over HTTP (x402 v2, its HTTP transport, and `exact` on EVM with EIP-3009): a call paid
 * for by a signed transferWithAuthorization, which the runner makes in the same transaction as
 * the call. A port of the Python reference (bruno/server/x402.py): its challenge's description,
 * tags and bazaar extension, and its OpenAPI operation, are the Python's (make vectors).
 */

import { type Answer, hexOf, quantity, resultOf } from "./chain.ts";
import { encodeBytes, word } from "./abi.ts";
import { concatBytes } from "@noble/hashes/utils.js";
import {
  inner,
  signerOf,
  type TransferAuthorization,
  transferDeployment,
  transferDigest,
} from "./eip3009.ts";
import { balanceOfCall } from "./tokens.ts";
import { type Address, checksum, hasCode, type Hex, hex, keccak, utf8 } from "./evm.ts";
import { chainPath } from "./front.ts";
import type { Quotes, Relay } from "./relay.ts";
import {
  authorities,
  checkAuthorizations,
  checkedSubmission,
  InvalidSubmissionError,
  type Submission,
  submissionId,
} from "./submission.ts";
import {
  InvalidParams,
  type PaymentPayload,
  paymentPayload,
  type X402Call,
  x402Params,
} from "./wire.ts";
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

/** A PAYMENT-SIGNATURE header that isn't a payment payload. */
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
 * A PAYMENT-SIGNATURE header's payment payload, as the Python reference decodes it: base64 of
 * its JSON, strictly (padded, the base64 alphabet only, UTF-8), the accepted requirement whole,
 * the optional parts typed, its authorization's numbers each a uint256.
 */
export function payloadOf(header: string): PaymentPayload {
  let raw: unknown;
  try {
    if (!BASE64.test(header)) throw new Error("not base64");
    const binary = Uint8Array.from(atob(header), (c) => c.charCodeAt(0));
    raw = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(binary));
  } catch (e) {
    throw new MalformedPaymentError(`PAYMENT-SIGNATURE: ${e}`);
  }
  let payload: PaymentPayload;
  try {
    payload = paymentPayload(raw);
  } catch (e) {
    if (e instanceof InvalidParams) {
      throw new MalformedPaymentError(`PAYMENT-SIGNATURE: ${e.message}`);
    }
    throw e;
  }
  for (
    const [name, value] of Object.entries({
      value: payload.authorization.value,
      validAfter: payload.authorization.validAfter,
      validBefore: payload.authorization.validBefore,
    })
  ) {
    if (value > MAX_UINT256) {
      throw new MalformedPaymentError(`PAYMENT-SIGNATURE: ${name} isn't a uint256`);
    }
  }
  return payload;
}

/** The call in an x402 request's body, as the Python reference decodes it: null if there's
 * none, or it doesn't parse. */
export function paramsOf(body: string): X402Call | null {
  try {
    return x402Params(JSON.parse(body));
  } catch {
    return null;
  }
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
      {},
      true,
    );
  } else {
    // checked as the submission's will be when it's taken
    checkAuthorizations(call.authorizations, relay.chain.chainId, relay.config.maxAuthorizations);
    const authority = authorities(call.authorizations.slice(0, 1))[0] ?? null;
    quotes = await relay.x402Quote(call.gas, call.call.data.length, authority, call.payer);
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
    contract = hasCode(hexOf(resultOf(deployed)));
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
const IS_VALID_SIGNATURE = keccak(utf8("isValidSignature(bytes32,bytes)")).slice(0, 4);

/** eth_call of `account`'s ERC-1271 isValidSignature(hash, signature). */
export function isValidSignatureCall(account: Address, hash: Uint8Array, signature: Uint8Array) {
  const data = hex(concatBytes(IS_VALID_SIGNATURE, hash, word(64n), encodeBytes(signature)));
  return { method: "eth_call", params: [{ to: account, data }, "latest"] };
}

/** Whether an isValidSignature answer is ERC-1271's magic value: its first four bytes. */
function magic(result: unknown): boolean {
  return typeof result === "string" && result.length >= 10 &&
    result.slice(0, 10).toLowerCase() === hex(IS_VALID_SIGNATURE);
}

/** The EIP-3009 transfer `payload` authorizes, its signature as signed. */
export function transferOf(payload: PaymentPayload): TransferAuthorization {
  const a = payload.authorization;
  return {
    payer: a.from,
    to: a.to,
    value: a.value,
    validAfter: a.validAfter,
    validBefore: a.validBefore,
    nonce: a.nonce,
    signature: payload.signature,
  };
}

/** The paid submission `payload` makes of `call`, checked against what's asked: a
 * PaymentRefusedError why not. */
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
  const method = accepted.assetTransferMethod;
  const flow = accepted.paymentFlow;
  if (
    (method !== null && method !== ASSET_TRANSFER_METHOD) ||
    (flow !== null && flow !== PAYMENT_FLOW)
  ) {
    throw new PaymentRefusedError(
      INVALID_REQUIREMENTS,
      `only ${ASSET_TRANSFER_METHOD}, ${PAYMENT_FLOW} is taken`,
    );
  }
  // exact: the transfer is of the amount accepted, no more and no less
  const amount = accepted.amount;
  if (!/^[0-9]+$/.test(amount) || BigInt(amount) !== a.value) {
    throw new PaymentRefusedError(VALUE_MISMATCH, `value isn't the amount ${amount}`);
  }
  const transfer = transferOf(payload);
  let deadline: number;
  try {
    deadline = await relay.x402Deadline(transfer.validAfter, transfer.validBefore);
  } catch (e) {
    if (!(e instanceof InvalidSubmissionError)) throw e;
    throw new PaymentRefusedError(e.field === "validAfter" ? NOT_YET_VALID : EXPIRED, e.message);
  }
  try {
    return checkedSubmission({
      call: { to: call.call.to, data: call.call.data, value: 0n },
      authorizations: call.authorizations,
      gas: call.gas,
      token,
      payee: a.to,
      amount: transfer.value,
      maxBlockNumber: deadline,
      transfer,
      ...(call.payer.deployment === undefined ? {} : { deployment: call.payer.deployment }),
    });
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
  url: string,
  transaction: Hex,
): unknown {
  return {
    success: true,
    transaction,
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
