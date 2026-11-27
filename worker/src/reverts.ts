/**
 * A transaction that reverts when it's simulated or estimated (SimulationFailedError), and what
 * its revert data says, read as a refusal (revertReason): Solidity's reason or panic, the
 * runner's own errors, and the EntryPoint's FailedOp.
 */

import { selector } from "./abi.ts";
import { hex } from "./evm.ts";
import { message, type Refused, refused } from "./refusals.ts";
import { BALANCE_UNREADABLE, GAS_EXHAUSTED, UNDERPAID } from "./runner.ts";
import { FAILED_OP, FAILED_OP_WITH_REVERT } from "./userops.ts";

/** The submission's transaction reverts: with `revertData`, what it says read for its message
 * (revertReason). */
export class SimulationFailedError extends Error {
  readonly refused: Refused;

  constructor(readonly revertData: Uint8Array) {
    const why = revertReason(revertData);
    super(message("simulation-failed", { reason: String(why) }));
    this.refused = why;
  }
}

// Solidity's revert reason and panic
const ERROR = hex(selector("Error(string)"));
const PANIC = hex(selector("Panic(uint256)"));

/** What a simulation's revert data says, as its refusal: Solidity's revert reason or panic, the
 * runner's own errors, the EntryPoint's FailedOp and FailedOpWithRevert (the revert inside it
 * read as any is); anything else by its selector. */
export function revertReason(data: Uint8Array): Refused {
  const head = hex(data.subarray(0, 4));
  if (head === hex(UNDERPAID)) return refused("revert-underpaid");
  if (head === hex(GAS_EXHAUSTED)) return refused("revert-gas-exhausted");
  if (head === hex(BALANCE_UNREADABLE)) return refused("revert-balance-unreadable");
  const failed = head === FAILED_OP ? stringAt(data, 1) : null;
  if (failed !== null) return refused("revert-failed-op", { text: failed });
  const reason = head === FAILED_OP_WITH_REVERT ? stringAt(data, 1) : null;
  const inner = head === FAILED_OP_WITH_REVERT ? bytesAt(data, 2) : null;
  if (reason !== null && inner !== null) {
    const said = String(revertReason(inner));
    return refused("revert-failed-op-with-revert", { text: reason, inner: said });
  }
  const text = head === ERROR ? stringAt(data, 0) : null;
  if (text !== null) return refused("revert-error", { text });
  if (head === PANIC && data.length === 36) {
    return refused("revert-panic", { code: BigInt(hex(data.subarray(4))).toString(16) });
  }
  if (data.length === 0) return refused("revert-empty");
  return refused("revert-other", { selector: head });
}

/** The ABI-encoded string whose offset is argument `index` of `data`'s arguments, as UTF-8; null
 * if it doesn't decode. */
function stringAt(data: Uint8Array, index: number): string | null {
  const raw = bytesAt(data, index);
  if (raw === null) return null;
  try {
    return new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(raw);
  } catch {
    return null;
  }
}

/** The ABI-encoded bytes (or string) whose offset is argument `index` of `data`'s arguments; null
 * if they don't decode. */
function bytesAt(data: Uint8Array, index: number): Uint8Array | null {
  const args = data.subarray(4);
  if (args.length < 32 * (index + 1)) return null;
  const offset = BigInt(hex(args.subarray(32 * index, 32 * index + 32)));
  if (offset + 32n > BigInt(args.length)) return null;
  const start = Number(offset) + 32;
  const length = BigInt(hex(args.subarray(start - 32, start)));
  if (BigInt(start) + length > BigInt(args.length)) return null;
  return args.subarray(start, start + Number(length));
}
