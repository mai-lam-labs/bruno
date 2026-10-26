/**
 * Amounts as callers give them: whole units exactly, as a string or a number, never rounded;
 * a token named by its alias or address.
 */

import { assertEquals, assertThrows } from "@std/assert";
import { Bruno } from "../src/client.ts";
import { baseUnitsOf, ETH, TokenError, tokenOf, valueOf } from "../src/tokens.ts";

const SEPOLIA = 11_155_111;
const USDC = tokenOf(SEPOLIA, "USDC");
const UNKNOWN = tokenOf(SEPOLIA, "0x000000000000000000000000000000000000bEEF");

Deno.test("whole units, as a string or a number, exactly", () => {
  assertEquals(baseUnitsOf({ decimal: "2.5" }, USDC), 2_500_000n);
  assertEquals(baseUnitsOf({ decimal: 2.5 }, USDC), 2_500_000n);
  assertEquals(baseUnitsOf({ decimal: "0.000001" }, USDC), 1n);
  assertEquals(baseUnitsOf({ decimal: 1e-6 }, USDC), 1n);
  assertEquals(baseUnitsOf({ decimal: 1e21 }, tokenOf(SEPOLIA, "ETH")), 10n ** 39n);
  assertEquals(baseUnitsOf({ decimal: 1.5e-7 }, tokenOf(SEPOLIA, "WETH")), 150_000_000_000n);
  assertEquals(baseUnitsOf({ baseUnits: 7n }, UNKNOWN), 7n);
});

Deno.test("never rounded: more digits than the token has, or not a decimal, is refused", () => {
  assertThrows(() => baseUnitsOf({ decimal: "0.0000001" }, USDC), TokenError);
  assertThrows(() => baseUnitsOf({ decimal: 0.1 + 0.2 }, USDC), TokenError);
  for (const bad of ["", "-1", "1.", ".5", "1e3", "01", " 1", "1,5"]) {
    assertThrows(() => baseUnitsOf({ decimal: bad }, USDC), TokenError, undefined, bad);
  }
  for (const bad of [Number.NaN, Number.POSITIVE_INFINITY, -1]) {
    assertThrows(() => baseUnitsOf({ decimal: bad }, USDC), TokenError);
  }
  assertThrows(() => baseUnitsOf({ baseUnits: -1n }, USDC), TokenError);
});

Deno.test("a token's decimals not known: whole units are refused, base units taken", () => {
  assertEquals([UNKNOWN.symbol, UNKNOWN.decimals], [null, null]);
  assertThrows(() => baseUnitsOf({ decimal: "1" }, UNKNOWN), TokenError);
  assertEquals(valueOf(UNKNOWN, 7n).decimal, null);
});

Deno.test("aliases, per chain: by alias in any case, or by address", () => {
  assertEquals(tokenOf(SEPOLIA, "usdc"), USDC);
  assertEquals(tokenOf(SEPOLIA, USDC.address.toLowerCase()), USDC);
  assertEquals(tokenOf(1, "ETH").address, ETH);
  assertEquals(tokenOf(1, "USDT").decimals, 6);
  assertThrows(() => tokenOf(SEPOLIA, "USDT"), TokenError);
  assertEquals(valueOf(USDC, 2_500_000n).decimal, "2.5");
  assertEquals(valueOf(USDC, 3_000_000n).decimal, "3");
  assertEquals(valueOf(USDC, 1n).decimal, "0.000001");
});

Deno.test("a chain's own tokens, given: named as aliases, ahead of the built-in ones", () => {
  // a local chain's USDC, at mainnet's address
  const local = { symbol: "USDC", address: tokenOf(1, "USDC").address, decimals: 6 };
  assertEquals(tokenOf(31_337, "usdc", [local]), local);
  assertEquals(tokenOf(31_337, local.address.toLowerCase(), [local]), local);
  assertThrows(() => tokenOf(31_337, "USDC"), TokenError);
  const bruno = new Bruno({ chain: 31_337, tokens: [local] });
  assertEquals([bruno.chain, bruno.token("USDC")], ["31337", local]);
  assertEquals(baseUnitsOf({ decimal: "2.5" }, bruno.token("USDC")), 2_500_000n);
});
