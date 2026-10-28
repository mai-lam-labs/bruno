/**
 * x402 v2's `exact` scheme on EVM with EIP-3009, as Bruno answers it: the 402's requirements
 * read, a `transferWithAuthorization` of the price built as typed data for any signer
 * (eip3009.ts), and the PAYMENT-SIGNATURE and PAYMENT-RESPONSE headers (base64 of JSON).
 */

import { freshNonce, type Transfer, transferOf, transferTypedData } from "./eip3009.ts";
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

export class X402Error extends Error {}

/** `network`'s chain id: x402 v2 names an EVM network `eip155:<chain id>`. */
export function networkChainId(network: string): number {
  const match = /^eip155:([1-9][0-9]*)$/.exec(network);
  if (match === null) throw new X402Error(`not an EVM network: ${network}`);
  return Number(match[1]);
}

/** The requirements this client pays by: `exact` on EVM, paid by EIP-3009 and settled with the
 * work (the defaults, or named so). */
export function usableRequirements(required: PaymentRequired): readonly Requirement[] {
  return required.accepts.filter((r) =>
    r.scheme === "exact" &&
    r.network.startsWith("eip155:") &&
    (r.extra.assetTransferMethod ?? "eip3009") === "eip3009" &&
    (r.extra.paymentFlow ?? "authorization") === "authorization"
  );
}

/** The transfer paying `requirement` from `payer`: `value` in the token's smallest unit (its
 * amount, or more as a tip), valid now and for its maxTimeoutSeconds. */
export function transferFor(
  requirement: Requirement,
  payer: Hex,
  value: bigint,
  now: number = Math.floor(Date.now() / 1000),
  nonce: Hex = freshNonce(),
): Transfer {
  return transferOf(payer, requirement.payTo, value, requirement.maxTimeoutSeconds, now, nonce);
}

/** `transfer` as the typed data its payer signs, for `requirement`'s token. */
export function typedData(requirement: Requirement, transfer: Transfer): TypedData {
  return transferTypedData(
    networkChainId(requirement.network),
    requirement.asset,
    requirement.extra,
    transfer,
  );
}

/** The PAYMENT-SIGNATURE header paying `requirement` with `transfer`, signed `signature`. */
function paymentSignature(
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
  return encoded(payload);
}

/** A payment signed: its PAYMENT-SIGNATURE header, and the signature in it. */
export interface SignedPayment {
  readonly header: string;
  readonly signature: Hex;
}

/** Sign `transfer` for `requirement` with `signer`: its PAYMENT-SIGNATURE header, and the
 * signature, as signed (ERC-6492-wrapped, for a smart account not yet deployed). */
export async function signedPayment(
  required: PaymentRequired,
  requirement: Requirement,
  transfer: Transfer,
  signer: Signer,
): Promise<SignedPayment> {
  const signature = await signer.signTypedData(typedData(requirement, transfer));
  return { header: paymentSignature(required, requirement, transfer, signature), signature };
}

export function decodePaymentRequired(header: string): PaymentRequired {
  return decoded(header) as PaymentRequired;
}

export function decodeSettlement(header: string): Settlement {
  return decoded(header) as Settlement;
}

/** A header's value: base64 of the JSON's UTF-8, as x402 has it (btoa alone takes Latin-1). */
function encoded(value: unknown): string {
  const utf8 = new TextEncoder().encode(JSON.stringify(value));
  return btoa(Array.from(utf8, (b) => String.fromCharCode(b)).join(""));
}

function decoded(header: string): unknown {
  const binary = Uint8Array.from(atob(header), (c) => c.charCodeAt(0));
  return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(binary));
}
