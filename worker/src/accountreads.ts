/**
 * What's read of the accounts a plan depends on, each in one batch at the highest block seen: one
 * account's state (AccountState), and a call's accounts, its target and its EIP-7702 authorities
 * (CallAccounts). The reads themselves (accountReads, authorityReads) and what their answers say
 * (accountsOf, emptyAmong) are a plan's to batch with its own.
 */

import { type Chain, ChainError, codeOf, hasCode, quantity } from "./chain.ts";
import { type Address, hex } from "./evm.ts";
import { type Answer, type Call, resultOf } from "./jsonrpc.ts";

/** What an account delegated by EIP-7702 has as its code, before its delegate's address. */
export const DELEGATION_PREFIX = "0xef0100";

/** An account as read: whether it's empty (no balance, no nonce, no code), its nonce, and
 * whether it has code, a delegation's included: what runs when it's sent ETH. */
export interface AccountState {
  readonly empty: boolean;
  readonly nonce: number;
  readonly hasCode: boolean;
}

/** `account`'s state, read in one batch at the highest block seen. */
export async function accountState(chain: Chain, account: Address): Promise<AccountState> {
  const reads = await chain.batch(authorityReads(account, chain.seenTag));
  const [, nonce, code] = reads;
  if (nonce === undefined || code === undefined) throw new ChainError("no account read");
  return {
    empty: emptyAmong(reads) === 1,
    nonce: Number(quantity(resultOf(nonce))),
    hasCode: hasCode(resultOf(code)),
  };
}

/** How many of `authorities` are empty (no balance, no nonce, no code), read in one batch at the
 * highest block seen: nothing read if there are none. */
export async function countEmpty(
  chain: Chain,
  authorities: readonly Address[],
): Promise<number> {
  const at = chain.seenTag;
  const reads = authorities.flatMap((authority) => authorityReads(authority, at));
  return emptyAmong(await chain.batch(reads));
}

/** What's read of a call's accounts: those EIP-7702 delegated in its transaction (its
 * authorities, and each target delegated already), how many of its authorities are empty, and
 * which of them are delegated already (`delegating`). */
export interface CallAccounts {
  readonly delegated: ReadonlySet<Address>;
  readonly empty: number;
  readonly delegating: ReadonlySet<Address>;
}

/** Each of `targets`' code, and each authority's balance, nonce and code, read in one batch at
 * the highest block seen. */
export async function callAccounts(
  chain: Chain,
  targets: readonly Address[],
  authorities: readonly Address[],
): Promise<CallAccounts> {
  const reads = accountReads(targets, authorities, chain.seenTag);
  return accountsOf(targets, authorities, await chain.batch(reads));
}

/** What's read of a submission's calls' accounts at `block` (CallAccounts): each target's code,
 * in the calls' order, then each authority's balance, nonce and code. */
export function accountReads(
  targets: readonly Address[],
  authorities: readonly Address[],
  block: string,
): Call[] {
  return [
    ...targets.map((target): Call => ({ method: "eth_getCode", params: [target, block] })),
    ...authorities.flatMap((authority) => authorityReads(authority, block)),
  ];
}

/** A submission's calls' accounts from their reads' `answers` (accountReads). */
export function accountsOf(
  targets: readonly Address[],
  authorities: readonly Address[],
  answers: readonly Answer[],
): CallAccounts {
  const delegatedCode = (i: number): boolean => {
    const answer = answers[i];
    if (answer === undefined) throw new ChainError("no code read");
    return hex(codeOf(resultOf(answer))).startsWith(DELEGATION_PREFIX);
  };
  const delegated = new Set(authorities);
  targets.forEach((target, i) => {
    if (delegatedCode(i)) delegated.add(target);
  });
  const theirs = targets.length;
  const delegating = new Set(authorities.filter((_, i) => delegatedCode(theirs + 2 + 3 * i)));
  return { delegated, empty: emptyAmong(answers.slice(theirs)), delegating };
}

/** An authority's reads at `block`, to tell whether it's empty: its balance, nonce and code. */
export function authorityReads(account: Address, block: string): Call[] {
  return [
    { method: "eth_getBalance", params: [account, block] },
    { method: "eth_getTransactionCount", params: [account, block] },
    { method: "eth_getCode", params: [account, block] },
  ];
}

/** How many of the authorities whose reads `answers` are, three each, are empty. */
export function emptyAmong(answers: readonly Answer[]): number {
  if (answers.length % 3 !== 0) throw new ChainError("no authority read");
  let empty = 0;
  for (let i = 0; i < answers.length; i += 3) {
    const [balance, nonce, code] = answers.slice(i, i + 3).map(resultOf);
    if (quantity(balance) === 0n && quantity(nonce) === 0n && !hasCode(code)) {
      empty++;
    }
  }
  return empty;
}
