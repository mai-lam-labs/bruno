/**
 * What the relay knows of a chain by measuring it, never by assuming it: the nodes' chain id,
 * the fork in force, whether the deployer is there, each token's gas, decimals and symbol, and
 * the payee's holdings of each x402 token. One batch, taken again once it's an hour old. A port
 * of the Python reference (bruno/measurements.py).
 */

import { type Answer, type Chain, emptyCall, hexOf, quantity, resultOf } from "./chain.ts";
import type { ChainConfig, Eip712Domain } from "./config.ts";
import { DEPLOYER, RUNTIME } from "./deployer.ts";
import { measured as measuredTransfer, measuring as measuringTransfer } from "./eip3009.ts";
import { type Address, bytes, hex, NATIVE_ASSET } from "./evm.ts";
import { type GasSchedule, scheduleOf } from "./forks.ts";
import type { Erc20 } from "./runner.ts";
import { balanceOfCall, decimalsCall, erc20, measuring, symbolCall, symbolOf } from "./tokens.ts";

/** A chain as measured at `block` (`at`, Date.now()): its fork's schedule, each token's
 * measured gas (null for ETH), decimals and symbol, the x402 tokens the payee holds; each token
 * suspended and why, each taken without x402 and why; and why the chain is unavailable. */
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
  readonly unavailable: string | null;
}

/** What's wrong, each a line: none if all's well. */
export function problems(m: Measurements): string[] {
  return [
    ...(m.unavailable === null ? [] : [`unavailable: ${m.unavailable}`]),
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
    ...erc20s.map((t) => measuring(t, payee)),
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
  const schedule = scheduleOf(emptyCallGas);
  let unavailable: string | null = null;
  if (nodes !== chain.chainId) {
    unavailable = `the nodes are chain ${nodes}, not ${chain.chainId}`;
  } else if (schedule === null) {
    unavailable = `a fork no schedule knows: a plain call costs ${emptyCallGas}`;
  } else if (hexOf(resultOf(at(3))) !== hex(RUNTIME)) {
    unavailable = `the deployer isn't at ${DEPLOYER}`;
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
        tokens.set(token, erc20(token, payee, Number(quantity(resultOf(at(4 + i)))), schedule));
        decimals.set(token, Number(quantity(resultOf(at(4 + n + i)))));
        symbols.set(token, symbolOf(bytes(hexOf(resultOf(at(4 + 2 * n + i))))));
      } catch (e) {
        suspended.set(token, `unmeasured: ${e instanceof Error ? e.message : e}`);
        tokens.delete(token);
      }
    }
    const first = 4 + 3 * n;
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
        withoutX402.set(
          token,
          `its eip3009 domain's transfer fails: ${e instanceof Error ? e.message : e}`,
        );
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
    unavailable,
  };
}
