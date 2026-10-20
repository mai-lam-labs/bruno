/**
 * EVM values shared across the relay, and the gas facts every supported fork shares.
 */

import { keccak_256 } from "@noble/hashes/sha3.js";
import { bytesToHex, hexToBytes } from "@noble/hashes/utils.js";

/** A 0x-prefixed hex string. */
export type Hex = `0x${string}`;

/** An address, EIP-55 checksummed. */
export type Address = Hex & { readonly __address: unique symbol };

/** A warm account's access (EIP-2929), the same in every fork the relay supports. */
export const WARM_ACCOUNT_ACCESS = 100;

/** A call as a transaction would make it: a target, its calldata, and the wei sent. */
export interface EncodedCall {
  readonly to: Address;
  readonly data: Uint8Array;
  readonly value: bigint;
}

export function keccak(data: Uint8Array): Uint8Array {
  return keccak_256(data);
}

export function hex(data: Uint8Array): Hex {
  return `0x${bytesToHex(data)}`;
}

export function bytes(value: Hex): Uint8Array {
  return hexToBytes(value.slice(2));
}

export function utf8(text: string): Uint8Array {
  return new TextEncoder().encode(text);
}

/** `address` (20 bytes, as hex in any case), EIP-55 checksummed. */
export function checksum(address: string): Address {
  const lower = address.toLowerCase().replace(/^0x/, "");
  if (!/^[0-9a-f]{40}$/.test(lower)) {
    throw new Error(`not an address: ${address}`);
  }
  const digest = bytesToHex(keccak(utf8(lower)));
  let out = "0x";
  for (let i = 0; i < 40; i++) {
    const c = lower[i] as string;
    out += parseInt(digest[i] as string, 16) >= 8 ? c.toUpperCase() : c;
  }
  return out as Address;
}

/** ETH where an asset's address is expected (ERC-7528). */
export const NATIVE_ASSET: Address = checksum("0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee");

/** Memory's cost at `words` 32-byte words (Yellow Paper): an expansion costs the difference. */
export function memoryGas(words: number): number {
  return 3 * words + Math.floor((words * words) / 512);
}
