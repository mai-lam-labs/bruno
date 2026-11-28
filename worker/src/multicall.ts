/**
 * Multicall3's aggregate3: several calls made in order in one transaction, each required to
 * succeed. Where the concurrency account would send consecutive calls the relay bounds each in a
 * transaction of its own, it sends them as one aggregate3 (plan.ts): one transaction's own gas
 * where there were several. Multicall3 treats every caller alike and keeps nothing, so it's a
 * target the account may call directly (targets.json).
 *
 * OWN_GAS is what Multicall3 spends itself making n calls, beside what each call uses and its
 * target's access: measured exactly in the EVM (bruno/tests/test_multicall.py), for calls of a
 * bounded call's calldata (eip3009's CALLDATA_BYTES) that answer nothing. It isn't a line:
 * Multicall3 keeps every call's data and answer in memory, which costs a little more the more
 * there is. The figures hold for Multicall3's own code (CODE_HASH), which a chain is measured to
 * have before it's served. As bruno/multicall.py's.
 */

import { concatBytes } from "@noble/hashes/utils.js";
import { addressWord, encodeBytes, selector, word } from "./abi.ts";
import { CALLDATA_BYTES } from "./eip3009.ts";
import { type Address, bytes, type EncodedCall, WARM_ACCOUNT_ACCESS } from "./evm.ts";
import type { GasSchedule } from "./forks.ts";

export const MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11" as Address;
/** keccak256 of its code, as it is at that address on Ethereum (mds1/multicall3). */
export const CODE_HASH = bytes(
  "0xd5c15df687b16f2ff992fc8d767b4216323184a2bbc6ee2f9c398c318e770891",
);
const AGGREGATE3 = selector("aggregate3((address,bool,bytes)[])");
/** Multicall3's own gas making n calls, by n. */
export const OWN_GAS = [893, 2_160, 3_386, 4_622, 5_861, 7_100, 8_339, 9_579, 10_818, 12_058];
/** The most calls one aggregate3 is planned for. */
export const MOST_CALLS = OWN_GAS.length - 1;

/** Calls that can't be one aggregate3 as the relay plans it: a bug of ours. */
export class GroupError extends Error {}

/**
 * Multicall3's aggregate3 of `calls`, in order, none allowed to fail: one that does reverts
 * them all. GroupError for a call sending ETH, which aggregate3 doesn't.
 */
export function aggregate3(calls: readonly EncodedCall[]): EncodedCall {
  if (calls.some((call) => call.value !== 0n)) {
    throw new GroupError("a call sending ETH isn't grouped");
  }
  // each entry a dynamic tuple (target, allowFailure false, its data after its three words);
  // the list their offsets, then them
  const entries = calls.map((call) =>
    concatBytes(addressWord(call.to), word(0n), word(3n * 32n), encodeBytes(call.data))
  );
  let size = entries.length * 32;
  const offsets = entries.map((entry) => {
    const offset = word(BigInt(size));
    size += entry.length;
    return offset;
  });
  const data = concatBytes(
    AGGREGATE3,
    word(32n),
    word(BigInt(entries.length)),
    ...offsets,
    ...entries,
  );
  return { to: MULTICALL3, data, value: 0n };
}

/**
 * What an aggregate3 of `calls` takes beside what each call uses: Multicall3's own (OWN_GAS),
 * and each call's target's access, cold the first time and warm after. GroupError for more
 * calls than it's measured for, or one whose calldata isn't the length measured.
 */
export function ownGas(calls: readonly EncodedCall[], schedule: GasSchedule): number {
  const own = OWN_GAS[calls.length];
  if (own === undefined || calls.some((call) => call.data.length !== CALLDATA_BYTES)) {
    throw new GroupError("an aggregate3 whose own gas isn't measured");
  }
  const targets = new Set(calls.map((call) => call.to)).size;
  return own + schedule.coldAccountAccess * targets +
    WARM_ACCOUNT_ACCESS * (calls.length - targets);
}
