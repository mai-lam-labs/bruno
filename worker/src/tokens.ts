/**
 * What the relay needs to know about a payment token, read from the chain (the Python
 * reference's bruno/tokens.py).
 */

import type { Chain } from "./chain.ts";
import { type Address, bytes, hex } from "./evm.ts";
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
