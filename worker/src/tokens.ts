/**
 * What the relay needs to know about a payment token, read from the chain (bruno/tokens.py's): the calls, which the relay batches as it opens, and what they
 * answer.
 */

import { concatBytes } from "@noble/hashes/utils.js";
import { addressWord, BALANCE_OF, selector } from "./abi.ts";
import { type Call, ethCall } from "./jsonrpc.ts";
import { type Address, hex } from "./evm.ts";
import { type GasSchedule, intrinsic } from "./forks.ts";
import type { Erc20 } from "./runner.ts";

const DECIMALS = selector("decimals()");
const SYMBOL = selector("symbol()");
const ALLOWANCE = selector("allowance(address,address)");

function balanceOf(holder: Address): Uint8Array {
  return concatBytes(BALANCE_OF, addressWord(holder));
}

/** eth_estimateGas of `token`'s balanceOf(holder): what erc20 measures by. */
export function measuring(token: Address, holder: Address): Call {
  return { method: "eth_estimateGas", params: [{ to: token, data: hex(balanceOf(holder)) }] };
}

/** `token`, its balanceOf gas measured: `estimate` (measuring's answer) less intrinsic. */
export function erc20(
  token: Address,
  holder: Address,
  estimate: number,
  schedule: GasSchedule,
): Erc20 {
  return { address: token, balanceGas: estimate - intrinsic(schedule, balanceOf(holder)).standard };
}

/** `token`'s balanceOf(holder). */
export function balanceOfCall(token: Address, holder: Address): Call {
  return ethCall(token, balanceOf(holder));
}

/** `token`'s allowance(owner, spender). */
export function allowanceCall(token: Address, owner: Address, spender: Address): Call {
  return ethCall(token, concatBytes(ALLOWANCE, addressWord(owner), addressWord(spender)));
}

/** `token`'s decimals(). */
export function decimalsCall(token: Address): Call {
  return ethCall(token, DECIMALS);
}

/** `token`'s symbol(). */
export function symbolCall(token: Address): Call {
  return ethCall(token, SYMBOL);
}

/** A symbol() answer as text: an ABI string, or, as some older tokens answer (MKR's), bytes32
 * padded with zeros; undecodable bytes replaced. */
export function symbolOf(answer: Uint8Array): string {
  const decoder = new TextDecoder();
  if (answer.length === 32) {
    let end = answer.length;
    while (end > 0 && answer[end - 1] === 0) end--;
    return decoder.decode(answer.slice(0, end));
  }
  const length = Number(BigInt(hex(answer.slice(32, 64))));
  return decoder.decode(answer.slice(64, 64 + length));
}
