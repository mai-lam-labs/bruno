/**
 * EIP-3009's transferWithAuthorization: a payment a client signs off-chain, which the runner
 * makes before the client's call (x402's `exact` scheme on EVM). The calldata, the measurement
 * of its gas, and the block a signed authorization lasts until. A port of the Python
 * reference (bruno/eip3009.py).
 */

import { secp256k1 } from "@noble/curves/secp256k1.js";
import { concatBytes } from "@noble/hashes/utils.js";
import type { Call, Latest } from "./chain.ts";
import type { Eip712Domain } from "./config.ts";
import { type Address, bytes, hex, keccak, utf8 } from "./evm.ts";
import { type GasSchedule, intrinsic } from "./forks.ts";
import { bigIntOf, bytesOf, keyAddress, signer } from "./transactions.ts";

/** The (v, r, s) overload, which every EIP-3009 token has. */
const TRANSFER_WITH_AUTHORIZATION = keccak(
  utf8(
    "transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,uint8,bytes32,bytes32)",
  ),
).slice(0, 4);
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
/** The calldata's length: a selector and nine words. */
export const CALLDATA_BYTES = 4 + 9 * 32;

/**
 * `payer`'s signed authorization for `value` of its token to go to `to`, by anyone, once (its
 * `nonce`), after `validAfter` and before `validBefore` (Unix seconds).
 */
export interface TransferAuthorization {
  readonly payer: Address;
  readonly to: Address;
  readonly value: bigint;
  readonly validAfter: bigint;
  readonly validBefore: bigint;
  readonly nonce: Uint8Array;
  readonly v: number;
  readonly r: bigint;
  readonly s: bigint;
}

/** Its nine fields as ABI words: its calldata's arguments, and its part of a submission's id. */
export function words(a: TransferAuthorization): Uint8Array {
  return concatBytes(
    addressWord(a.payer),
    addressWord(a.to),
    word(a.value),
    word(a.validAfter),
    word(a.validBefore),
    a.nonce,
    word(BigInt(a.v)),
    word(a.r),
    word(a.s),
  );
}

/** transferWithAuthorization(from, to, value, validAfter, validBefore, nonce, v, r, s). */
export function calldata(a: TransferAuthorization): Uint8Array {
  return concatBytes(TRANSFER_WITH_AUTHORIZATION, words(a));
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

/** secp256k1's group order: a signature's s is at most half of it (EIP-2). */
const SECP256K1_N = 0xfffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141n;

/**
 * The account whose key signed `transfer` (ecrecover): null if its signature recovers to none.
 * A token that also takes ERC-1271 signatures (FiatToken v2.2) accepts a contract payer whose
 * signature recovers to someone else, so a mismatch alone doesn't make it invalid.
 */
export function signerOf(
  domain: Eip712Domain,
  chainId: number,
  token: Address,
  transfer: TransferAuthorization,
): Address | null {
  // refused by the token's ECDSA (EIP-2), so by any token
  if (transfer.s > SECP256K1_N / 2n || (transfer.v !== 27 && transfer.v !== 28)) return null;
  const hash = digest(
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
  try {
    const signature = concatBytes(
      Uint8Array.of(transfer.v - 27),
      bytesOf(transfer.r, 32),
      bytesOf(transfer.s, 32),
    );
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
    v: (signature[0] as number) + 27,
    r: bigIntOf(signature.slice(1, 33)),
    s: bigIntOf(signature.slice(33, 65)),
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

function word(value: bigint): Uint8Array {
  return bytesOf(value, 32);
}

function addressWord(address: Address): Uint8Array {
  return concatBytes(new Uint8Array(12), bytes(address));
}
