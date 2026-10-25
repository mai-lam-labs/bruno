/**
 * x402 v2's `exact` scheme on EVM with EIP-3009, as Bruno answers it: the 402's requirements
 * read, a `transferWithAuthorization` of the price built as typed data for any signer, and the
 * PAYMENT-SIGNATURE and PAYMENT-RESPONSE headers (base64 of JSON).
 */

import type { Hex, Signer, TypedData } from "./types.ts";

/** What a 402 asks for: one way to pay. */
export interface Requirement {
  readonly scheme: string;
  readonly network: string;
  readonly amount: string;
  readonly asset: Hex;
  readonly payTo: Hex;
  readonly maxTimeoutSeconds: number;
  readonly extra: {
    readonly name: string;
    readonly version: string;
    readonly assetTransferMethod?: string;
    readonly paymentFlow?: string;
  };
}

/** A 402's PAYMENT-REQUIRED: the ways to pay, and what for. */
export interface PaymentRequired {
  readonly x402Version: number;
  readonly error?: string;
  readonly resource?: unknown;
  readonly accepts: readonly Requirement[];
  readonly extensions?: unknown;
}

/** A settlement's PAYMENT-RESPONSE. */
export interface Settlement {
  readonly success: boolean;
  readonly transaction: Hex | "";
  readonly network: string;
  readonly payer?: Hex;
  readonly errorReason?: string;
}

/** The transfer a payment authorizes, as signed. */
export interface Transfer {
  readonly from: Hex;
  readonly to: Hex;
  readonly value: bigint;
  readonly validAfter: bigint;
  readonly validBefore: bigint;
  readonly nonce: Hex;
}

/** EIP-3009's TransferWithAuthorization, and the domain's fields, as EIP-712 types. */
const TYPES = {
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

export class X402Error extends Error {}

/** `network`'s chain id: x402 v2 names an EVM network `eip155:<chain id>`. */
export function chainIdOf(network: string): number {
  const match = /^eip155:([1-9][0-9]*)$/.exec(network);
  if (match === null) throw new X402Error(`not an EVM network: ${network}`);
  return Number(match[1]);
}

/** The requirement to pay by: `exact` on EVM, paid by EIP-3009 and settled with the work (the
 * defaults, or named so), in `asset` if given. */
export function chooseRequirement(required: PaymentRequired, asset?: Hex): Requirement {
  const chosen = required.accepts.find((r) =>
    r.scheme === "exact" &&
    r.network.startsWith("eip155:") &&
    (r.extra.assetTransferMethod ?? "eip3009") === "eip3009" &&
    (r.extra.paymentFlow ?? "authorization") === "authorization" &&
    (asset === undefined || r.asset.toLowerCase() === asset.toLowerCase())
  );
  if (chosen === undefined) {
    throw new X402Error("no way to pay this client takes: exact, EIP-3009, on EVM");
  }
  return chosen;
}

/** A fresh 32-byte nonce: a transfer's, never used before. */
export function freshNonce(): Hex {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return `0x${[...bytes].map((b) => b.toString(16).padStart(2, "0")).join("")}`;
}

/** The transfer paying `requirement` from `payer`: `value` (its amount, or more as a tip),
 * valid now and for its maxTimeoutSeconds. */
export function transferFor(
  requirement: Requirement,
  payer: Hex,
  value: bigint,
  now: number = Math.floor(Date.now() / 1000),
  nonce: Hex = freshNonce(),
): Transfer {
  return {
    from: payer,
    to: requirement.payTo,
    value,
    validAfter: 0n,
    validBefore: BigInt(now + requirement.maxTimeoutSeconds),
    nonce,
  };
}

/** `transfer` as the typed data its payer signs, for `requirement`'s token. */
export function typedData(requirement: Requirement, transfer: Transfer): TypedData {
  return {
    domain: {
      name: requirement.extra.name,
      version: requirement.extra.version,
      chainId: chainIdOf(requirement.network),
      verifyingContract: requirement.asset,
    },
    types: TYPES,
    primaryType: "TransferWithAuthorization",
    message: {
      from: transfer.from,
      to: transfer.to,
      value: transfer.value.toString(),
      validAfter: transfer.validAfter.toString(),
      validBefore: transfer.validBefore.toString(),
      nonce: transfer.nonce,
    },
  };
}

/** The PAYMENT-SIGNATURE header paying `requirement` with `transfer`, signed `signature`. */
export function paymentSignature(
  required: PaymentRequired,
  requirement: Requirement,
  transfer: Transfer,
  signature: Hex,
): string {
  const payload = {
    x402Version: 2,
    ...(required.resource === undefined ? {} : { resource: required.resource }),
    accepted: { ...requirement, amount: transfer.value.toString() },
    payload: {
      signature,
      authorization: {
        from: transfer.from,
        to: transfer.to,
        value: transfer.value.toString(),
        validAfter: transfer.validAfter.toString(),
        validBefore: transfer.validBefore.toString(),
        nonce: transfer.nonce,
      },
    },
  };
  return btoa(JSON.stringify(payload));
}

/** Sign `transfer` for `requirement` with `signer`: its PAYMENT-SIGNATURE header. */
export async function pay(
  required: PaymentRequired,
  requirement: Requirement,
  transfer: Transfer,
  signer: Signer,
): Promise<string> {
  const signature = await signer.signTypedData(typedData(requirement, transfer));
  return paymentSignature(required, requirement, transfer, signature);
}

export function decodePaymentRequired(header: string): PaymentRequired {
  return JSON.parse(atob(header)) as PaymentRequired;
}

export function decodeSettlement(header: string): Settlement {
  return JSON.parse(atob(header)) as Settlement;
}
