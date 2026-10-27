/**
 * ERC-6492: a smart account that isn't deployed yet wraps its signature with how it's deployed,
 * abi.encode((address, bytes, bytes), (factory, factoryCalldata, signature)) followed by 32
 * magic bytes, 0x6492 repeated. The client reads the deployment from it, to price it first.
 */

import type { Hex } from "./types.ts";

const MAGIC = "6492".repeat(16);

/** What a wrapped signature carries: its factory, the factory's calldata, and the signature
 * the deployed account checks. */
export interface Unwrapped {
  readonly factory: Hex;
  readonly factoryData: Hex;
  readonly signature: Hex;
}

export class WrapperError extends Error {}

/** Whether `signature` is ERC-6492's wrapper: it ends in the magic bytes. */
export function wrapped(signature: Hex): boolean {
  return signature.toLowerCase().endsWith(MAGIC);
}

/** `signature`'s wrapper read: null if it isn't wrapped. */
export function unwrapped(signature: Hex): Unwrapped | null {
  if (!wrapped(signature)) return null;
  const payload = signature.slice(2, signature.length - MAGIC.length).toLowerCase();
  const word = (at: number): string => {
    const text = payload.slice(at * 2, at * 2 + 64);
    if (text.length !== 64) throw new WrapperError("not ERC-6492's wrapper: too short");
    return text;
  };
  const factoryWord = word(0);
  if (!/^0{24}/.test(factoryWord)) {
    throw new WrapperError("not ERC-6492's wrapper: an address's padding isn't empty");
  }
  const dynamic = (offsetAt: number): Hex => {
    const offset = BigInt(`0x${word(offsetAt)}`);
    if (offset + 32n > BigInt(payload.length / 2)) {
      throw new WrapperError("not ERC-6492's wrapper: an offset past its end");
    }
    const at = Number(offset);
    const length = Number(BigInt(`0x${word(at)}`));
    const data = payload.slice((at + 32) * 2, (at + 32 + length) * 2);
    if (data.length !== length * 2) {
      throw new WrapperError("not ERC-6492's wrapper: data past its end");
    }
    return `0x${data}`;
  };
  return {
    factory: `0x${factoryWord.slice(24)}`,
    factoryData: dynamic(32),
    signature: dynamic(64),
  };
}
