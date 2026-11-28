/**
 * A sponsorship: transactions an account signed and can't pay the gas for, which the relay sends
 * after its own calls and the ETH the account needs, and before a transaction calling the
 * forwarder for the payment (docs/development/plans/sponsorship.md). What's taken and stored of
 * one, beside a Submission, and its id. It's planned as a submission is (plan.ts): its calls
 * classified alike, its payment known by the forwarder's check. As bruno/sponsorship.py's.
 */

import { concatBytes } from "@noble/hashes/utils.js";
import { addressWord, encodeBytes, word } from "./abi.ts";
import { type Address, keccak } from "./evm.ts";
import { refused } from "./refusals.ts";
import {
  authorities,
  callsEncoded,
  InvalidSubmissionError,
  type Submission,
  type SubmittedCall,
} from "./submission.ts";
import type { Decoded, SignedAuthorization } from "./transactions.ts";

/**
 * An `account`'s sponsorship: the `authorizations` it signed, which the relay's first transaction
 * carries; the `calls` the relay makes first, each with the gas it's given, as a submission's;
 * the account's own `transactions`, as signed, in order; and its payment, at least `amount` of
 * `token` to the forwarder at `payee`, by block `maxBlockNumber`. Checked as it's made
 * (checkedSponsorship).
 */
export interface Sponsorship {
  readonly account: Address;
  readonly authorizations: readonly SignedAuthorization[];
  readonly calls: readonly SubmittedCall[];
  readonly transactions: readonly Decoded[];
  readonly token: Address;
  readonly payee: Address;
  readonly amount: bigint;
  readonly maxBlockNumber: number;
  /** Never: a sponsorship's payment is the account's own transaction, or its code's. */
  readonly transfer?: never;
  /** Never: a sponsorship deploys nothing. */
  readonly deployment?: never;
}

/** Whether what was taken is a sponsorship, not a submission. */
export function isSponsorship(taken: Submission | Sponsorship): taken is Sponsorship {
  return "transactions" in taken;
}

/**
 * What's sponsored is `account`'s: there's a call (`calls` of them) or a transaction, and every
 * authorization and every signed transaction (`senders`, each one's, null for one not signed
 * yet) is its own. Its calls are the relay's to make, to anything, classified as a submission's
 * are. InvalidSubmissionError otherwise, naming the field.
 */
export function owned(
  account: Address,
  calls: number,
  authorizations: readonly SignedAuthorization[],
  senders: readonly (Address | null)[],
): void {
  if (calls === 0 && senders.length === 0) {
    throw new InvalidSubmissionError("transactions", refused("sponsored-nothing"));
  }
  const given: [string, Address | null][] = authorities(authorizations).map((
    authority,
    index,
  ) => [`authorizationList[${index}]`, authority]);
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
 * `s`, as a sponsorship's made: every authorization and transaction is the account's, the
 * transactions' nonces are consecutive, and there's a call or a transaction.
 * InvalidSubmissionError otherwise.
 */
export function checkedSponsorship(s: Sponsorship): Sponsorship {
  owned(s.account, s.calls.length, s.authorizations, s.transactions.map((t) => t.signed.sender));
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
 * The ETH the account's transactions need, which the relay sends it before them: each one's gas
 * at its max fee, and its value. What a transaction sends the forwarder isn't funded:
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
 * authorizations, (address,bytes,uint256,uint256)[] calls (as a submission's id encodes them),
 * bytes[] transactions, address token, address payee, uint256 amount, uint256 maxBlockNumber)):
 * the same sponsorship, the same id.
 */
export function sponsorshipId(s: Sponsorship): Uint8Array {
  const head: Uint8Array[] = [];
  const tail: Uint8Array[] = [];
  const headSize = 8 * 32;
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
  dynamic(callsEncoded(s.calls));
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
