/**
 * EIP-3009's transferWithAuthorization: a transfer of an EIP-3009 token (USDC) its payer signs
 * as typed data, which anyone may then make. The transfer, its typed data for any signer, and
 * the token call that makes it.
 */

import type { Hex, TypedData } from "./types.ts";

/** The transfer a payment authorizes, as signed. */
export interface Transfer {
  readonly from: Hex;
  readonly to: Hex;
  readonly value: bigint;
  readonly validAfter: bigint;
  readonly validBefore: bigint;
  readonly nonce: Hex;
}

/** The EIP-712 domain an EIP-3009 token's transfers are signed for, beyond its chain and
 * address: its name and version. */
export interface Eip3009Domain {
  readonly name: string;
  readonly version: string;
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

/** A fresh 32-byte nonce: a transfer's, never used before. */
export function freshNonce(): Hex {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return `0x${[...bytes].map((b) => b.toString(16).padStart(2, "0")).join("")}`;
}

/** The transfer of `value` (in the token's smallest unit) from `from` to `to`, valid now and
 * for `seconds`, once (its `nonce`). */
export function transferOf(
  from: Hex,
  to: Hex,
  value: bigint,
  seconds: number,
  now: number = Math.floor(Date.now() / 1000),
  nonce: Hex = freshNonce(),
): Transfer {
  return { from, to, value, validAfter: 0n, validBefore: BigInt(now + seconds), nonce };
}

/** `transfer` as the typed data its payer signs, for the token at `token` on `chainId`, its
 * domain `domain`. */
export function transferTypedData(
  chainId: number,
  token: Hex,
  domain: Eip3009Domain,
  transfer: Transfer,
): TypedData {
  return {
    domain: {
      name: domain.name,
      version: domain.version,
      chainId,
      verifyingContract: token,
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

/** transferWithAuthorization's selectors: a key's signature as (v, r, s), which every EIP-3009
 * token takes; and as bytes, FiatToken v2.2's (USDC's), for a contract's (ERC-1271). */
const WITH_VRS = "e3ee160e";
const WITH_BYTES = "cf092995";

/**
 * The token call that makes `transfer`, signed `signature` (inner, if it was ERC-6492-wrapped:
 * the token checks the deployed account's own): through the (v, r, s) overload for a key's 65
 * bytes (a v of 0 or 1 read as 27 or 28), else the bytes overload, as x402's reference
 * facilitator chooses. Anyone may send it.
 */
export function transferCalldata(transfer: Transfer, signature: Hex): Hex {
  const word = (n: bigint) => n.toString(16).padStart(64, "0");
  const address = (a: Hex) => a.slice(2).toLowerCase().padStart(64, "0");
  const head = address(transfer.from) + address(transfer.to) + word(transfer.value) +
    word(transfer.validAfter) + word(transfer.validBefore) + transfer.nonce.slice(2);
  const bytes = signature.slice(2);
  if (bytes.length === 130) {
    const v = Number.parseInt(bytes.slice(128, 130), 16);
    return `0x${WITH_VRS}${head}${word(BigInt(v < 27 ? v + 27 : v))}${bytes.slice(0, 64)}${
      bytes.slice(64, 128)
    }`;
  }
  const padded = bytes.padEnd(Math.ceil(bytes.length / 64) * 64, "0");
  return `0x${WITH_BYTES}${head}${word(7n * 32n)}${word(BigInt(bytes.length / 2))}${padded}`;
}
