/**
 * What the relay needs to know about a payment token, read from the chain (the Python
 * reference's bruno/tokens.py): the calls, which the relay batches as it opens, and what they
 * answer.
 */

import type { Call } from "./chain.ts";
import { type Address, bytes, hex } from "./evm.ts";
import { type GasSchedule, intrinsic } from "./forks.ts";
import type { Erc20 } from "./runner.ts";

function balanceOf(holder: Address): Uint8Array {
  return Uint8Array.from([...bytes("0x70a08231"), ...new Uint8Array(12), ...bytes(holder)]);
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

/** `token`'s decimals(). */
export function decimalsCall(token: Address): Call {
  return { method: "eth_call", params: [{ to: token, data: "0x313ce567" }, "latest"] };
}

/** `token`'s symbol(). */
export function symbolCall(token: Address): Call {
  return { method: "eth_call", params: [{ to: token, data: "0x95d89b41" }, "latest"] };
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
