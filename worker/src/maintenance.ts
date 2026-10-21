/**
 * The float and its concurrency accounts, read together (the Python reference's
 * bruno/maintenance.py, its survey): what a batch maps submissions onto, and what the root
 * shows of them.
 */

import { derive } from "./accounts.ts";
import type { Chain } from "./chain.ts";
import type { Address } from "./evm.ts";
import type { Signer } from "./transactions.ts";

/** A concurrency account as the chain has it. */
export interface AccountState {
  readonly index: number;
  readonly address: Address;
  readonly balance: bigint;
  readonly nonce: number;
}

/** The float and every concurrency account, as read at block `block`. */
export interface AccountsStatus {
  readonly block: number;
  readonly floatBalance: bigint;
  readonly floatNonce: number;
  readonly accounts: readonly AccountState[];
}

/** The float and concurrency accounts 0 to `count` - 1, read in one batch as of the latest
 * block, `block`. */
export async function survey(
  chain: Chain,
  float: Signer,
  count: number,
  block: number,
): Promise<AccountsStatus> {
  const addresses = Array.from({ length: count }, (_, i) => derive(float, i).address);
  const [floatState, ...states] = await chain.accounts([float.address, ...addresses]);
  const { balance, nonce } = floatState as { balance: bigint; nonce: number };
  return {
    block,
    floatBalance: balance,
    floatNonce: nonce,
    accounts: states.map((s, index) => ({
      index,
      address: addresses[index] as Address,
      balance: s.balance,
      nonce: s.nonce,
    })),
  };
}

/** `status` as stored: its quantities as decimal strings. */
export function statusToJson(status: AccountsStatus): string {
  return JSON.stringify(status, (_, v) => (typeof v === "bigint" ? v.toString() : v));
}

export function statusFromJson(json: string): AccountsStatus {
  const s = JSON.parse(json);
  return {
    block: s.block,
    floatBalance: BigInt(s.floatBalance),
    floatNonce: s.floatNonce,
    accounts: s.accounts.map((a: { balance: string }) => ({ ...a, balance: BigInt(a.balance) })),
  };
}
