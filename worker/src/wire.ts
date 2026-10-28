/**
 * The interface's requests as read: each field checked as the Python reference's wire types
 * declare it (bruno/server/wire.py), a field that isn't refused as InvalidParams; and JSON-RPC's
 * error codes. REST, JSON-RPC and x402 read through the same checks.
 */

import { type Address, bytes, checksum, type Hex } from "./evm.ts";
import type { TransferAuthorization } from "./eip3009.ts";
import type { Deployment } from "./erc6492.ts";
import type { Payer } from "./relay.ts";
import { checkedSubmission, type Submission } from "./submission.ts";
import type { SignedAuthorization } from "./transactions.ts";
import { InvalidUserOperationError, type UserOperation } from "./userops.ts";

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
const DECIMAL = /^(0|[1-9][0-9]{0,77})$/;
const SIGNATURE = new RegExp(`^0x(?:[0-9a-fA-F]{2}){1,${MAX_SIGNATURE_BYTES}}$`);
const UINT256 = 1n << 256n;
const SAFE = BigInt(Number.MAX_SAFE_INTEGER) + 1n;

/** A request's field that isn't what its type says: the field, and why. */
export class InvalidParams extends Error {
  constructor(readonly field: string, message: string) {
    super(`${field}: ${message}`);
  }
}

/** The call an x402 payment is for; and, as for a quote, what's known of its signer. */
export interface X402Call {
  readonly call: { readonly to: Address; readonly data: Uint8Array };
  readonly gas: number;
  readonly authorizations: readonly SignedAuthorization[];
  readonly payer: Payer;
}

/** What a client pays with: the requirement it accepted, its authorization and signature. */
export interface PaymentPayload {
  readonly x402Version: number;
  readonly accepted: {
    readonly scheme: string;
    readonly network: string;
    readonly amount: string;
    readonly asset: Address;
    readonly payTo: Address;
    /** The protocol's reserved keys in `extra`, as a client may echo them. */
    readonly assetTransferMethod: string | null;
    readonly paymentFlow: string | null;
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

/** A submission, as REST's body or postpaid_sendTransaction's parameter. */
export function submission(raw: unknown): Submission {
  const s = object(raw, "submission");
  const call = object(s.call, "call");
  const payment = object(s.payment, "payment");
  const authorizations = optionalList(s.authorizationList, "authorizationList").map(
    (a, i) => authorization(a, `authorizationList[${i}]`),
  );
  const payee = address(payment.payee, "payment.payee");
  const amount = quantityOf(payment.amount, "payment.amount");
  const transfer = absent(payment.transfer)
    ? undefined
    : transferOf(object(payment.transfer, "payment.transfer"), payee, amount);
  return checkedSubmission({
    call: { to: address(call.to, "call.to"), data: bytes(data(call.data, "call.data")), value: 0n },
    authorizations,
    gas: safe(s.gas, "gas"),
    token: address(payment.token, "payment.token"),
    payee,
    amount,
    maxBlockNumber: safe(s.maxBlockNumber, "maxBlockNumber"),
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

/** An x402 request's body: the call, and what's known of its payer. A gas past a safe integer
 * is kept at the largest, past every max_gas. */
export function x402Params(raw: unknown): X402Call {
  const p = object(raw, "body");
  const call = object(p.call, "call");
  return {
    payer: payerOf(p),
    call: { to: address(call.to, "call.to"), data: bytes(data(call.data, "call.data")) },
    gas: clamped(p.gas, "gas"),
    // checked as the submission's will be when it's taken
    authorizations: optionalList(p.authorizationList, "authorizationList").map((item, i) =>
      authorization(item, `authorizationList[${i}]`)
    ),
  };
}

/** A PAYMENT-SIGNATURE's payment payload, its JSON parsed: the accepted requirement whole, the
 * optional parts typed. Its authorization's numbers are decimals, bounded by the caller. */
export function paymentPayload(raw: unknown): PaymentPayload {
  const p = object(raw, "payment");
  if (!Number.isInteger(p.x402Version)) throw new InvalidParams("x402Version", "an integer");
  const accepted = object(p.accepted, "accepted");
  if (!Number.isInteger(accepted.maxTimeoutSeconds)) {
    throw new InvalidParams("accepted.maxTimeoutSeconds", "an integer");
  }
  let assetTransferMethod: string | null = null;
  let paymentFlow: string | null = null;
  if (!absent(accepted.extra)) {
    const extra = object(accepted.extra, "accepted.extra");
    text(extra.name, "accepted.extra.name");
    text(extra.version, "accepted.extra.version");
    if (!absent(extra.assetTransferMethod)) {
      assetTransferMethod = text(extra.assetTransferMethod, "accepted.extra.assetTransferMethod");
    }
    if (!absent(extra.paymentFlow)) {
      paymentFlow = text(extra.paymentFlow, "accepted.extra.paymentFlow");
    }
  }
  if (!absent(p.resource)) resource(p.resource);
  if (!absent(p.extensions)) object(p.extensions, "extensions");
  const payload = object(p.payload, "payload");
  const a = object(payload.authorization, "payload.authorization");
  return {
    x402Version: p.x402Version as number,
    accepted: {
      scheme: text(accepted.scheme, "accepted.scheme"),
      network: text(accepted.network, "accepted.network"),
      amount: text(accepted.amount, "accepted.amount"),
      asset: address(accepted.asset, "accepted.asset"),
      payTo: address(accepted.payTo, "accepted.payTo"),
      assetTransferMethod,
      paymentFlow,
    },
    authorization: {
      from: address(a.from, "authorization.from"),
      to: address(a.to, "authorization.to"),
      value: decimal(a.value, "authorization.value"),
      validAfter: decimal(a.validAfter, "authorization.validAfter"),
      validBefore: decimal(a.validBefore, "authorization.validBefore"),
      nonce: bytes(matched(a.nonce, BYTES32, "authorization.nonce")),
    },
    signature: bytes(matched(payload.signature, SIGNATURE, "payload.signature")),
  };
}

/** A payload's resource, checked as the Python's ResourceInfo. */
function resource(raw: unknown): void {
  const r = object(raw, "resource");
  text(r.url, "resource.url");
  for (const field of ["description", "mimeType", "serviceName"]) {
    if (!absent(r[field])) text(r[field], `resource.${field}`);
  }
  if (r.tags !== undefined) {
    array(r.tags, "resource.tags").forEach((t, i) => text(t, `resource.tags[${i}]`));
  }
}

/** A payment's signed EIP-3009 transfer: of `amount`, from `from` to `payee`. */
function transferOf(
  t: Record<string, unknown>,
  payee: Address,
  amount: bigint,
): TransferAuthorization {
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
    yParity: clamped(a.yParity, `${at}.yParity`),
    r: quantityOf(a.r, `${at}.r`),
    s: quantityOf(a.s, `${at}.s`),
  };
}

export function absent(raw: unknown): boolean {
  return raw === undefined || raw === null;
}

/**
 * ERC-7769's UserOperation for EntryPoint v0.7 (bruno/server/wire.py's UserOperationJson): each
 * field as its type says, absent gas limits and fees zero; then the paymaster's fields refused,
 * an op paying the relay by its fees or by its calls.
 */
export function userOperation(raw: unknown): UserOperation {
  const op = object(raw, "userOperation");
  const given = (name: string): boolean => op[name] !== undefined && op[name] !== null;
  const optional = (name: string): bigint => given(name) ? quantityOf(op[name], name) : 0n;
  const parsed: UserOperation = {
    sender: address(op.sender, "sender"),
    nonce: quantityOf(op.nonce, "nonce"),
    factory: given("factory") ? address(op.factory, "factory") : null,
    factoryData: given("factoryData")
      ? bytes(data(op.factoryData, "factoryData"))
      : new Uint8Array(),
    callData: bytes(data(op.callData, "callData")),
    callGasLimit: optional("callGasLimit"),
    verificationGasLimit: optional("verificationGasLimit"),
    preVerificationGas: optional("preVerificationGas"),
    maxFeePerGas: optional("maxFeePerGas"),
    maxPriorityFeePerGas: optional("maxPriorityFeePerGas"),
    signature: bytes(data(op.signature, "signature")),
  };
  if (given("paymaster")) address(op.paymaster, "paymaster");
  if (given("paymasterData")) data(op.paymasterData, "paymasterData");
  const paymaster = [
    "paymaster",
    "paymasterVerificationGasLimit",
    "paymasterPostOpGasLimit",
    "paymasterData",
  ];
  for (const name of paymaster.slice(1, 3)) optional(name);
  if (paymaster.some(given)) {
    throw new InvalidUserOperationError(
      "paymaster",
      "none: an op pays the relay by its fees or by its calls",
    );
  }
  return parsed;
}

/** An op, and its EntryPoint: REST's body, to estimate or to send it. */
export function userOperationParams(raw: unknown): { op: UserOperation; entryPoint: Address } {
  const p = object(raw, "body");
  return { op: userOperation(p.userOperation), entryPoint: address(p.entryPoint, "entryPoint") };
}

export function object(raw: unknown, at: string): Record<string, unknown> {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    throw new InvalidParams(at, "an object");
  }
  return raw as Record<string, unknown>;
}

export function array(raw: unknown, at: string): unknown[] {
  if (!Array.isArray(raw)) throw new InvalidParams(at, "a list");
  return raw;
}

/** A list that may be left out, empty if it is: null isn't one (as msgspec reads a tuple). */
function optionalList(raw: unknown, at: string): unknown[] {
  return raw === undefined ? [] : array(raw, at);
}

function text(raw: unknown, at: string): string {
  if (typeof raw !== "string") throw new InvalidParams(at, "a string");
  return raw;
}

function matched(raw: unknown, pattern: RegExp, at: string): Hex {
  if (typeof raw !== "string" || !pattern.test(raw)) throw new InvalidParams(at, "malformed");
  return raw as Hex;
}

/** A hex quantity under `bound`: a uint256's, unless the field's type is narrower. */
export function quantityOf(raw: unknown, at: string, bound: bigint = UINT256): bigint {
  if (typeof raw !== "string" || !QUANTITY.test(raw) || raw.length > QUANTITY_MAX_LENGTH) {
    throw new InvalidParams(at, "a quantity, as hex");
  }
  const value = BigInt(raw);
  if (value >= bound) throw new InvalidParams(at, "out of range");
  return value;
}

/** A hex quantity that's a safe integer: gas, a length, a block number. */
export function safe(raw: unknown, at: string): number {
  return Number(quantityOf(raw, at, SAFE));
}

/** A hex quantity as a number, one past a safe integer kept at the largest. */
function clamped(raw: unknown, at: string): number {
  const value = quantityOf(raw, at);
  return value >= SAFE ? Number.MAX_SAFE_INTEGER : Number(value);
}

/** A decimal integer, as x402 writes its amounts and times: at most 78 digits. */
function decimal(raw: unknown, at: string): bigint {
  return BigInt(matched(raw, DECIMAL, at));
}

export function address(raw: unknown, at: string): Address {
  if (typeof raw !== "string" || !ADDRESS.test(raw)) throw new InvalidParams(at, "an address");
  return checksum(raw);
}

function data(raw: unknown, at: string): Hex {
  if (typeof raw !== "string" || !DATA.test(raw)) throw new InvalidParams(at, "bytes, as hex");
  return raw.toLowerCase() as Hex;
}
