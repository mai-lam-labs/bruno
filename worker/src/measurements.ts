/**
 * What the relay knows of a chain by measuring it, never by assuming it: the nodes' chain id,
 * the fork in force, whether the deployer is there, whether it's an OP-stack chain whose fees
 * beyond gas our formulas give, each token's gas, decimals and symbol, and the payee's holdings
 * of each x402 token. One batch (and, on an OP-stack chain, one more at its block), taken again
 * once it's an hour old. As bruno/measurements.py's.
 */

import { type Chain, codeOf, emptyCall, hasCode, hexOf, quantity, wordOf } from "./chain.ts";
import { type Answer, resultOf } from "./jsonrpc.ts";
import type { ChainConfig, Eip712Domain } from "./config.ts";
import { agrees, checking, GAS_PRICE_ORACLE, read, reading } from "./datafee.ts";
import { DEPLOYER, RUNTIME } from "./deployer.ts";
import { measured as measuredTransfer, measuring as measuringTransfer } from "./eip3009.ts";
import { type Address, bytes, hex, hexQuantity, NATIVE_ASSET } from "./evm.ts";
import { type GasSchedule, scheduleOf as forkScheduleOf } from "./forks.ts";
import type { Erc20 } from "./runner.ts";
import {
  balanceOfCall,
  decimalsCall,
  erc20,
  measuring as measuringBalance,
  symbolCall,
  symbolOf,
} from "./tokens.ts";

/** `key`'s value in `map`, which has it by construction (a token checked supported first is
 * measured and configured): a miss is a bug of ours, thrown as one, never a default guessed
 * (as bruno's lookups by key raise KeyError). */
export function lookup<K, V>(map: ReadonlyMap<K, V>, key: K, what: string): V {
  if (!map.has(key)) throw new Error(`${what}: none for ${String(key)}`);
  return map.get(key) as V;
}

/** Who can act on a refusal of what the relay can't serve: the client, trying again later
 * (`retry`) or asking for something else (`request`); or only the relay's operator (`operator`)
 * (bruno/measurements.py's Category). */
export type Category = "retry" | "request" | "operator";

/** Why a chain can't be served now (Unavailable): its category the reason's
 * (bruno/measurements.py's Unavailability). */
export type Unavailability =
  | "node-failed"
  | "not-measured"
  | "block-unseen"
  | "min-fee-suspended"
  | "wrong-chain"
  | "unknown-fork"
  | "no-deployer"
  | "unknown-fee-formula";

/** Those that pass by themselves are retried; the rest are the operator's. */
const PASSING: ReadonlySet<Unavailability> = new Set<Unavailability>([
  "node-failed",
  "not-measured",
  "block-unseen",
  "min-fee-suspended",
]);

export function unavailabilityCategory(reason: Unavailability): Category {
  return PASSING.has(reason) ? "retry" : "operator";
}

/** Why a chain can't be served: the `reason`, and its details for people (`why`). */
export interface Unavailable {
  readonly reason: Unavailability;
  readonly why: string;
}

/** A chain as measured at `block` (`at`, Date.now()): its fork's schedule, each token's
 * measured gas (null for ETH), decimals and symbol, the x402 tokens the payee holds; each token
 * suspended and why, each taken without x402 and why; whether it's an OP-stack chain; and why
 * the chain is unavailable. */
export interface Measurements {
  readonly at: number;
  readonly block: number;
  readonly schedule: GasSchedule | null;
  readonly tokens: ReadonlyMap<Address, Erc20 | null>;
  readonly decimals: ReadonlyMap<Address, number>;
  readonly symbols: ReadonlyMap<Address, string>;
  readonly payeeHolds: ReadonlySet<Address>;
  readonly suspended: ReadonlyMap<Address, string>;
  readonly withoutX402: ReadonlyMap<Address, string>;
  readonly opStack: boolean;
  readonly unavailable: Unavailable | null;
}

/** The chain can't be served now: its nodes, its fork or its deployer aren't what the relay
 * needs (measurements), or it hasn't been measured yet; why (`reason`), and its details for
 * people. */
export class UnavailableError extends Error {
  constructor(readonly reason: Unavailability, message: string) {
    super(message);
  }

  static of(unavailable: Unavailable): UnavailableError {
    return new UnavailableError(unavailable.reason, unavailable.why);
  }
}

/** Where the tokens' answers start in measure's batch: after the chain id, the block, the empty
 * call, the deployer's code and the fee oracle's. */
const TOKENS = 5;

/** What's wrong, each a line: none if all's well. */
export function problems(m: Measurements): string[] {
  return [
    ...(m.unavailable === null ? [] : [`unavailable: ${m.unavailable.why}`]),
    ...[...m.suspended].map(([t, why]) => `${t} suspended: ${why}`),
    ...[...m.withoutX402].map(([t, why]) => `${t} not over x402: ${why}`),
  ];
}

/** `m`, the payee's holdings of each measured token as `holdings` says. */
export function holding(m: Measurements, holdings: ReadonlyMap<Address, bigint>): Measurements {
  const holds = new Set(m.payeeHolds);
  for (const [token, amount] of holdings) {
    if (amount > 0n) holds.add(token);
    else holds.delete(token);
  }
  return { ...m, payeeHolds: holds };
}

/** The chain measured, in one batch: throw only if the nodes don't answer it at all. */
export async function measure(
  chain: Chain,
  config: ChainConfig,
  payee: Address,
): Promise<Measurements> {
  const erc20s = config.tokens.map((t) => t.address).filter((t) => t !== NATIVE_ASSET);
  const domains = new Map<Address, Eip712Domain>();
  for (const t of config.tokens) {
    if (t.eip3009 !== null && erc20s.includes(t.address)) domains.set(t.address, t.eip3009);
  }
  const transfers = [...domains].map(([t, d]) => measuringTransfer(d, chain.chainId, t, payee));
  const answers = await chain.batch([
    { method: "eth_chainId", params: [] },
    { method: "eth_getBlockByNumber", params: ["latest", false] },
    emptyCall(),
    { method: "eth_getCode", params: [DEPLOYER, "latest"] },
    { method: "eth_getCode", params: [GAS_PRICE_ORACLE, "latest"] },
    ...erc20s.map((t) => measuringBalance(t, payee)),
    ...erc20s.map(decimalsCall),
    ...erc20s.map(symbolCall),
    ...transfers.map((t) => t.call),
    ...[...domains.keys()].map((t) => balanceOfCall(t, payee)),
  ]);
  const at = (i: number): Answer => {
    const answer = answers[i];
    if (answer === undefined) throw new Error(`no answer ${i}`);
    return answer;
  };
  const nodes = Number(quantity(resultOf(at(0))));
  const block = Number(quantity((resultOf(at(1)) as { number: string }).number));
  const emptyCallGas = Number(quantity(resultOf(at(2))));
  const opStack = hasCode(resultOf(at(4)));
  const schedule = forkScheduleOf(emptyCallGas);
  let unavailable: Unavailable | null = null;
  if (nodes !== chain.chainId) {
    const why = `the nodes are chain ${nodes}, not ${chain.chainId}`;
    unavailable = { reason: "wrong-chain", why };
  } else if (schedule === null) {
    const why = `a fork no schedule knows: a plain call costs ${emptyCallGas}`;
    unavailable = { reason: "unknown-fork", why };
  } else if (hex(codeOf(resultOf(at(3)))) !== hex(RUNTIME)) {
    unavailable = { reason: "no-deployer", why: `the deployer isn't at ${DEPLOYER}` };
  } else if (opStack && !(await dataFeeKnown(chain, block))) {
    const why = "an OP-stack fee formula Bruno doesn't know: its oracle answers otherwise";
    unavailable = { reason: "unknown-fee-formula", why };
  }
  const tokens = new Map<Address, Erc20 | null>([[NATIVE_ASSET, null]]);
  const decimals = new Map<Address, number>([[NATIVE_ASSET, 18]]);
  const symbols = new Map<Address, string>([[NATIVE_ASSET, "ETH"]]);
  const suspended = new Map<Address, string>();
  const withoutX402 = new Map<Address, string>();
  const payeeHolds = new Set<Address>();
  if (schedule !== null) {
    const n = erc20s.length;
    for (const [i, token] of erc20s.entries()) {
      try {
        tokens.set(
          token,
          erc20(token, payee, Number(quantity(resultOf(at(TOKENS + i)))), schedule),
        );
        decimals.set(token, Number(quantity(resultOf(at(TOKENS + n + i)))));
        symbols.set(token, symbolOf(bytes(hexOf(resultOf(at(TOKENS + 2 * n + i))))));
      } catch (e) {
        console.warn(`chain ${chain.chainId}: ${token} unmeasured:`, e);
        suspended.set(token, "its balanceOf, decimals() or symbol() didn't measure");
        tokens.delete(token);
      }
    }
    const first = TOKENS + 3 * n;
    for (const [i, token] of [...domains.keys()].entries()) {
      const current = tokens.get(token);
      const transfer = transfers[i];
      if (current === undefined || current === null || transfer === undefined) continue;
      try {
        const gas = measuredTransfer(
          Number(quantity(resultOf(at(first + i)))),
          transfer.data,
          schedule,
        );
        tokens.set(token, { ...current, transferGas: gas });
        if (quantity(resultOf(at(first + domains.size + i))) > 0n) payeeHolds.add(token);
      } catch (e) {
        // taken without x402: only its transfer failed (a domain not the token's, say)
        console.warn(`chain ${chain.chainId}: ${token}'s transfer unmeasured:`, e);
        withoutX402.set(token, "a transfer signed for its eip3009 domain didn't measure");
      }
    }
  }
  return {
    at: Date.now(),
    block,
    schedule,
    tokens,
    decimals,
    symbols,
    payeeHolds,
    suspended,
    withoutX402,
    opStack,
    unavailable,
  };
}

/** Whether, at `block`, the chain's fee oracle answers what datafee's formulas give from the
 * inputs read at the same block: one batch, its reads all of that block. */
async function dataFeeKnown(chain: Chain, block: number): Promise<boolean> {
  const tag = hexQuantity(block);
  const inputs = reading(tag);
  const answers = await chain.batch([...inputs, ...checking(tag)]);
  try {
    const values = answers.map((a) => wordOf(resultOf(a)));
    return agrees(read(values.slice(0, inputs.length)), values.slice(inputs.length));
  } catch (e) {
    console.warn(`chain ${chain.chainId}: the fee oracle didn't answer:`, e);
    return false;
  }
}

/** The measured schedule of an available chain. */
export function scheduleOf(measured: Measurements): GasSchedule {
  if (measured.schedule === null) {
    if (measured.unavailable !== null) throw UnavailableError.of(measured.unavailable);
    throw new UnavailableError("unknown-fork", "no schedule measured");
  }
  return measured.schedule;
}
