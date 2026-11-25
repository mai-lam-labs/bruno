/**
 * A sponsorship: transactions an account signed and can't pay the gas for, which the relay sends
 * between a transaction of its own funding the account and one calling the forwarder for the
 * payment (docs/development/plans/sponsorship.md). What's taken and stored of one, beside a
 * Submission, and its id. As bruno/sponsorship.py's.
 */

import { concatBytes } from "@noble/hashes/utils.js";
import { addressWord, encodeBytes, word } from "./abi.ts";
import { type Address, type EncodedCall, keccak } from "./evm.ts";
import { refused } from "./refusals.ts";
import { authorities, InvalidSubmissionError, type Submission } from "./submission.ts";
import type { Decoded, SignedAuthorization } from "./transactions.ts";

/**
 * An `account`'s sponsorship: the `authorizations` it signed, which the relay's first transaction
 * carries; the `call` that transaction makes into the account's code, if any, given `gas`; the
 * account's own `transactions`, as signed, in order; and its payment, at least `amount` of
 * `token` to the forwarder at `payee`, by block `maxBlockNumber`. Checked as it's made
 * (checkedSponsorship).
 */
export interface Sponsorship {
  readonly account: Address;
  readonly authorizations: readonly SignedAuthorization[];
  readonly call: EncodedCall | null;
  readonly gas: number;
  readonly transactions: readonly Decoded[];
  readonly token: Address;
  readonly payee: Address;
  readonly amount: bigint;
  readonly maxBlockNumber: number;
}

/** Whether what was taken is a sponsorship, not a submission. */
export function isSponsorship(taken: Submission | Sponsorship): taken is Sponsorship {
  return "transactions" in taken;
}

/**
 * What's sponsored is `account`'s: there's a call or a transaction, the call is to it, and every
 * authorization and every signed transaction (`senders`, each one's, null for one not signed
 * yet) is its own. InvalidSubmissionError otherwise, naming the field.
 */
export function owned(
  account: Address,
  call: EncodedCall | null,
  authorizations: readonly SignedAuthorization[],
  senders: readonly (Address | null)[],
): void {
  if (call === null && senders.length === 0) {
    throw new InvalidSubmissionError("transactions", refused("sponsored-nothing"));
  }
  const given: [string, Address | null][] = [];
  if (call !== null) given.push(["call", call.to]);
  authorities(authorizations).forEach((authority, index) =>
    given.push([`authorizationList[${index}]`, authority])
  );
  senders.forEach((sender, index) => given.push([`transactions[${index}]`, sender]));
  for (const [field, its] of given) {
    if (its !== null && its !== account) {
      throw new InvalidSubmissionError(
        field,
        refused("sponsored-not-accounts", { given: its, account }),
      );
    }
  }
}

/**
 * `s`, as a sponsorship's made: every authorization and transaction is the account's, the call
 * is to it, the transactions' nonces are consecutive, and there's a call or a transaction.
 * InvalidSubmissionError otherwise.
 */
export function checkedSponsorship(s: Sponsorship): Sponsorship {
  owned(s.account, s.call, s.authorizations, s.transactions.map((t) => t.signed.sender));
  let following: number | null = null;
  s.transactions.forEach((transaction, index) => {
    const nonce = transaction.signed.nonce;
    if (following !== null && nonce !== following) {
      throw new InvalidSubmissionError(
        `transactions[${index}]`,
        refused("sponsored-nonce", { nonce, following }),
      );
    }
    following = nonce + 1;
  });
  return s;
}

/**
 * The ETH the account's transactions need, which the relay's first transaction sends it: each
 * one's gas at its max fee, and its value. What a transaction sends the forwarder isn't funded:
 * the payment would be in its own price, which no amount covers. The account may pay out of
 * anything else it's sent: the check asks for more than was sent, whatever it's paid from.
 */
export function funding(s: Sponsorship): bigint {
  return s.transactions.reduce(
    (total, t) =>
      total + BigInt(t.signed.gas) * t.signed.maxFeePerGas +
      (t.to === s.payee ? 0n : t.signed.value),
    0n,
  );
}

/**
 * keccak256(abi.encode(address account, (uint256,address,uint256,uint8,uint256,uint256)[]
 * authorizations, bytes callData, uint256 gas, bytes[] transactions, address token, address
 * payee, uint256 amount, uint256 maxBlockNumber)): the same sponsorship, the same id.
 */
export function sponsorshipId(s: Sponsorship): Uint8Array {
  const head: Uint8Array[] = [];
  const tail: Uint8Array[] = [];
  const headSize = 9 * 32;
  let tailSize = 0;
  const dynamic = (encoded: Uint8Array): void => {
    head.push(word(BigInt(headSize + tailSize)));
    tail.push(encoded);
    tailSize += encoded.length;
  };
  head.push(addressWord(s.account));
  dynamic(
    concatBytes(
      word(BigInt(s.authorizations.length)),
      ...s.authorizations.flatMap((a) => [
        word(a.chainId),
        addressWord(a.address),
        word(a.nonce),
        word(BigInt(a.yParity)),
        word(a.r),
        word(a.s),
      ]),
    ),
  );
  dynamic(encodeBytes(s.call === null ? new Uint8Array(0) : s.call.data));
  head.push(word(BigInt(s.gas)));
  // bytes[]: its length, each element's offset from the offsets' start, then the elements
  const raws = s.transactions.map((t) => encodeBytes(t.signed.raw));
  let offset = raws.length * 32;
  const offsets = raws.map((raw) => {
    const at = word(BigInt(offset));
    offset += raw.length;
    return at;
  });
  dynamic(concatBytes(word(BigInt(raws.length)), ...offsets, ...raws));
  head.push(
    addressWord(s.token),
    addressWord(s.payee),
    word(s.amount),
    word(BigInt(s.maxBlockNumber)),
  );
  return keccak(concatBytes(...head, ...tail));
}
