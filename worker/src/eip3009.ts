/**
 * EIP-3009's transferWithAuthorization: a payment a client signs off-chain, which the runner
 * makes before the client's call (x402's `exact` scheme on EVM). The calldata, the measurement
 * of its gas, and the block a signed authorization lasts until. A payer is a key (65 bytes,
 * sent as (v, r, s)) or a contract (ERC-1271, any other signature, sent as bytes), as x402's
 * reference facilitator does; one not deployed yet wraps its signature (ERC-6492, erc6492.ts):
 * the token is given the inner one, and the wrapper's deployment made first. A port of the
 * Python reference (bruno/eip3009.py).
 */

import { secp256k1 } from "@noble/curves/secp256k1.js";
import { concatBytes } from "@noble/hashes/utils.js";
import type { Call, Latest } from "./chain.ts";
import type { Eip712Domain } from "./config.ts";
import { type Deployment, unwrapped } from "./erc6492.ts";
import { type Address, hex, keccak, SECP256K1_N, utf8 } from "./evm.ts";
import { type GasSchedule, intrinsic } from "./forks.ts";
import { bigIntOf, bytesOf, keyAddress, signer } from "./transactions.ts";
import { addressWord, word } from "./abi.ts";

/** The (v, r, s) overload, which every EIP-3009 token has; and the bytes one (FiatToken
 * v2.2's), for a contract's signature. */
const TRANSFER_WITH_AUTHORIZATION = keccak(
  utf8(
    "transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,uint8,bytes32,bytes32)",
  ),
).slice(0, 4);
const TRANSFER_WITH_AUTHORIZATION_BYTES = keccak(
  utf8("transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,bytes)"),
).slice(0, 4);
/** An ECDSA signature's length: r, s and v. */
export const ECDSA_BYTES = 65;
const TRANSFER_TYPEHASH = keccak(
  utf8(
    "TransferWithAuthorization(address from,address to,uint256 value,uint256 validAfter," +
      "uint256 validBefore,bytes32 nonce)",
  ),
);
const DOMAIN_TYPEHASH = keccak(
  utf8("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
);
const NEVER = (1n << 256n) - 1n;
/** A key's calldata's length: a selector and nine words (a quote's, before the payer is known). */
export const CALLDATA_BYTES = 4 + 9 * 32;
/** The bytes overload's: a selector, six words, the signature's offset and length, and the
 * signature in words. */
const BYTES_CALLDATA_WORDS = 8;

/**
 * A transfer's calldata as a quote sizes it, before it's signed: a key's (null), or a contract's,
 * its signature `signatureBytes` long; its content no matter.
 */
export function quotedCalldata(signatureBytes: number | null): Uint8Array {
  if (signatureBytes === null) return new Uint8Array(CALLDATA_BYTES).fill(0xff);
  const words = BYTES_CALLDATA_WORDS + Math.ceil(signatureBytes / 32);
  return new Uint8Array(4 + 32 * words).fill(0xff);
}

/**
 * `payer`'s signed authorization for `value` of its token to go to `to`, by anyone, once (its
 * `nonce`), after `validAfter` and before `validBefore` (Unix seconds): `signature` a key's 65
 * bytes (r, s, v) or a contract's, as it signed, ERC-6492-wrapped if it isn't deployed yet.
 */
export interface TransferAuthorization {
  readonly payer: Address;
  readonly to: Address;
  readonly value: bigint;
  readonly validAfter: bigint;
  readonly validBefore: bigint;
  readonly nonce: Uint8Array;
  readonly signature: Uint8Array;
}

/** Its signature as the token checks it: ERC-6492's wrapper, if any, taken off. */
export function inner(a: TransferAuthorization): Uint8Array {
  return unwrapped(a.signature, a.payer).signature;
}

/** The payer's deployment its signature's ERC-6492 wrapper carries, if it's wrapped. */
export function transferDeployment(a: TransferAuthorization): Deployment | null {
  return unwrapped(a.signature, a.payer).deployment;
}

/** Whether its signature is a key's: 65 bytes, sent as (v, r, s). */
export function ecdsa(a: TransferAuthorization): boolean {
  return inner(a).length === ECDSA_BYTES;
}

/** Its 65-byte signature's v (27 or 28: 0 and 1 read as them), r and s. */
export function vrs(a: TransferAuthorization): { v: number; r: bigint; s: bigint } {
  const signature = inner(a);
  const v = signature[64] as number;
  return {
    v: v < 27 ? v + 27 : v,
    r: bigIntOf(signature.slice(0, 32)),
    s: bigIntOf(signature.slice(32, 64)),
  };
}

/** Its fields, `signature` (its own, as signed, unless another is given) as bytes,
 * ABI-encoded: the bytes overload's arguments, and its part of a submission's id. */
export function encoded(
  a: TransferAuthorization,
  signature: Uint8Array = a.signature,
): Uint8Array {
  const padded = new Uint8Array(Math.ceil(signature.length / 32) * 32);
  padded.set(signature);
  return concatBytes(
    addressWord(a.payer),
    addressWord(a.to),
    word(a.value),
    word(a.validAfter),
    word(a.validBefore),
    a.nonce,
    word(7n * 32n), // the signature's offset: after these seven words
    word(BigInt(signature.length)),
    padded,
  );
}

/** transferWithAuthorization(from, to, value, validAfter, validBefore, nonce, ...): with
 * (v, r, s) for a key's signature, the bytes for a contract's (its inner one, if wrapped). */
export function calldata(a: TransferAuthorization): Uint8Array {
  if (!ecdsa(a)) return concatBytes(TRANSFER_WITH_AUTHORIZATION_BYTES, encoded(a, inner(a)));
  const { v, r, s } = vrs(a);
  return concatBytes(
    TRANSFER_WITH_AUTHORIZATION,
    addressWord(a.payer),
    addressWord(a.to),
    word(a.value),
    word(a.validAfter),
    word(a.validBefore),
    a.nonce,
    word(BigInt(v)),
    word(r),
    word(s),
  );
}

/** What an authorization's signer signs (EIP-712): the token's domain, and the transfer. */
export function digest(
  domain: Eip712Domain,
  chainId: number,
  token: Address,
  payer: Address,
  to: Address,
  value: bigint,
  validAfter: bigint,
  validBefore: bigint,
  nonce: Uint8Array,
): Uint8Array {
  const separator = keccak(concatBytes(
    DOMAIN_TYPEHASH,
    keccak(utf8(domain.name)),
    keccak(utf8(domain.version)),
    word(BigInt(chainId)),
    addressWord(token),
  ));
  const struct = keccak(concatBytes(
    TRANSFER_TYPEHASH,
    addressWord(payer),
    addressWord(to),
    word(value),
    word(validAfter),
    word(validBefore),
    nonce,
  ));
  return keccak(concatBytes(Uint8Array.of(0x19, 0x01), separator, struct));
}

/** What `transfer`'s payer signed (EIP-712): its token's domain, and the transfer. */
export function transferDigest(
  domain: Eip712Domain,
  chainId: number,
  token: Address,
  transfer: TransferAuthorization,
): Uint8Array {
  return digest(
    domain,
    chainId,
    token,
    transfer.payer,
    transfer.to,
    transfer.value,
    transfer.validAfter,
    transfer.validBefore,
    transfer.nonce,
  );
}

/**
 * The account whose key signed `transfer` (ecrecover): null if its signature isn't a key's or
 * recovers to none. A token that also takes ERC-1271 signatures (FiatToken v2.2) accepts a
 * contract payer whose signature recovers to someone else, so a mismatch alone doesn't make it
 * invalid.
 */
export function signerOf(
  domain: Eip712Domain,
  chainId: number,
  token: Address,
  transfer: TransferAuthorization,
): Address | null {
  if (!ecdsa(transfer)) return null;
  const { v, r, s } = vrs(transfer);
  // refused by the token's ECDSA (EIP-2), so by any token
  if (s > SECP256K1_N / 2n || (v !== 27 && v !== 28)) return null;
  const hash = transferDigest(domain, chainId, token, transfer);
  try {
    const signature = concatBytes(Uint8Array.of(v - 27), bytesOf(r, 32), bytesOf(s, 32));
    return keyAddress(
      secp256k1.Signature.fromBytes(signature, "recovered").recoverPublicKey(hash).toBytes(false),
    );
  } catch {
    return null;
  }
}

/**
 * eth_estimateGas of a zero-value transfer to `payee` authorized by an account made for it, with
 * a random nonce (a fixed one, being public, could be spent to break this), never sent; and its
 * calldata. Signed for the wrong domain, it fails.
 */
export function measuring(
  domain: Eip712Domain,
  chainId: number,
  token: Address,
  payee: Address,
): { call: Call; data: Uint8Array } {
  const account = signer(secp256k1.utils.randomSecretKey());
  const nonce = crypto.getRandomValues(new Uint8Array(32));
  const hash = digest(domain, chainId, token, account.address, payee, 0n, 0n, NEVER, nonce);
  const signature = secp256k1.sign(hash, account.privateKey, {
    prehash: false,
    format: "recovered",
  });
  const data = calldata({
    payer: account.address,
    to: payee,
    value: 0n,
    validAfter: 0n,
    validBefore: NEVER,
    nonce,
    // r ‖ s ‖ v, as a key signs
    signature: concatBytes(signature.slice(1), Uint8Array.of((signature[0] as number) + 27)),
  });
  return { call: { method: "eth_estimateGas", params: [{ to: token, data: hex(data) }] }, data };
}

/** A measuring estimate's execution: less its intrinsic gas. */
export function measured(estimate: number, data: Uint8Array, schedule: GasSchedule): number {
  return estimate - intrinsic(schedule, data).standard;
}

/**
 * The most a transfer can take, from the zero-value one's `measuredGas`: the payer's balance
 * changed, and the payee's changed too if it `payeeHolds` some already, else set from zero.
 */
export function transferGas(
  measuredGas: number,
  schedule: GasSchedule,
  payeeHolds: boolean,
): number {
  return measuredGas + schedule.storageWrite +
    (payeeHolds ? schedule.storageWrite : schedule.storageSet);
}

/** The last block expected before `validBefore`, a slot a block after `latest`. */
export function lastBlockBefore(latest: Latest, validBefore: bigint, slotSeconds: number): number {
  const ahead = (validBefore - 1n - BigInt(latest.timestamp)) / BigInt(slotSeconds);
  // floor division, as the Python's //, for a validBefore already passed
  const floored = (validBefore - 1n - BigInt(latest.timestamp)) < 0n &&
      (validBefore - 1n - BigInt(latest.timestamp)) % BigInt(slotSeconds) !== 0n
    ? ahead - 1n
    : ahead;
  return latest.number + Number(floored);
}
