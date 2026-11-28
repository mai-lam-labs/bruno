/**
 * The interface's requests as read: each field checked as bruno/server/wire.py's types
 * declare it, a field that isn't refused as InvalidParamsError; and JSON-RPC's
 * error codes. REST, JSON-RPC and x402 read through the same checks.
 */

import {
  type Address,
  bytes,
  checksum,
  type EncodedCall,
  type Hex,
  hex,
  hexQuantity,
} from "./evm.ts";
import { type Asked, checkedAsked, type Unsigned } from "./sponsoring.ts";
import { checkedSponsorship, type Sponsorship } from "./sponsorship.ts";
import type { TransferAuthorization, UnsignedTransfer } from "./eip3009.ts";
import type { Deployment } from "./erc6492.ts";
import type { Payer, Quoted, QuotedCall } from "./pricing.ts";
import {
  type Bound,
  checkedSubmission,
  checkSigned,
  InvalidSubmissionError,
  type Submission,
  type SubmittedCall,
} from "./submission.ts";
import {
  type Decoded,
  decoded,
  type SignedAuthorization,
  TransactionError,
} from "./transactions.ts";
import {
  EIP7702_MARKER,
  entryPointAt,
  InvalidUserOperationError,
  type Paymaster,
  type UserOperation,
} from "./userops.ts";
import { type Category, categoryOf, type Refusal, type Refused, refused } from "./refusals.ts";

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
  UNAVAILABLE: -32015,
} as const;

/** The longest signature a payment carries, in bytes: a key's is 65, a contract's (a passkey's
 * WebAuthn assertion, say) longer, an ERC-6492 wrapper longer still, never near this. */
export const MAX_SIGNATURE_BYTES = 4096;

const QUANTITY = /^0x(0|[1-9a-f][0-9a-f]*)$/;
/** A uint256's: "0x" and at most 64 digits. */
const QUANTITY_MAX_LENGTH = 66;
const ADDRESS = /^0x[0-9a-fA-F]{40}$/;
const DATA = /^0x([0-9a-fA-F]{2})*$/;
export const HASH = /^0x[0-9a-f]{64}$/;
const BYTES32 = /^0x[0-9a-fA-F]{64}$/;
/** A number of at most a word, as hex, leading zeros allowed: a signature's r, s and yParity as
 * clients write them, padded (viem's eip7702Auth) or not. */
const WORD = /^0x[0-9a-fA-F]{1,64}$/;
/** An op's factory: an address, or EntryPoint v0.8's EIP-7702 marker (ERC-7769). */
const EIP7702_FACTORY = "0x7702";
const DECIMAL = /^(0|[1-9][0-9]{0,77})$/;
const SIGNATURE = new RegExp(`^0x(?:[0-9a-fA-F]{2}){1,${MAX_SIGNATURE_BYTES}}$`);
const UINT256 = 1n << 256n;
const SAFE = BigInt(Number.MAX_SAFE_INTEGER) + 1n;

/** A request's field that isn't what its type says: the field, and why. */
export class InvalidParamsError extends Error {
  constructor(readonly field: string, readonly refused: Refused) {
    super(`${field}: ${refused}`);
  }
}

/** JSON-RPC's error object; a REST error's body is {"error": this}. Every error the relay raises
 * has its data, its refusal its `reason`; JSON-RPC's own (a parse error, a method unknown) and an
 * internal error have none. */
export interface ErrorObject {
  code: number;
  message: string;
  data?: ErrorData;
}

/** An error's data: why (`reason`, its refusal), and what its code adds: for invalid params or an
 * Invalid Request, the field at fault, as a path into the request's parameter (`call.to`,
 * `authorizationList[0]`), named as the interface names it over REST and JSON-RPC alike, and if
 * it's over a limit, the `most` it may be and what it `needs` (hex quantities), where that isn't
 * what was given; for Unsupported or Unavailable, who can act on it (`category`, the refusal's);
 * for underpaid or no capacity, what's required in `token` to be taken; for a simulation failed,
 * the revert data whole (`reason` the revert's); for a chain not served, the chain, as the path
 * names it (bruno/server/wire.py's ErrorData). */
export interface ErrorData {
  readonly reason: Refusal;
  readonly field?: string;
  readonly most?: string;
  readonly needs?: string;
  readonly category?: Category;
  readonly token?: Address;
  readonly required?: string;
  readonly revertData?: Hex;
  readonly chain?: string;
}

/** Invalid params naming `field`, and the limit it's over (`bound`), if that's why
 * (bruno/server/wire.py's ErrorData.invalid). */
export function invalidData(reason: Refusal, field: string, bound?: Bound): ErrorData {
  if (bound === undefined) return { reason, field };
  return {
    reason,
    field,
    most: hexQuantity(bound.most),
    ...(bound.needs === undefined ? {} : { needs: hexQuantity(bound.needs) }),
  };
}

/** An Unsupported or Unavailable error's: `reason`, and who can act on it
 * (bruno/server/wire.py's ErrorData.of). */
export function errorDataOf(reason: Refusal): ErrorData {
  return { reason, category: categoryOf(reason) };
}

/** An invalid params or Invalid Request error naming `field`, its type or shape wrong: the
 * decoder's own words (`malformed`). */
export function invalid(code: number, field: string, why: string): ErrorObject {
  return { code, message: `${field}: ${why}`, data: invalidData("malformed", field) };
}

/** The calls an x402 payment is for, a call's gas estimated if not given; and, as for a quote,
 * what's known of its signer. */
export interface X402Params {
  readonly calls: readonly { readonly call: EncodedCall; readonly gas: number | null }[];
  readonly authorizations: readonly SignedAuthorization[];
  readonly payer: Payer;
}

// x402 v2 (github.com/coinbase/x402, specs/x402-specification-v2.md), as bruno/server/wire.py's:
// its own nesting and field names, its amounts and times decimal strings on the wire

/**
 * `exact`'s `extra` on EVM: the token's EIP-712 domain, which the client signs for; and the
 * protocol's reserved keys, as a client may echo them: how the payment moves (EIP-3009 by
 * default) and when it settles (`authorization` by default).
 */
export interface TokenDomain {
  readonly name: string;
  readonly version: string;
  readonly assetTransferMethod?: string;
  readonly paymentFlow?: string;
}

/** What's asked for a call, in one token: what a challenge offers, and a payment accepts. */
export interface PaymentRequirements {
  readonly scheme: string;
  readonly network: string;
  readonly amount: string;
  readonly asset: Address;
  readonly payTo: Address;
  readonly maxTimeoutSeconds: number;
  readonly extra?: TokenDomain;
}

export interface ResourceInfo {
  readonly url: string;
  readonly description?: string;
  readonly mimeType?: string;
  readonly serviceName?: string;
  readonly tags?: readonly string[];
}

/** An extension's data, and the JSON Schema it's validated by. */
export interface Extension {
  readonly info: Record<string, unknown>;
  readonly schema: Record<string, unknown>;
}

export interface PaymentRequired {
  readonly x402Version: number;
  readonly resource: ResourceInfo;
  readonly accepts: readonly PaymentRequirements[];
  readonly error?: string;
  readonly extensions?: Record<string, Extension>;
}

/** An EIP-3009 authorization's fields, as `exact` on EVM carries them. */
export interface Eip3009Authorization {
  readonly from: Address;
  readonly to: Address;
  readonly value: bigint;
  readonly validAfter: bigint;
  readonly validBefore: bigint;
  readonly nonce: Uint8Array;
}

/**
 * `exact`'s payload on EVM: the authorization, and its signature: a key's 65 bytes, a contract
 * payer's (ERC-1271), or ERC-6492-wrapped, as x402's reference facilitator takes them.
 */
export interface ExactEvmPayload {
  readonly signature: Uint8Array;
  readonly authorization: Eip3009Authorization;
}

/** What a client pays with: the requirement it accepted, and its payload. */
export interface PaymentPayload {
  readonly x402Version: number;
  readonly accepted: PaymentRequirements;
  readonly payload: ExactEvmPayload;
  readonly resource?: ResourceInfo;
  readonly extensions?: Record<string, unknown>;
}

export interface SettleResponse {
  readonly success: boolean;
  readonly transaction: string;
  readonly network: string;
  readonly errorReason?: string;
  readonly payer?: Address;
  readonly amount?: string;
  readonly extensions?: Record<string, Extension>;
}

/** A paid submission taken: its id, and where its status is. */
export interface X402Result {
  readonly id: Hex;
  readonly statusUrl: string;
}

/** The signed transaction `raw` is, as hex: InvalidSubmissionError naming `field` if it isn't
 * one. */
function decodedTransaction(raw: unknown, field: string): Decoded {
  try {
    return decoded(bytes(data(raw, field)));
  } catch (e) {
    if (!(e instanceof TransactionError)) throw e;
    throw new InvalidSubmissionError(field, refused("transaction-undecodable", { why: e.message }));
  }
}

/** The fields of each request of the relay's own, and of what's its own within one, read
 * strictly (bruno/server/wire.py's Strict): one it doesn't name is refused. */
const QUOTE_FIELDS = [
  "tokens",
  "calls",
  "authorizationList",
  "authority",
  "payer",
  "deployment",
  "verifyGas",
];
const SUBMISSION_FIELDS = [
  "calls",
  "payment",
  "maxBlockNumber",
  "authorizationList",
  "deployment",
];
const X402_FIELDS = ["calls", "authorizationList", "payer", "deployment", "verifyGas"];
const SUBMITTED_CALL_FIELDS = ["to", "data", "value", "gas"];
const QUOTED_CALL_FIELDS = ["to", "data", "value", "callDataLength", "gas"];
const PAYMENT_FIELDS = ["token", "payee", "amount", "transfer"];
const TRANSFER_FIELDS = ["from", "validAfter", "validBefore", "nonce", "signature"];
const DEPLOYMENT_FIELDS = ["account", "factory", "factoryData"];
const SPONSORSHIP_QUOTE_FIELDS = [
  "tokens",
  "account",
  "transactions",
  "authorizationList",
  "calls",
  "maxBlockNumber",
];
const SPONSORSHIP_FIELDS = [
  "account",
  "payment",
  "maxBlockNumber",
  "transactions",
  "authorizationList",
  "calls",
];
const SPONSORSHIP_PAYMENT_FIELDS = ["token", "payee", "amount"];
const UNSIGNED_FIELDS = ["to", "data", "value", "gas"];

/** `o` has no field but `fields`: InvalidParamsError naming one it doesn't, `within` the path
 * to `o` in its request (as msgspec's forbid_unknown_fields refuses it). */
export function only(o: Record<string, unknown>, fields: readonly string[], within: string): void {
  for (const key of Object.keys(o)) {
    if (!fields.includes(key)) {
      throw new InvalidParamsError(
        `${within}${key}`,
        refused("malformed", { why: "an unknown field" }),
      );
    }
  }
}

/** The most calls a request may carry: each is a read or two to plan, and where they're sent
 * directly, a transaction of its bundle. */
export const MAX_CALLS = 8;

/** A request's `calls`, made in order: one at least, MAX_CALLS at most. */
function callsOf(raw: unknown): unknown[] {
  const calls = array(raw, "calls");
  if (calls.length < 1 || calls.length > MAX_CALLS) {
    const why = `a list of 1 to ${MAX_CALLS} calls`;
    throw new InvalidParamsError("calls", refused("malformed", { why }));
  }
  return calls;
}

/** A sponsorship's `calls`, which the relay makes before the account's own transactions: none,
 * if it has transactions, MAX_CALLS at most; each as a submission's. */
function sponsoredCallsOf(raw: unknown): SubmittedCall[] {
  const calls = optionalList(raw, "calls");
  if (calls.length > MAX_CALLS) {
    const why = `a list of at most ${MAX_CALLS} calls`;
    throw new InvalidParamsError("calls", refused("malformed", { why }));
  }
  return calls.map((each, index) => submittedCallOf(each, `calls[${index}]`));
}

/** One of a request's calls, at `field`: its target, its data, the ETH it sends, and its gas if
 * it gives one. */
function submittedCallOf(raw: unknown, field: string): SubmittedCall {
  const entry = object(raw, field);
  only(entry, SUBMITTED_CALL_FIELDS, `${field}.`);
  return {
    call: {
      to: address(entry.to, `${field}.to`),
      data: bytes(data(entry.data, `${field}.data`)),
      value: absent(entry.value) ? 0n : quantityOf(entry.value, `${field}.value`),
    },
    gas: absent(entry.gas) ? null : clamped(entry.gas, `${field}.gas`),
  };
}

/**
 * One of the calls a quote prices, at `field`: whole (`to` and `data`), or only its data's length
 * (`callDataLength`), both if they agree; the ETH it sends; and its gas, if given (only a call
 * given whole is estimated). InvalidSubmissionError without the call or its length, with only
 * one of `to` and `data`, or with only its length and no gas. A gas or length past a safe
 * integer is kept at the largest, past every limit.
 */
function quotedCallOf(raw: unknown, field: string): QuotedCall {
  const entry = object(raw, field);
  only(entry, QUOTED_CALL_FIELDS, `${field}.`);
  const to = absent(entry.to) ? null : address(entry.to, `${field}.to`);
  const given = absent(entry.data) ? null : bytes(data(entry.data, `${field}.data`));
  const value = absent(entry.value) ? 0n : quantityOf(entry.value, `${field}.value`);
  const length = absent(entry.callDataLength)
    ? null
    : clamped(entry.callDataLength, `${field}.callDataLength`);
  const gas = absent(entry.gas) ? null : clamped(entry.gas, `${field}.gas`);
  if ((to === null) !== (given === null)) {
    throw new InvalidSubmissionError(field, refused("quote-no-call"));
  }
  const encoded = to === null || given === null ? null : { to, data: given, value };
  if (encoded === null && length === null) {
    throw new InvalidSubmissionError(field, refused("quote-no-call"));
  }
  if (encoded === null && gas === null) {
    throw new InvalidSubmissionError(`${field}.gas`, refused("quote-no-gas"));
  }
  return { call: encoded, callDataLength: length ?? encoded?.data.length ?? 0, gas };
}

/**
 * The sponsorship a quote is asked about, as REST's body or postpaid_quoteSponsorship's
 * parameter `p`: each of its transactions signed already (its raw bytes) or still to be (an
 * object). InvalidSubmissionError with a transaction that isn't one, or anything that isn't the
 * account's.
 */
export function askedOf(p: Record<string, unknown>): Asked {
  only(p, SPONSORSHIP_QUOTE_FIELDS, "");
  return checkedAsked({
    account: address(p.account, "account"),
    authorizations: optionalList(p.authorizationList, "authorizationList").map(
      (a, i) => authorization(a, `authorizationList[${i}]`),
    ),
    calls: sponsoredCallsOf(p.calls),
    transactions: optionalList(p.transactions, "transactions").map(
      (raw, index): Decoded | Unsigned => {
        const field = `transactions[${index}]`;
        if (typeof raw === "string") return decodedTransaction(raw, field);
        const t = object(raw, field);
        only(t, UNSIGNED_FIELDS, `${field}.`);
        return {
          to: absent(t.to) ? null : address(t.to, `${field}.to`),
          data: absent(t.data) ? new Uint8Array(0) : bytes(data(t.data, `${field}.data`)),
          value: absent(t.value) ? 0n : quantityOf(t.value, `${field}.value`),
          gas: absent(t.gas) ? null : clamped(t.gas, `${field}.gas`),
        };
      },
    ),
    maxBlockNumber: absent(p.maxBlockNumber) ? null : clamped(p.maxBlockNumber, "maxBlockNumber"),
  });
}

/** A sponsorship, as REST's body or postpaid_sponsor's parameter: InvalidSubmissionError with a
 * transaction that isn't one, or an authorization or transaction that isn't the account's. */
export function sponsorship(raw: unknown): Sponsorship {
  const s = object(raw, "sponsorship");
  only(s, SPONSORSHIP_FIELDS, "");
  const payment = object(s.payment, "payment");
  only(payment, SPONSORSHIP_PAYMENT_FIELDS, "payment.");
  const transactions = optionalList(s.transactions, "transactions").map(
    (raw, index) => decodedTransaction(raw, `transactions[${index}]`),
  );
  return checkedSponsorship({
    account: address(s.account, "account"),
    authorizations: optionalList(s.authorizationList, "authorizationList").map(
      (a, i) => authorization(a, `authorizationList[${i}]`),
    ),
    calls: sponsoredCallsOf(s.calls),
    transactions,
    token: address(payment.token, "payment.token"),
    payee: address(payment.payee, "payment.payee"),
    amount: quantityOf(payment.amount, "payment.amount"),
    maxBlockNumber: clamped(s.maxBlockNumber, "maxBlockNumber"),
  });
}

/** A submission, as REST's body or postpaid_sendTransaction's parameter. */
export function submission(raw: unknown): Submission {
  const s = object(raw, "submission");
  only(s, SUBMISSION_FIELDS, "");
  const calls = callsOf(s.calls).map((raw, index) => submittedCallOf(raw, `calls[${index}]`));
  const payment = object(s.payment, "payment");
  only(payment, PAYMENT_FIELDS, "payment.");
  const authorizations = optionalList(s.authorizationList, "authorizationList").map(
    (a, i) => authorization(a, `authorizationList[${i}]`),
  );
  const payee = address(payment.payee, "payment.payee");
  const amount = quantityOf(payment.amount, "payment.amount");
  const transfer = absent(payment.transfer)
    ? undefined
    : transferOf(object(payment.transfer, "payment.transfer"), payee, amount);
  return checkedSubmission({
    calls,
    authorizations,
    token: address(payment.token, "payment.token"),
    payee,
    amount,
    maxBlockNumber: clamped(s.maxBlockNumber, "maxBlockNumber"),
    ...(transfer === undefined ? {} : { transfer }),
    ...(absent(s.deployment) ? {} : { deployment: deploymentOf(s.deployment, "deployment") }),
  });
}

/** What a quote, or an x402 request, is told of its payment's signer. A verifyGas past a safe
 * integer is kept at the largest: past every max_gas, which refuses it. */
export function payerOf(p: Record<string, unknown>): Payer {
  return {
    ...(absent(p.payer) ? {} : { address: address(p.payer, "payer") }),
    ...(absent(p.deployment) ? {} : { deployment: deploymentOf(p.deployment, "deployment") }),
    ...(absent(p.verifyGas) ? {} : { verifyGas: clamped(p.verifyGas, "verifyGas") }),
  };
}

/** What a quote prices: its calls (quotedCallOf), and their authorizations, signed, or the
 * account that will sign one (`authority`). */
export function quoted(p: Record<string, unknown>): Quoted {
  only(p, QUOTE_FIELDS, "");
  const calls = callsOf(p.calls).map((raw, index) => quotedCallOf(raw, `calls[${index}]`));
  const authorizations = optionalList(p.authorizationList, "authorizationList").map((a, i) =>
    authorization(a, `authorizationList[${i}]`)
  );
  const authority = absent(p.authority) ? null : address(p.authority, "authority");
  return { calls, authorizations, authority };
}

/** An x402 request's calls as their challenge quotes them. */
export function x402Quoted(params: X402Params): Quoted {
  return {
    calls: params.calls.map(({ call, gas }) => ({ call, callDataLength: call.data.length, gas })),
    authorizations: params.authorizations,
    authority: null,
  };
}

/** Each of an x402 request's calls' gas, if it states it. */
export function statedGas(params: X402Params): (number | null)[] {
  return params.calls.map((each) => each.gas);
}

/** An x402 request's body: its calls, and what's known of its payer. A gas past a safe integer
 * is kept at the largest, past every max_gas. */
export function x402Params(raw: unknown): X402Params {
  const p = object(raw, "body");
  only(p, X402_FIELDS, "");
  return {
    payer: payerOf(p),
    calls: callsOf(p.calls).map((raw, index) => submittedCallOf(raw, `calls[${index}]`)),
    // checked as the submission's will be when it's taken
    authorizations: optionalList(p.authorizationList, "authorizationList").map((item, i) =>
      authorization(item, `authorizationList[${i}]`)
    ),
  };
}

/**
 * The submission an x402 payment of `token` to `payee` by `transfer` makes of the calls in
 * `params`, each given its `gas`, included by `deadline`.
 */
export function x402Submission(
  params: X402Params,
  token: Address,
  payee: Address,
  deadline: number,
  transfer: TransferAuthorization,
  gas: readonly (number | null)[],
): Submission {
  return checkedSubmission({
    calls: params.calls.map(({ call }, index) => ({ call, gas: gas[index] ?? null })),
    authorizations: params.authorizations,
    token,
    payee,
    amount: transfer.value,
    maxBlockNumber: deadline,
    transfer,
    ...(params.payer.deployment === undefined ? {} : { deployment: params.payer.deployment }),
  });
}

/** A PAYMENT-SIGNATURE's payment payload, its JSON parsed, as x402 nests it. Its authorization's
 * numbers are decimals, bounded by the caller. */
export function paymentPayload(raw: unknown): PaymentPayload {
  const p = object(raw, "payment");
  if (!Number.isInteger(p.x402Version)) {
    throw new InvalidParamsError("x402Version", refused("malformed", { why: "an integer" }));
  }
  const payload = object(p.payload, "payload");
  const a = object(payload.authorization, "payload.authorization");
  return {
    x402Version: p.x402Version as number,
    accepted: paymentRequirements(p.accepted, "accepted"),
    payload: {
      signature: bytes(matched(payload.signature, SIGNATURE, "payload.signature")),
      authorization: {
        from: address(a.from, "payload.authorization.from"),
        to: address(a.to, "payload.authorization.to"),
        value: decimal(a.value, "payload.authorization.value"),
        validAfter: decimal(a.validAfter, "payload.authorization.validAfter"),
        validBefore: decimal(a.validBefore, "payload.authorization.validBefore"),
        nonce: bytes(matched(a.nonce, BYTES32, "payload.authorization.nonce")),
      },
    },
    ...(absent(p.resource) ? {} : { resource: resourceInfo(p.resource, "resource") }),
    ...(absent(p.extensions) ? {} : { extensions: object(p.extensions, "extensions") }),
  };
}

/** What x402's /verify and /settle take: a payment, and what the resource server required of
 * it. */
export interface FacilitatorRequest {
  readonly x402Version: number;
  readonly paymentPayload: PaymentPayload;
  readonly paymentRequirements: PaymentRequirements;
}

/** x402's /verify and /settle request, as bruno/server/wire.py's FacilitatorRequest decodes
 * it. */
export function facilitatorRequest(raw: unknown): FacilitatorRequest {
  const r = object(raw, "body");
  if (!Number.isInteger(r.x402Version)) {
    throw new InvalidParamsError("x402Version", refused("malformed", { why: "an integer" }));
  }
  return {
    x402Version: r.x402Version as number,
    paymentPayload: paymentPayload(r.paymentPayload),
    paymentRequirements: paymentRequirements(r.paymentRequirements, "paymentRequirements"),
  };
}

function paymentRequirements(raw: unknown, at: string): PaymentRequirements {
  const r = object(raw, at);
  if (!Number.isInteger(r.maxTimeoutSeconds)) {
    throw new InvalidParamsError(
      `${at}.maxTimeoutSeconds`,
      refused("malformed", { why: "an integer" }),
    );
  }
  return {
    scheme: text(r.scheme, `${at}.scheme`),
    network: text(r.network, `${at}.network`),
    amount: text(r.amount, `${at}.amount`),
    asset: address(r.asset, `${at}.asset`),
    payTo: address(r.payTo, `${at}.payTo`),
    maxTimeoutSeconds: r.maxTimeoutSeconds as number,
    ...(absent(r.extra) ? {} : { extra: tokenDomain(r.extra, `${at}.extra`) }),
  };
}

function tokenDomain(raw: unknown, at: string): TokenDomain {
  const d = object(raw, at);
  return {
    name: text(d.name, `${at}.name`),
    version: text(d.version, `${at}.version`),
    ...(absent(d.assetTransferMethod)
      ? {}
      : { assetTransferMethod: text(d.assetTransferMethod, `${at}.assetTransferMethod`) }),
    ...(absent(d.paymentFlow) ? {} : { paymentFlow: text(d.paymentFlow, `${at}.paymentFlow`) }),
  };
}

function resourceInfo(raw: unknown, at: string): ResourceInfo {
  const r = object(raw, at);
  return {
    url: text(r.url, `${at}.url`),
    ...(absent(r.description) ? {} : { description: text(r.description, `${at}.description`) }),
    ...(absent(r.mimeType) ? {} : { mimeType: text(r.mimeType, `${at}.mimeType`) }),
    ...(absent(r.serviceName) ? {} : { serviceName: text(r.serviceName, `${at}.serviceName`) }),
    // left out or a list: null isn't one (as msgspec reads a tuple)
    ...(r.tags === undefined
      ? {}
      : { tags: array(r.tags, `${at}.tags`).map((t, i) => text(t, `${at}.tags[${i}]`)) }),
  };
}

/** A payment's signed EIP-3009 transfer: of `amount`, from `from` to `payee`. */
function transferOf(
  t: Record<string, unknown>,
  payee: Address,
  amount: bigint,
): TransferAuthorization {
  only(t, TRANSFER_FIELDS, "payment.transfer.");
  return {
    payer: address(t.from, "payment.transfer.from"),
    to: payee,
    value: amount,
    validAfter: quantityOf(t.validAfter, "payment.transfer.validAfter"),
    validBefore: quantityOf(t.validBefore, "payment.transfer.validBefore"),
    nonce: bytes(matched(t.nonce, BYTES32, "payment.transfer.nonce")),
    signature: bytes(matched(t.signature, SIGNATURE, "payment.transfer.signature")),
  };
}

function deploymentOf(raw: unknown, at: string): Deployment {
  const d = object(raw, at);
  only(d, DEPLOYMENT_FIELDS, `${at}.`);
  return {
    account: address(d.account, `${at}.account`),
    factory: address(d.factory, `${at}.factory`),
    factoryData: bytes(data(d.factoryData, `${at}.factoryData`)),
  };
}

function authorization(raw: unknown, at: string): SignedAuthorization {
  const a = object(raw, at);
  return {
    chainId: quantityOf(a.chainId, `${at}.chainId`),
    address: address(a.address, `${at}.address`),
    nonce: quantityOf(a.nonce, `${at}.nonce`),
    yParity: clampedNumber(wordOf(a.yParity, `${at}.yParity`)),
    r: wordOf(a.r, `${at}.r`),
    s: wordOf(a.s, `${at}.s`),
  };
}

/** A number of at most a word, padded or not. */
function wordOf(raw: unknown, at: string): bigint {
  if (typeof raw !== "string" || !WORD.test(raw)) {
    throw new InvalidParamsError(at, refused("malformed", { why: "a word, as hex" }));
  }
  return BigInt(raw);
}

export function absent(raw: unknown): boolean {
  return raw === undefined || raw === null;
}

/**
 * ERC-7769's UserOperation (bruno/server/wire.py's UserOperationJson), for the EntryPoint at
 * `entryPoint`: each field as its type says, absent gas limits and fees zero, an EIP-7702
 * account's factory 0x7702, its authorization's signature checked as a transaction's would be;
 * its paymaster's fields, if it has one, its limits null if not given (left to the estimate).
 */
export function userOperation(raw: unknown, entryPoint: Address): UserOperation {
  const op = object(raw, "userOperation");
  const given = (name: string): boolean => op[name] !== undefined && op[name] !== null;
  const optional = (name: string): bigint => given(name) ? quantityOf(op[name], name) : 0n;
  const orNull = (name: string): bigint | null => given(name) ? quantityOf(op[name], name) : null;
  // a fee left out is 0; given, it's a quantity: null isn't one
  const fee = (name: string): bigint => op[name] === undefined ? 0n : quantityOf(op[name], name);
  let paymaster: Paymaster | null = null;
  const verificationGasLimit = orNull("paymasterVerificationGasLimit");
  const postOpGasLimit = orNull("paymasterPostOpGasLimit");
  const paymasterData = given("paymasterData")
    ? bytes(data(op.paymasterData, "paymasterData"))
    : null;
  if (given("paymaster")) {
    paymaster = {
      address: address(op.paymaster, "paymaster"),
      verificationGasLimit,
      postOpGasLimit,
      data: paymasterData ?? new Uint8Array(),
    };
  } else if (verificationGasLimit !== null || postOpGasLimit !== null || paymasterData !== null) {
    throw new InvalidUserOperationError("paymaster", refused("paymaster-fields"));
  }
  let auth: SignedAuthorization | null = null;
  if (given("eip7702Auth")) {
    auth = authorization(op.eip7702Auth, "eip7702Auth");
    try {
      checkSigned(auth.yParity, auth.r, auth.s, auth.nonce);
    } catch (e) {
      if (e instanceof InvalidSubmissionError) {
        throw new InvalidUserOperationError("eip7702Auth", e.refused);
      }
      throw e;
    }
  }
  let factory: Address | null = null;
  if (given("factory")) {
    factory = op.factory === EIP7702_FACTORY ? EIP7702_MARKER : address(op.factory, "factory");
  }
  const parsed: UserOperation = {
    entryPoint: entryPointAt(entryPoint),
    sender: address(op.sender, "sender"),
    nonce: quantityOf(op.nonce, "nonce"),
    factory,
    factoryData: given("factoryData")
      ? bytes(data(op.factoryData, "factoryData"))
      : new Uint8Array(),
    callData: bytes(data(op.callData, "callData")),
    callGasLimit: optional("callGasLimit"),
    verificationGasLimit: optional("verificationGasLimit"),
    preVerificationGas: optional("preVerificationGas"),
    maxFeePerGas: fee("maxFeePerGas"),
    maxPriorityFeePerGas: fee("maxPriorityFeePerGas"),
    signature: bytes(data(op.signature, "signature")),
    authorization: auth,
    paymaster,
  };
  return parsed;
}

/** An op, and its EntryPoint: REST's body, to estimate or to send it. */
export function userOperationParams(raw: unknown): UserOperation {
  const p = object(raw, "body");
  return userOperation(p.userOperation, address(p.entryPoint, "entryPoint"));
}

export function object(raw: unknown, at: string): Record<string, unknown> {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    throw new InvalidParamsError(at, refused("malformed", { why: "an object" }));
  }
  return raw as Record<string, unknown>;
}

export function array(raw: unknown, at: string): unknown[] {
  if (!Array.isArray(raw)) {
    throw new InvalidParamsError(at, refused("malformed", { why: "a list" }));
  }
  return raw;
}

/** A list that may be left out, empty if it is: null isn't one (as msgspec reads a tuple). */
function optionalList(raw: unknown, at: string): unknown[] {
  return raw === undefined ? [] : array(raw, at);
}

function text(raw: unknown, at: string): string {
  if (typeof raw !== "string") {
    throw new InvalidParamsError(at, refused("malformed", { why: "a string" }));
  }
  return raw;
}

function matched(raw: unknown, pattern: RegExp, at: string): Hex {
  if (typeof raw !== "string" || !pattern.test(raw)) {
    throw new InvalidParamsError(at, refused("malformed", { why: "malformed" }));
  }
  return raw as Hex;
}

/** A hex quantity under `bound`: a uint256's, unless the field's type is narrower. */
export function quantityOf(raw: unknown, at: string, bound: bigint = UINT256): bigint {
  if (typeof raw !== "string" || !QUANTITY.test(raw) || raw.length > QUANTITY_MAX_LENGTH) {
    throw new InvalidParamsError(at, refused("malformed", { why: "a quantity, as hex" }));
  }
  const value = BigInt(raw);
  if (value >= bound) {
    throw new InvalidParamsError(at, refused("malformed", { why: "out of range" }));
  }
  return value;
}

/** A hex quantity that's a safe integer: gas, a length, a block number. */
export function safe(raw: unknown, at: string): number {
  return Number(quantityOf(raw, at, SAFE));
}

/** A hex quantity as a number, one past a safe integer kept at the largest: past every limit
 * the relay checks it against, as bruno/server's ints are, so refused saying the limit. */
export function clamped(raw: unknown, at: string): number {
  return clampedNumber(quantityOf(raw, at));
}

function clampedNumber(value: bigint): number {
  return value >= SAFE ? Number.MAX_SAFE_INTEGER : Number(value);
}

/** A decimal integer, as x402 writes its amounts and times: at most 78 digits. */
function decimal(raw: unknown, at: string): bigint {
  return BigInt(matched(raw, DECIMAL, at));
}

export function address(raw: unknown, at: string): Address {
  if (typeof raw !== "string" || !ADDRESS.test(raw)) {
    throw new InvalidParamsError(at, refused("malformed", { why: "an address" }));
  }
  return checksum(raw);
}

function data(raw: unknown, at: string): Hex {
  if (typeof raw !== "string" || !DATA.test(raw)) {
    throw new InvalidParamsError(at, refused("malformed", { why: "bytes, as hex" }));
  }
  return raw.toLowerCase() as Hex;
}

/** EIP-3009's transferWithAuthorization, and its token's domain, as EIP-712 types them. */
const TRANSFER_TYPES = {
  EIP712Domain: [
    { name: "name", type: "string" },
    { name: "version", type: "string" },
    { name: "chainId", type: "uint256" },
    { name: "verifyingContract", type: "address" },
  ],
  TransferWithAuthorization: [
    { name: "from", type: "address" },
    { name: "to", type: "address" },
    { name: "value", type: "uint256" },
    { name: "validAfter", type: "uint256" },
    { name: "validBefore", type: "uint256" },
    { name: "nonce", type: "bytes32" },
  ],
} as const;

/** A transfer for its payer to sign, as EIP-712's JSON (eth_signTypedData_v4's) writes it, its
 * numbers as decimal strings: signed as it is, it's the payment (bruno/server/wire.py's
 * TransferTypedData). */
export function transferTypedData(t: UnsignedTransfer): unknown {
  return {
    types: TRANSFER_TYPES,
    primaryType: "TransferWithAuthorization",
    domain: {
      name: t.domain.name,
      version: t.domain.version,
      chainId: t.chainId,
      verifyingContract: t.token,
    },
    message: {
      from: t.payer,
      to: t.to,
      value: t.value.toString(),
      validAfter: t.validAfter.toString(),
      validBefore: t.validBefore.toString(),
      nonce: hex(t.nonce),
    },
  };
}
