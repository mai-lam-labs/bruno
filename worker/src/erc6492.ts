/**
 * ERC-6492: a signature from a contract account that may not be deployed yet, carrying its
 * deployment. A wrapped signature is abi.encode((address, bytes, bytes), (factory,
 * factoryCalldata, signature)) followed by the 32 magic bytes, 0x6492 repeated: the factory,
 * called with the calldata, deploys the account (or prepares one that's deployed but can't check
 * the signature yet), and the inner signature is then the account's ERC-1271 one.
 *
 * A deployment is the same triple a submission can also name for itself: an account, and the
 * factory call that deploys it (ERC-4337's `factory` and `factoryData`). A port of the Python
 * reference (bruno/erc6492.py), decoding as its eth_abi does: padding must be zero and data
 * within bounds; trailing bytes and shared offsets are taken.
 */

import { concatBytes } from "@noble/hashes/utils.js";
import { type Address, bytes, checksum, type Hex, hex } from "./evm.ts";
import { bigIntOf } from "./transactions.ts";
import { addressWord, encodeBytes, word } from "./abi.ts";

export const MAGIC: Uint8Array = bytes(`0x${"6492".repeat(16)}` as Hex);

/** A signature ends in ERC-6492's magic bytes, but what's before them isn't its wrapper. */
export class WrapperError extends Error {}

/** `account`, deployed by calling `factory` with `factoryData` when it has no code. */
export interface Deployment {
  readonly account: Address;
  readonly factory: Address;
  readonly factoryData: Uint8Array;
}

/** Its fields, (address,address,bytes), ABI-encoded: its part of a submission's id. */
export function deploymentEncoded(d: Deployment): Uint8Array {
  return concatBytes(
    addressWord(d.account),
    addressWord(d.factory),
    word(3n * 32n),
    encodeBytes(d.factoryData),
  );
}

export function sameDeployment(a: Deployment, b: Deployment): boolean {
  return a.account === b.account && a.factory === b.factory &&
    hex(a.factoryData) === hex(b.factoryData);
}

/** Whether `signature` is ERC-6492's wrapper: it ends in the magic bytes. */
export function wrapped(signature: Uint8Array): boolean {
  if (signature.length < MAGIC.length) return false;
  const tail = signature.subarray(signature.length - MAGIC.length);
  return tail.every((b, i) => b === MAGIC[i]);
}

/**
 * `signature`'s inner signature, and the deployment of `account` its wrapper carries: as it is,
 * and null, if it isn't wrapped.
 */
export function unwrapped(
  signature: Uint8Array,
  account: Address,
): { signature: Uint8Array; deployment: Deployment | null } {
  if (!wrapped(signature)) return { signature, deployment: null };
  const payload = signature.subarray(0, signature.length - MAGIC.length);
  const head = (index: number): Uint8Array => {
    const at = index * 32;
    if (at + 32 > payload.length) throw new WrapperError("not ERC-6492's wrapper: too short");
    return payload.subarray(at, at + 32);
  };
  const factoryWord = head(0);
  if (factoryWord.subarray(0, 12).some((b) => b !== 0)) {
    throw new WrapperError("not ERC-6492's wrapper: an address's padding isn't empty");
  }
  const factory = checksum(hex(factoryWord.subarray(12)));
  const data = dynamicBytes(payload, bigIntOf(head(1)));
  const inner = dynamicBytes(payload, bigIntOf(head(2)));
  return { signature: inner, deployment: { account, factory, factoryData: data } };
}

/** The bytes at `offset` in `payload`: a length word, then the data padded with zeros. */
function dynamicBytes(payload: Uint8Array, offset: bigint): Uint8Array {
  if (offset + 32n > BigInt(payload.length)) {
    throw new WrapperError("not ERC-6492's wrapper: an offset past its end");
  }
  const at = Number(offset);
  const length = bigIntOf(payload.subarray(at, at + 32));
  const padded = ((length + 31n) / 32n) * 32n;
  if (BigInt(at) + 32n + padded > BigInt(payload.length)) {
    throw new WrapperError("not ERC-6492's wrapper: data past its end");
  }
  const start = at + 32;
  const data = payload.subarray(start, start + Number(length));
  if (payload.subarray(start + Number(length), start + Number(padded)).some((b) => b !== 0)) {
    throw new WrapperError("not ERC-6492's wrapper: padding isn't empty");
  }
  return data.slice();
}
