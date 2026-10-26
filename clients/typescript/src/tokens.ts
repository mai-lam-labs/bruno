/**
 * Tokens and amounts. A token is named by its alias on the chain (`"USDC"`, `"ETH"`) or its
 * address; an amount always says its unit: whole units as a decimal string, or the token's
 * smallest unit (wei, for ETH).
 */

import type { Hex } from "./types.ts";

/** ETH, as ERC-7528 names it among tokens. */
export const ETH: Hex = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE";

/** The chains Bruno serves, by the name its paths take. */
export const CHAIN_IDS: Readonly<Record<string, number>> = { ethereum: 1, sepolia: 11_155_111 };

/** A token: its address, and its alias and decimals where known. */
export interface Token {
  readonly address: Hex;
  readonly symbol: string | null;
  readonly decimals: number | null;
}

/** An amount, its unit named: `{ decimal: "2.5" }` (or `2.5`) in whole units, or
 * `{ baseUnits: 2_500_000n }` in the token's smallest unit (wei, for ETH). A number is read as
 * the decimal it prints as, so one with more digits than the token has (`0.1 + 0.2`) is refused,
 * never rounded. */
export type Amount = { readonly decimal: string | number } | { readonly baseUnits: bigint };

/** An amount of a token, in both units: `decimal` (whole units) null where its decimals aren't
 * known. */
export interface Value {
  readonly token: Token;
  readonly baseUnits: bigint;
  readonly decimal: string | null;
}

/** Each chain's tokens by alias: those Bruno's own deployment takes, which a relay's terms
 * confirm. */
const ALIASES: Readonly<Record<number, readonly Token[]>> = {
  1: [
    { symbol: "ETH", address: ETH, decimals: 18 },
    { symbol: "WETH", address: "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", decimals: 18 },
    { symbol: "USDC", address: "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", decimals: 6 },
    { symbol: "USDT", address: "0xdAC17F958D2ee523a2206206994597C13D831ec7", decimals: 6 },
    { symbol: "DAI", address: "0x6B175474E89094C44Da98b954EedeAC495271d0F", decimals: 18 },
  ],
  11_155_111: [
    { symbol: "ETH", address: ETH, decimals: 18 },
    { symbol: "WETH", address: "0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14", decimals: 18 },
    { symbol: "USDC", address: "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238", decimals: 6 },
  ],
};

const ADDRESS = /^0x[0-9a-fA-F]{40}$/;
const DECIMAL = /^(0|[1-9][0-9]*)(?:\.([0-9]+))?$/;

export class TokenError extends Error {}

/** The token `name` names on `chainId`: an alias, or an address (its alias and decimals if
 * it's an aliased token's). `tokens` are the chain's beyond its built-in aliases, looked up
 * first. */
export function tokenOf(chainId: number, name: string, tokens: readonly Token[] = []): Token {
  const known = [...tokens, ...(ALIASES[chainId] ?? [])];
  if (ADDRESS.test(name)) {
    const token = known.find((t) => t.address.toLowerCase() === name.toLowerCase());
    return token ?? { address: name as Hex, symbol: null, decimals: null };
  }
  const token = known.find((t) => t.symbol?.toUpperCase() === name.toUpperCase());
  if (token === undefined) {
    const names = known.flatMap((t) => t.symbol ?? []).join(", ") || "none";
    throw new TokenError(`no token ${name} on chain ${chainId} (aliases: ${names})`);
  }
  return token;
}

/** `amount` of `token` in its smallest unit: a decimal exactly, never rounded, and only for a
 * token whose decimals are known. */
export function baseUnitsOf(amount: Amount, token: Token): bigint {
  if ("baseUnits" in amount) {
    if (amount.baseUnits < 0n) throw new TokenError(`a negative amount: ${amount.baseUnits}`);
    return amount.baseUnits;
  }
  const decimal = typeof amount.decimal === "number" ? decimalText(amount.decimal) : amount.decimal;
  const match = DECIMAL.exec(decimal);
  if (match === null) throw new TokenError(`not a decimal amount: "${decimal}"`);
  if (token.decimals === null) {
    throw new TokenError(
      `${token.address}'s decimals aren't known: give its amount as { baseUnits }`,
    );
  }
  const fraction = match[2] ?? "";
  if (fraction.length > token.decimals) {
    throw new TokenError(
      `${decimal} has more than ${token.symbol}'s ${token.decimals} decimals`,
    );
  }
  return BigInt(match[1] + fraction.padEnd(token.decimals, "0"));
}

/** `baseUnits` of `token`, in both units. */
export function valueOf(token: Token, baseUnits: bigint): Value {
  return {
    token,
    baseUnits,
    decimal: token.decimals === null ? null : decimalOf(baseUnits, token.decimals),
  };
}

/** A number as the decimal it prints as, its exponent (1e-7, 1e+21) written out. */
function decimalText(n: number): string {
  if (!Number.isFinite(n) || n < 0) throw new TokenError(`not an amount: ${n}`);
  const [mantissa = "", exponent = "0"] = n.toString().split("e");
  const [whole = "", fraction = ""] = mantissa.split(".");
  const shift = Number(exponent);
  const digits = whole + fraction;
  const point = whole.length + shift;
  if (point <= 0) return `0.${"0".repeat(-point)}${digits}`;
  if (point >= digits.length) return digits + "0".repeat(point - digits.length);
  return `${digits.slice(0, point)}.${digits.slice(point)}`;
}

function decimalOf(baseUnits: bigint, decimals: number): string {
  const digits = baseUnits.toString().padStart(decimals + 1, "0");
  const whole = digits.slice(0, digits.length - decimals);
  const fraction = digits.slice(digits.length - decimals).replace(/0+$/, "");
  return fraction === "" ? whole : `${whole}.${fraction}`;
}

/** A token a client will pay in: the most it pays (`max`, the price and any tip together) and
 * what it adds to the price (`tip`, ranking it ahead when the relay's queue is full). */
export interface PayWith {
  readonly token: string;
  readonly max: Amount;
  readonly tip?: Amount;
}
