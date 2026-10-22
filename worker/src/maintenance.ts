/**
 * The float, its concurrency accounts and the payee, read together (the Python reference's
 * bruno/maintenance.py, its survey): what a batch maps submissions onto, and what the root
 * shows of them.
 */

import { derive } from "./accounts.ts";
import { type Chain, quantity, resultOf } from "./chain.ts";
import type { Address } from "./evm.ts";
import { balanceOfCall } from "./tokens.ts";
import type { Signer } from "./transactions.ts";

/** A concurrency account as the chain has it. */
export interface AccountState {
  readonly index: number;
  readonly address: Address;
  readonly balance: bigint;
  readonly nonce: number;
}

/** What an account holds of a token, and the token's decimals and symbol: ETH's as ERC-7528's
 * address. */
export interface Holding {
  readonly token: Address;
  readonly amount: bigint;
  readonly decimals: number;
  readonly symbol: string;
}

/** The float, every concurrency account and the payee, as read at block `block`: each's balance
 * and nonce, and what the payee holds of each token, all as of the one read. */
export interface AccountsStatus {
  readonly block: number;
  readonly floatBalance: bigint;
  readonly floatNonce: number;
  readonly accounts: readonly AccountState[];
  readonly payeeBalance: bigint;
  readonly payeeNonce: number;
  readonly payeeTokens: readonly Holding[];
}

/** The float, concurrency accounts 0 to `count` - 1 and the payee, and what the payee holds of
 * each of `tokens` (each's decimals and symbol), read in one batch as of the latest block,
 * `block`. */
export async function survey(
  chain: Chain,
  float: Signer,
  count: number,
  block: number,
  payee: Address,
  tokens: ReadonlyMap<Address, { decimals: number; symbol: string }>,
): Promise<AccountsStatus> {
  const addresses = Array.from({ length: count }, (_, i) => derive(float, i).address);
  const all = [float.address, ...addresses, payee];
  const answers = (await chain.batch([
    ...all.flatMap((a) => [
      { method: "eth_getBalance", params: [a, "latest"] },
      { method: "eth_getTransactionCount", params: [a, "latest"] },
    ]),
    ...[...tokens.keys()].map((t) => balanceOfCall(t, payee)),
  ])).map((a) => quantity(resultOf(a)));
  const value = (i: number): bigint => answers[i] as bigint;
  const at = 2 * all.length;
  return {
    block,
    floatBalance: value(0),
    floatNonce: Number(value(1)),
    accounts: addresses.map((address, index) => ({
      index,
      address,
      balance: value(2 + 2 * index),
      nonce: Number(value(3 + 2 * index)),
    })),
    payeeBalance: value(at - 2),
    payeeNonce: Number(value(at - 1)),
    payeeTokens: [...tokens].map(([token, { decimals, symbol }], n) => ({
      token,
      amount: value(at + n),
      decimals,
      symbol,
    })),
  };
}

/** `status` as stored: its quantities as decimal strings. */
export function statusToJson(status: AccountsStatus): string {
  return JSON.stringify(status, (_, v) => (typeof v === "bigint" ? v.toString() : v));
}

/** A stored status; null for one stored before the payee was read with the accounts. */
export function statusFromJson(json: string): AccountsStatus | null {
  const s = JSON.parse(json);
  if (s.payeeBalance === undefined) return null;
  return {
    block: s.block,
    floatBalance: BigInt(s.floatBalance),
    floatNonce: s.floatNonce,
    accounts: s.accounts.map((a: { balance: string }) => ({ ...a, balance: BigInt(a.balance) })),
    payeeBalance: BigInt(s.payeeBalance),
    payeeNonce: s.payeeNonce,
    payeeTokens: s.payeeTokens.map((h: { amount: string }) => ({ ...h, amount: BigInt(h.amount) })),
  };
}
