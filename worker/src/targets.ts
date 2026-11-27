/**
 * The contracts a call may be sent to directly, from a concurrency account, without the runner:
 * targets.json, Python's too (bruno/targets.py). Each is sender-agnostic, so a concurrency
 * account calling it gains nothing there, in its own name, that a later call could use: a whole
 * contract, every function of it; or a contract's listed functions only (`functions`, by
 * signature). Some are on every chain, the rest on the chain they're listed under. A call that
 * isn't to one needs a runner, for isolation: plan.ts's proofOf decides.
 *
 * Some of a target's functions are marked `bounded`: a token's EIP-3009 transfer a key signs,
 * whose gas the relay can compute itself from the token's measured transfer, where it runs no
 * code but the token's. Whether one call of it is bounded is that call's to say (plan.ts): its
 * signer must be a key.
 */

import targetsDocument from "./targets.json" with { type: "json" };
import { selector } from "./abi.ts";
import { type Address, checksum, type EncodedCall, hex } from "./evm.ts";

interface Entry {
  readonly name: string;
  readonly address: string;
  readonly functions?: readonly string[];
  readonly bounded?: readonly string[];
}

interface Document {
  readonly everywhere: readonly Entry[];
  readonly chains: Readonly<Record<string, readonly Entry[]>>;
}

/** A contract a call may be sent to directly: its `name`, the selectors of the functions it may
 * be called with, or null for any, and of those whose gas the relay can bound (`bounded`). */
export interface Target {
  readonly name: string;
  readonly selectors: ReadonlySet<string> | null;
  readonly bounded: ReadonlySet<string>;
}

/** Whether a call with `data` may be sent to `target` directly. */
function takes(target: Target, data: Uint8Array): boolean {
  return target.selectors === null || target.selectors.has(hex(data.subarray(0, 4)));
}

function targets(entries: readonly Entry[]): ReadonlyMap<Address, Target> {
  return new Map(entries.map((e) => [checksum(e.address), {
    name: e.name,
    selectors: e.functions === undefined ? null : new Set(e.functions.map((f) => hex(selector(f)))),
    bounded: new Set((e.bounded ?? []).map((f) => hex(selector(f)))),
  }]));
}

const DOCUMENT: Document = targetsDocument;
const EVERYWHERE = targets(DOCUMENT.everywhere);
const ON_CHAIN = new Map(
  Object.entries(DOCUMENT.chains).map(([chain, entries]) => [Number(chain), targets(entries)]),
);

/** Whether `call` may be sent directly on chain `chainId`: to a target, with one of its functions,
 * sending no ETH. */
export function direct(chainId: number, call: EncodedCall): boolean {
  if (call.value !== 0n) return false;
  const target = targetOf(chainId, call.to);
  return target !== undefined && takes(target, call.data);
}

/** Whether `call` is to a function the relay can bound the gas of, on chain `chainId`: a call
 * that may be sent directly, to one of its target's `bounded` functions. */
export function boundable(chainId: number, call: EncodedCall): boolean {
  const target = targetOf(chainId, call.to);
  return direct(chainId, call) && target !== undefined &&
    target.bounded.has(hex(call.data.subarray(0, 4)));
}

function targetOf(chainId: number, to: Address): Target | undefined {
  return EVERYWHERE.get(to) ?? ON_CHAIN.get(chainId)?.get(to);
}
