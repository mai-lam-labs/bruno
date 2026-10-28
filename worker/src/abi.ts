/**
 * ABI encoding's pieces, as the Worker writes them by hand where the Python reference uses
 * eth_abi: a word, an address in one, and dynamic bytes with their length.
 */

import { concatBytes } from "@noble/hashes/utils.js";
import { type Address, bytes } from "./evm.ts";
import { bytesOf } from "./transactions.ts";

/** `value` as one 32-byte word. */
export function word(value: bigint): Uint8Array {
  return bytesOf(value, 32);
}

/** `address` as one word, left-padded. */
export function addressWord(address: Address): Uint8Array {
  return concatBytes(new Uint8Array(12), bytes(address));
}

/** `data` as dynamic bytes' tail: its length, then it, right-padded to whole words. */
export function encodeBytes(data: Uint8Array): Uint8Array {
  const padded = new Uint8Array(Math.ceil(data.length / 32) * 32);
  padded.set(data);
  return concatBytes(word(BigInt(data.length)), padded);
}
