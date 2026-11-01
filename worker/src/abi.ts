/**
 * ABI encoding's pieces, written by hand (bruno/abi.py's, and what bruno's eth_abi encodes): a
 * function's selector, a word, an address in one, and dynamic bytes with their length; and
 * ERC-20's balanceOf, which the runner, the user operations' balance reader, tokens and the
 * treasury all call.
 */

import { concatBytes } from "@noble/hashes/utils.js";
import { type Address, bytes, keccak, utf8 } from "./evm.ts";

/** The function `signature`'s selector: its hash's first four bytes. */
export function selector(signature: string): Uint8Array {
  return keccak(utf8(signature)).slice(0, 4);
}

/** `value` as one 32-byte word. */
export function word(value: bigint): Uint8Array {
  if (value < 0n || value >= 1n << 256n) throw new RangeError(`${value} isn't a uint256`);
  return bytes(`0x${value.toString(16).padStart(64, "0")}`);
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

export const BALANCE_OF = selector("balanceOf(address)");
