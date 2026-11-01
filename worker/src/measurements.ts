/**
 * What the relay knows of a chain by measuring it, never by assuming it: the nodes' chain id,
 * the fork in force, whether the deployer is there, whether it's an OP-stack chain whose fees
 * beyond gas our formulas give, each token's gas, decimals and symbol, and the payee's holdings
 * of each x402 token. One batch (and, on an OP-stack chain, one more at its block), taken again
 * once it's an hour old. As bruno/measurements.py's.
 */

import {
  type Chain,
  ChainError,
  codeOf,
  emptyCall,
  hasCode,
  hexOf,
  type Latest,
  quantity,
  wordOf,
} from "./chain.ts";
import { type Answer, JsonRpcError, resultOf } from "./jsonrpc.ts";
import { type AccountsStatus, statusFromJson, statusToJson } from "./maintenance.ts";
import type { Store } from "./store.ts";
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

/** How soon measurements that couldn't be taken again are tried again, the last kept. */
const MEASURE_RETRY_MS = 60_000;
/** How many of the last bugs of ours in a submission's work the root lists. */
export const INTERNAL_ERRORS_KEPT = 5;
/** Why a chain is unavailable before its first measurement. */
const UNMEASURED = "not measured yet: its nodes didn't answer";

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

/**
 * What the relay knows of its chain as last read: the measurements, taken again once
 * `measureSeconds` old, one measuring at a time; the float and its accounts as last read, by a
 * batch or the treasury; and the last bugs of ours a submission's work met. What's wrong with
 * the chain, the root shows, as stored ("problems", "internalErrors", "accounts").
 */
export class Measuring {
  /** The chain as last measured, and the measuring in flight, if any. */
  private measured: Measurements | null = null;
  private measuring: Promise<Measurements> | null = null;
  /** With nothing measured, when the nodes may be asked again after they didn't answer (ms). */
  private unmeasuredUntil = 0;
  /** The last bugs of ours a submission's work met, each its alone: listed with problems. */
  private internalErrors: string[];

  constructor(
    private readonly chain: Chain,
    private readonly config: ChainConfig,
    private readonly store: Store,
    /** Each token taken over x402, and its EIP-712 domain, as configured. */
    private readonly domains: ReadonlyMap<Address, Eip712Domain>,
  ) {
    this.internalErrors = JSON.parse(store.get("internalErrors") ?? "[]");
  }

  /** The chain as measured: measured again once `measureSeconds` old. If the nodes don't
   * answer, the last are kept and tried again a minute later; with none, unavailable. What's
   * wrong is stored, for the root. */
  async measurements(): Promise<Measurements> {
    if (this.fresh(this.measured)) return this.measured;
    this.measuring ??= this.remeasure().finally(() => {
      this.measuring = null;
    });
    return await this.measuring;
  }

  /** Measured again: the last kept, tried again a minute later, if the nodes don't answer; with
   * none, unavailable, not tried again for a minute. Why stays in the log: a node's URL isn't a
   * client's. */
  private async remeasure(): Promise<Measurements> {
    if (this.measured === null && Date.now() < this.unmeasuredUntil) {
      throw new UnavailableError("not-measured", UNMEASURED);
    }
    let measured: Measurements;
    try {
      measured = await measure(this.chain, this.config, this.config.payee);
    } catch (e) {
      if (!(e instanceof ChainError || e instanceof JsonRpcError)) {
        // a bug of ours: reported, nothing measured, not asked again yet
        console.error(`chain ${this.chain.chainId}: measuring: internal error`, e);
        this.internalError("measuring", e);
        this.measured = null;
        this.unmeasuredUntil = Date.now() + MEASURE_RETRY_MS;
        throw new UnavailableError("not-measured", UNMEASURED);
      }
      console.warn(`chain ${this.chain.chainId}: measuring:`, e);
      if (this.measured === null) {
        this.unmeasuredUntil = Date.now() + MEASURE_RETRY_MS;
        throw new UnavailableError("not-measured", UNMEASURED);
      }
      const retryAt = this.config.measureSeconds * 1000 - MEASURE_RETRY_MS;
      measured = { ...this.measured, at: Date.now() - retryAt };
    }
    for (const problem of problems(measured)) {
      console.error(`chain ${this.chain.chainId}: ${problem}`);
    }
    this.store.put("problems", JSON.stringify(problems(measured)));
    this.measured = measured;
    return measured;
  }

  private fresh(m: Measurements | null): m is Measurements {
    return m !== null && Date.now() - m.at < this.config.measureSeconds * 1000;
  }

  /** The measurements, if the chain can be served; else UnavailableError, why. */
  async available(): Promise<Measurements> {
    const measured = await this.measurements();
    if (measured.unavailable !== null) throw UnavailableError.of(measured.unavailable);
    return measured;
  }

  /** The latest block, with what's paid beyond gas on an OP-stack chain: measured first (from
   * the last measurements, read again only once they're old), so it's known which. */
  async latest(): Promise<Latest> {
    let opStack = false;
    try {
      opStack = (await this.measurements()).opStack;
    } catch (e) {
      if (!(e instanceof UnavailableError)) throw e; // nothing priced or sent until measured
    }
    return await this.chain.latest(opStack);
  }

  /** A bug of ours, logged where it was caught: listed with the chain's problems (the root reads
   * them stored, as "internalErrors"). */
  internalError(what: string, e: unknown): void {
    const name = e instanceof Error ? e.name : typeof e;
    this.internalErrors = [...this.internalErrors, `internal error ${what}: ${name}`].slice(
      -INTERNAL_ERRORS_KEPT,
    );
    this.store.put("internalErrors", JSON.stringify(this.internalErrors));
  }

  /** The float and its accounts as last read, by a batch or the treasury: what the root
   * shows. */
  get accountsStatus(): AccountsStatus | null {
    const stored = this.store.get("accounts");
    return stored === undefined ? null : statusFromJson(stored);
  }

  /** `status` kept as the accounts' latest, unless one read at a later block is. */
  recordAccounts(status: AccountsStatus): void {
    const kept = this.accountsStatus;
    if (kept === null || status.block >= kept.block) {
      this.store.put("accounts", statusToJson(status));
    }
  }

  /** What the payee holds of each x402 token, as the treasury last read it. */
  recordPayee(holdings: ReadonlyMap<Address, bigint>): void {
    if (this.measured === null) return;
    this.measured = holding(
      this.measured,
      new Map([...holdings].filter(([token]) => this.domains.has(token))),
    );
  }
}
