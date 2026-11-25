/**
 * The float, its concurrency accounts and the payee, read together (bruno/maintenance.py's
 * survey): what a batch maps submissions onto, and what the root
 * shows of them.
 */

import { derive } from "./accounts.ts";
import { type Chain, ChainError, quantity, wordOf } from "./chain.ts";
import { type Answer, JsonRpcError, resultOf } from "./jsonrpc.ts";
import { type Address, hexQuantity } from "./evm.ts";
import { balanceOfCall } from "./tokens.ts";
import type { Signer } from "./transactions.ts";

/** A concurrency account as the chain has it. */
export interface AccountState {
  readonly index: number;
  readonly address: Address;
  readonly balance: bigint;
  readonly nonce: number;
}

/** Whether `account` is in the state: a transfer to one that isn't creates it. */
export function exists(account: AccountState): boolean {
  return account.balance > 0n || account.nonce > 0;
}

/** Concurrency accounts `indices`, read from the chain in one batch. */
export async function monitor(
  chain: Chain,
  floatAccount: Signer,
  indices: readonly number[],
  block: string,
): Promise<AccountState[]> {
  const accounts = indices.map((index) => ({
    index,
    address: derive(floatAccount, index).address,
  }));
  const answers = (await chain.batch(
    accounts.flatMap(({ address }) => [
      { method: "eth_getBalance", params: [address, block] },
      { method: "eth_getTransactionCount", params: [address, block] },
    ]),
    { atomic: true },
  )).map(resultOf);
  return accounts.map(({ index, address }, i) => ({
    index,
    address,
    balance: quantity(answers[2 * i]),
    nonce: Number(quantity(answers[2 * i + 1])),
  }));
}

/** A token's decimals and symbol: how its amounts are written. */
export interface Denomination {
  readonly decimals: number;
  readonly symbol: string;
}

/** What an account holds of a token, and the token's decimals and symbol: ETH's as ERC-7528's
 * address. */
export interface Holding {
  readonly token: Address;
  readonly amount: bigint;
  readonly decimals: number;
  readonly symbol: string;
}

/** The float, every concurrency account, the payee and its forwarder, as read at block `block`:
 * each's balance and nonce (the forwarder's balance only), and what the payee and the forwarder
 * hold of each token, all as of the one read. */
export interface AccountsStatus {
  readonly block: number;
  readonly floatBalance: bigint;
  readonly floatNonce: number;
  readonly accounts: readonly AccountState[];
  readonly payeeBalance: bigint;
  readonly payeeNonce: number;
  readonly payeeTokens: readonly Holding[];
  readonly forwarderBalance: bigint;
  readonly forwarderTokens: readonly Holding[];
}

/** What an account holds of each of `tokens`, from its balanceOf's `answers` in their order, for
 * a read only the root shows (the forwarder's): a token whose answer isn't a balance is left out
 * and logged, so what a token says of that account stops nothing. */
export function shownHoldings(
  tokens: ReadonlyMap<Address, Denomination>,
  answers: readonly Answer[],
): Holding[] {
  const holdings: Holding[] = [];
  for (const [n, [token, { decimals, symbol }]] of [...tokens].entries()) {
    try {
      holdings.push({ token, amount: wordOf(resultOf(answers[n] as Answer)), decimals, symbol });
    } catch (e) {
      if (!(e instanceof JsonRpcError || e instanceof ChainError)) throw e;
      console.warn(`${token}'s balance of the forwarder unread:`, e);
    }
  }
  return holdings;
}

/** The float, concurrency accounts 0 to `count` - 1, the payee and its `forwarder`, and what
 * the payee and the forwarder hold of each of `tokens` (each's decimals and symbol), read in one
 * batch as of the latest block, `block`. The forwarder's tokens are read for the root alone: one
 * that doesn't answer is left out (shownHoldings). */
export async function survey(
  chain: Chain,
  floatAccount: Signer,
  count: number,
  block: number,
  payee: Address,
  forwarder: Address,
  tokens: ReadonlyMap<Address, Denomination>,
): Promise<AccountsStatus> {
  const addresses = Array.from({ length: count }, (_, i) => derive(floatAccount, i).address);
  const all = [floatAccount.address, ...addresses, payee];
  const read = await chain.batch([
    ...all.flatMap((a) => [
      { method: "eth_getBalance", params: [a, hexQuantity(block)] },
      { method: "eth_getTransactionCount", params: [a, hexQuantity(block)] },
    ]),
    ...[...tokens.keys()].map((t) => balanceOfCall(t, payee, hexQuantity(block))),
    { method: "eth_getBalance", params: [forwarder, hexQuantity(block)] },
    ...[...tokens.keys()].map((t) => balanceOfCall(t, forwarder, hexQuantity(block))),
  ], { atomic: true });
  const at = 2 * all.length;
  const forwarded = at + tokens.size;
  const answers = read.slice(0, forwarded + 1).map((a) => quantity(resultOf(a)));
  const value = (i: number): bigint => answers[i] as bigint;
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
    forwarderBalance: value(forwarded),
    forwarderTokens: shownHoldings(tokens, read.slice(forwarded + 1)),
  };
}

/** `status` as stored: its quantities as decimal strings. */
export function statusToJson(status: AccountsStatus): string {
  return JSON.stringify(status, (_, v) => (typeof v === "bigint" ? v.toString() : v));
}

/** A stored status; null for one stored before the forwarder was read with the accounts (0.6.0's),
 * which the next read replaces. */
export function statusFromJson(json: string): AccountsStatus | null {
  const s = JSON.parse(json);
  if (s.forwarderBalance === undefined) return null;
  const held = (h: { amount: string }) => ({ ...h, amount: BigInt(h.amount) });
  return {
    block: s.block,
    floatBalance: BigInt(s.floatBalance),
    floatNonce: s.floatNonce,
    accounts: s.accounts.map((a: { balance: string }) => ({ ...a, balance: BigInt(a.balance) })),
    payeeBalance: BigInt(s.payeeBalance),
    payeeNonce: s.payeeNonce,
    payeeTokens: s.payeeTokens.map(held),
    forwarderBalance: BigInt(s.forwarderBalance),
    forwarderTokens: s.forwarderTokens.map(held),
  };
}
