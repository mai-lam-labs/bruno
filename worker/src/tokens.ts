/**
 * What the relay needs to know about a payment token, read from the chain (the Python
 * reference's bruno/tokens.py).
 */

import type { Chain } from "./chain.ts";
import { type Address, bytes, hex, NATIVE_ASSET } from "./evm.ts";
import { type GasSchedule, intrinsic } from "./forks.ts";
import type { Erc20 } from "./runner.ts";

/** A token's balanceOf gas, measured: eth_estimateGas of balanceOf(holder), less intrinsic. */
export async function measureErc20(
  chain: Chain,
  token: Address,
  holder: Address,
  schedule: GasSchedule,
): Promise<Erc20> {
  const data = Uint8Array.from([
    ...bytes("0x70a08231"),
    ...new Uint8Array(12),
    ...bytes(holder),
  ]);
  const estimate = Number(
    BigInt(String(await chain.rpc("eth_estimateGas", [{ to: token, data: hex(data) }]))),
  );
  return { address: token, balanceGas: estimate - intrinsic(schedule, data).standard };
}

/** How many decimals `token` has (its decimals()): ETH's, 18. */
export async function decimals(chain: Chain, token: Address): Promise<number> {
  if (token === NATIVE_ASSET) return 18;
  return Number(BigInt(hex(await chain.call(token, bytes("0x313ce567")))));
}
