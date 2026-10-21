/**
 * A chain's configuration: the same JSON as the Python reference's (DEPLOYMENT.md,
 * Configuration), checked as it's read. Secrets aren't in it: the float's key is a binding.
 */

import type { Mode } from "./bundle.ts";
import { type Address, checksum, NATIVE_ASSET } from "./evm.ts";
import { Fraction } from "./fraction.ts";
import type { Hop, PoolKind } from "./prices.ts";

export class ConfigError extends Error {}

/** A token's EIP-712 domain as its EIP-3009 authorizations are signed for: its name() and
 * version(); its chain id and address complete it. */
export interface Eip712Domain {
  readonly name: string;
  readonly version: string;
}

/** A token accepted in payment; one with `eip3009` over x402 too. */
export interface TokenConfig {
  readonly address: Address;
  readonly price: readonly Hop[];
  readonly marginMultiplier: Fraction;
  readonly eip3009: Eip712Domain | null;
}

/** The least our fee is on a transaction: `amount` whole tokens (by its decimals()) of `token`,
 * one the chain accepts. */
export interface MinFee {
  readonly token: Address;
  readonly amount: Fraction;
  /** The amount as written: x402's minimum price, in OpenAPI. */
  readonly text: string;
}

/** Converting the payee's revenue to ETH for the float (the Python reference's TreasuryConfig). */
export interface TreasuryConfig {
  /** Uniswap's SwapRouter02. */
  readonly router: Address;
  readonly intervalSeconds: number;
  readonly runwayDays: Fraction;
  readonly maxCostMultiplier: Fraction;
  readonly slippageMultiplier: Fraction;
}

export interface BundlerConfig {
  readonly url: string;
  readonly mode: Mode;
  /** The identity key, as hex: a secret, bound by name. */
  readonly identityKey: string | null;
}

export interface ChainConfig {
  readonly rpc: readonly string[];
  readonly payee: Address;
  readonly weth: Address;
  readonly bundlers: readonly BundlerConfig[];
  readonly tokens: readonly TokenConfig[];
  readonly accounts: number;
  readonly maxPending: number;
  readonly maxDeadlineBlocks: number;
  readonly maxAuthorizations: number;
  readonly retentionSeconds: number;
  /** How old the chain's measurements may get before they're taken again, when next needed. */
  readonly measureSeconds: number;
  readonly maxGas: number;
  readonly maxCallBytes: number;
  readonly marginMultiplier: Fraction;
  /** Our fee is the market's tip a gas, at least this many wei a gas, and at least minFee. */
  readonly minFeePerGas: bigint;
  readonly minFee: MinFee | null;
  readonly priceTtlSeconds: number;
  readonly quoteBlocks: number;
  readonly slotSeconds: number;
  readonly batchSeconds: number;
  readonly fundingMultiplier: Fraction;
  readonly fundingTimeoutSeconds: number;
  readonly treasury: TreasuryConfig | null;
}

type Json = Record<string, unknown>;

/** One chain's entry of the configuration's `chains`. */
export function chainConfig(raw: unknown): ChainConfig {
  const c = object(raw, "chain");
  return {
    rpc: array(c.rpc, "rpc").map((u, i) => string(u, `rpc[${i}]`)),
    payee: address(c.payee, "payee"),
    weth: address(c.weth, "weth"),
    bundlers: array(c.bundlers, "bundlers").map((b, i) => bundler(b, `bundlers[${i}]`)),
    tokens: array(c.tokens, "tokens").map((t, i) =>
      token(t, `tokens[${i}]`, address(c.weth, "weth"))
    ),
    accounts: integer(c.accounts, "accounts", 1),
    maxPending: integer(c.max_pending, "max_pending", 1),
    maxDeadlineBlocks: integer(c.max_deadline_blocks, "max_deadline_blocks", 1),
    maxAuthorizations: integer(c.max_authorizations ?? 1, "max_authorizations", 0),
    retentionSeconds: nonNegative(c.retention_seconds ?? 3600, "retention_seconds"),
    measureSeconds: positive(c.measure_seconds ?? 3600, "measure_seconds"),
    maxGas: integer(c.max_gas, "max_gas", 1),
    maxCallBytes: integer(c.max_call_bytes, "max_call_bytes", 0),
    marginMultiplier: multiplier(c.margin_multiplier ?? "1", "margin_multiplier"),
    minFeePerGas: BigInt(integer(c.min_fee_per_gas ?? 0, "min_fee_per_gas", 0)),
    minFee: c.min_fee === undefined || c.min_fee === null ? null : minFee(c.min_fee, "min_fee"),
    priceTtlSeconds: integer(c.price_ttl_seconds ?? 60, "price_ttl_seconds", 0),
    quoteBlocks: integer(c.quote_blocks ?? 1, "quote_blocks", 1),
    slotSeconds: integer(c.slot_seconds ?? 12, "slot_seconds", 1),
    batchSeconds: positive(c.batch_seconds ?? 12, "batch_seconds"),
    fundingMultiplier: multiplier(c.funding_multiplier ?? "1", "funding_multiplier"),
    fundingTimeoutSeconds: positive(c.funding_timeout_seconds ?? 120, "funding_timeout_seconds"),
    treasury: c.treasury === undefined || c.treasury === null
      ? null
      : treasury(c.treasury, "treasury"),
  };
}

function bundler(raw: unknown, at: string): BundlerConfig {
  const b = object(raw, at);
  const mode = string(b.mode, `${at}.mode`);
  if (mode !== "dropping" && mode !== "standalone") {
    throw new ConfigError(`${at}.mode: dropping or standalone, not ${mode}`);
  }
  return {
    url: string(b.url, `${at}.url`),
    mode,
    identityKey: b.identity_key === undefined || b.identity_key === null
      ? null
      : string(b.identity_key, `${at}.identity_key`),
  };
}

function token(raw: unknown, at: string, weth: Address): TokenConfig {
  const t = object(raw, at);
  const tokenAddress = address(t.address, `${at}.address`);
  const price = array(t.price ?? [], `${at}.price`).map((h, i) => hop(h, `${at}.price[${i}]`));
  if (tokenAddress === NATIVE_ASSET && price.length > 0) {
    throw new ConfigError(`${at}: ETH is priced at 1 wei a wei: it has no pools`);
  }
  if (tokenAddress !== NATIVE_ASSET && tokenAddress !== weth && price.length === 0) {
    throw new ConfigError(`${at}: no pools to price ${tokenAddress} by`);
  }
  return {
    address: tokenAddress,
    price,
    marginMultiplier: multiplier(t.margin_multiplier ?? "1", `${at}.margin_multiplier`),
    eip3009: t.eip3009 === undefined || t.eip3009 === null
      ? null
      : domain(t.eip3009, `${at}.eip3009`),
  };
}

function domain(raw: unknown, at: string): Eip712Domain {
  const d = object(raw, at);
  return { name: string(d.name, `${at}.name`), version: string(d.version, `${at}.version`) };
}

function treasury(raw: unknown, at: string): TreasuryConfig {
  const t = object(raw, at);
  const runwayDays = Fraction.decimal(string(t.runway_days ?? "2", `${at}.runway_days`));
  if (runwayDays.compare(0n) <= 0) {
    throw new ConfigError(`${at}.runway_days: over 0, not ${t.runway_days}`);
  }
  return {
    router: address(t.router, `${at}.router`),
    intervalSeconds: integer(t.interval_seconds ?? 21600, `${at}.interval_seconds`, 1),
    runwayDays,
    maxCostMultiplier: multiplier(t.max_cost_multiplier ?? "1.02", `${at}.max_cost_multiplier`),
    slippageMultiplier: multiplier(t.slippage_multiplier ?? "1.005", `${at}.slippage_multiplier`),
  };
}

function minFee(raw: unknown, at: string): MinFee {
  const m = object(raw, at);
  const text = string(m.amount, `${at}.amount`);
  const amount = Fraction.decimal(text);
  if (amount.compare(0n) <= 0) {
    throw new ConfigError(`${at}.amount: a minimum fee is over 0, not ${m.amount}`);
  }
  return { token: address(m.token, `${at}.token`), amount, text };
}

function hop(raw: unknown, at: string): Hop {
  const h = object(raw, at);
  const kind = string(h.kind, `${at}.kind`);
  if (kind !== "uniswap-v2" && kind !== "uniswap-v3") {
    throw new ConfigError(`${at}.kind: uniswap-v2 or uniswap-v3, not ${kind}`);
  }
  return { kind: kind satisfies PoolKind, pool: address(h.pool, `${at}.pool`) };
}

function object(raw: unknown, at: string): Json {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    throw new ConfigError(`${at}: an object`);
  }
  return raw as Json;
}

function array(raw: unknown, at: string): unknown[] {
  if (!Array.isArray(raw)) {
    throw new ConfigError(`${at}: a list`);
  }
  return raw;
}

function string(raw: unknown, at: string): string {
  if (typeof raw !== "string") {
    throw new ConfigError(`${at}: a string`);
  }
  return raw;
}

function address(raw: unknown, at: string): Address {
  try {
    return checksum(string(raw, at));
  } catch (e) {
    throw new ConfigError(`${at}: ${e instanceof Error ? e.message : e}`);
  }
}

function integer(raw: unknown, at: string, min: number): number {
  if (typeof raw !== "number" || !Number.isSafeInteger(raw) || raw < min) {
    throw new ConfigError(`${at}: an integer, at least ${min}`);
  }
  return raw;
}

function nonNegative(raw: unknown, at: string): number {
  if (typeof raw !== "number" || !(raw >= 0)) {
    throw new ConfigError(`${at}: a number, at least 0`);
  }
  return raw;
}

function positive(raw: unknown, at: string): number {
  if (typeof raw !== "number" || !(raw > 0)) {
    throw new ConfigError(`${at}: a positive number`);
  }
  return raw;
}

/** A multiplier on cost, a string Decimal: "1.1" is 10% over, never under 1. */
function multiplier(raw: unknown, at: string): Fraction {
  const value = Fraction.decimal(string(raw, at));
  if (value.compare(1n) < 0) {
    throw new ConfigError(`${at}: a multiplier is at least 1, not ${raw}`);
  }
  return value;
}
