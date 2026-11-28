/**
 * A sponsorship's quote: what an account is to sign, filled in (each transaction's nonce, fees
 * and gas), the payment the relay offers in each token, and what it's charged
 * (docs/development/plans/sponsorship.md). Its calls are priced as a submission's are, and it's
 * planned by the one planner (plan.ts). One simulation a token, of the whole sequence with that
 * token's payment in it, fills in its transactions' gas; priced as a sponsorship of what's
 * quoted would be. As bruno/sponsoring.py's.
 */

import { concatBytes } from "@noble/hashes/utils.js";
import { addressWord, word } from "./abi.ts";
import { type Chain, type Latest, marketTip, risenBaseFee } from "./chain.ts";
import type { ChainConfig } from "./config.ts";
import { type Address, type EncodedCall, hex, hexQuantity, NATIVE_ASSET } from "./evm.ts";
import { NONZERO_BYTE_GAS, ZERO_BYTE_GAS } from "./forks.ts";
import { TRANSFER } from "./forwarder.ts";
import { lookup, type Measurements, scheduleOf } from "./measurements.ts";
import { accountState, callAccounts } from "./accountreads.ts";
import {
  CheckedByForwarder,
  type Draft,
  isOwn,
  planDrafts,
  planOf,
  signedJson,
  sponsored,
  transactionJson,
} from "./plan.ts";
import { ESTIMATE_HEADROOM, NO_PAYER, type Pricing } from "./pricing.ts";
import { gasUsed } from "./simulation.ts";
import { checkedSponsorship, owned } from "./sponsorship.ts";
import { authorities, type SubmittedCall } from "./submission.ts";
import type { Decoded, SignedAuthorization } from "./transactions.ts";

/** An amount's word in a payment's calldata, at its dearest over a unit's: every byte nonzero.
 * A payment's gas is estimated paying a unit, the amount not known until it's priced. */
const AMOUNT_WORD_GAS = 32 * (NONZERO_BYTE_GAS - ZERO_BYTE_GAS);
/** The largest amount: a quote's stand-in pays it, its charge the same whatever's paid. */
const MOST = (1n << 256n) - 1n;

/** A transaction the sponsored account is to sign, as a quote is asked about it: its gas
 * estimated if it's null; `to` null for a contract creation. */
export interface Unsigned {
  readonly to: Address | null;
  readonly data: Uint8Array;
  readonly value: bigint;
  readonly gas: number | null;
}

/** Whether a quote's transaction is signed already, not still to be. */
export function isSigned(transaction: Decoded | Unsigned | ToSign): transaction is Decoded {
  return "signed" in transaction;
}

/** What a sponsorship's quote is asked: the `account` sponsored; its authorizations, signed; the
 * calls the relay makes first, each with its gas, one's estimated if null; its own
 * transactions, each signed already or to be; and its deadline, the furthest one if null.
 * Checked as it's made (checkedAsked). */
export interface Asked {
  readonly account: Address;
  readonly authorizations: readonly SignedAuthorization[];
  readonly calls: readonly SubmittedCall[];
  readonly transactions: readonly (Decoded | Unsigned)[];
  readonly maxBlockNumber: number | null;
}

/** `asked`, as it's made: what's given is the account's (sponsorship.ts's owned).
 * InvalidSubmissionError otherwise. */
export function checkedAsked(asked: Asked): Asked {
  owned(
    asked.account,
    asked.calls.length,
    asked.authorizations,
    asked.transactions.map((t) => isSigned(t) ? t.signed.sender : null),
  );
  return asked;
}

/** A transaction filled in for its account to sign, EIP-1559's: `to` null for a contract
 * creation. */
export interface ToSign {
  readonly chainId: number;
  readonly nonce: number;
  readonly to: Address | null;
  readonly data: Uint8Array;
  readonly value: bigint;
  readonly gas: number;
  readonly maxFeePerGas: bigint;
  readonly maxPriorityFeePerGas: bigint;
}

/** What a sponsorship is charged in `token`, and the payment of it the relay offers, for the
 * account to sign after its own: null with no transaction of the account's, when it's its code,
 * run by the relay's call, that pays. */
export interface SponsoredTokenQuote {
  readonly token: Address;
  readonly amount: bigint;
  readonly payment: ToSign | null;
}

/** A sponsorship's quote, valid until `validUntilBlock`: its deadline, the forwarder it pays,
 * the gas each of its calls is priced for, in order, each of its transactions to sign (null for
 * one signed already), and its quote in each token. */
export interface SponsoredQuotes {
  readonly validUntilBlock: number;
  readonly maxBlockNumber: number;
  readonly forwarder: Address;
  readonly gas: readonly number[];
  readonly transactions: readonly (ToSign | null)[];
  readonly quotes: readonly SponsoredTokenQuote[];
}

/** `toSign` as a quote's stand-in for the transaction it will be once signed: priced and planned
 * by, never sent. */
function drafted(toSign: ToSign, sender: Address): Decoded {
  return {
    signed: {
      raw: new Uint8Array(0),
      hash: new Uint8Array(0),
      sender,
      nonce: toSign.nonce,
      gas: toSign.gas,
      maxFeePerGas: toSign.maxFeePerGas,
      value: toSign.value,
      beyondGas: 0n,
    },
    chainId: BigInt(toSign.chainId),
    maxPriorityFeePerGas: toSign.maxPriorityFeePerGas,
    to: toSign.to,
    data: toSign.data,
    accessList: [],
    authorizations: [],
  };
}

/** `toSign` as eth_simulateV1 takes a call, given `gas`: no nonce or fees, nothing validated. */
function callJson(sender: Address, toSign: ToSign, gas: number): Record<string, unknown> {
  return {
    from: sender,
    data: hex(toSign.data),
    value: hexQuantity(toSign.value),
    gas: hexQuantity(gas),
    ...(toSign.to === null ? {} : { to: toSign.to }),
  };
}

/** The payment a quote offers: `amount` of `token` transferred to the forwarder, or of ETH sent
 * it. */
export function paymentCall(forwarder: Address, token: Address, amount: bigint): EncodedCall {
  if (token === NATIVE_ASSET) return { to: forwarder, data: new Uint8Array(0), value: amount };
  return {
    to: token,
    data: concatBytes(TRANSFER, addressWord(forwarder), word(amount)),
    value: 0n,
  };
}

/** `gas` with ESTIMATE_HEADROOM, rounded up. */
function withHeadroom(gas: number): number {
  return Math.ceil((gas * ESTIMATE_HEADROOM.numerator) / ESTIMATE_HEADROOM.denominator);
}

/**
 * `asked`'s quote in each of `tokens`. Its calls are priced as a submission's are
 * (Pricing.priced): each one's accounts and bound read, one's gas estimated if not given. Each
 * transaction not signed yet is given the account's next nonce (after its authorizations, which
 * the relay's first transaction spends), the max fee a block may ask by the deadline, and the
 * market's tip. Then, a token at a time: with transactions of the account's, the payment in that
 * token is added after them, paying a unit (with none, the relay's calls pay, and no payment is
 * offered); the sequence is simulated once, the relay's own transactions as it's planned and
 * then the account's, for the gas each of the account's uses, a transaction's limit that and
 * ESTIMATE_HEADROOM where none was given (the payment's with what its amount's word may add);
 * and the whole is planned and charged as a sponsorship of it would be. A transaction that fails
 * is the quote's simulation failing.
 */
export async function sponsoredQuotes(
  chain: Chain,
  config: ChainConfig,
  pricing: Pricing,
  measured: Measurements,
  latest: Latest,
  asked: Asked,
  tokens: readonly Address[],
  sender: Address,
  forwarder: Address,
): Promise<SponsoredQuotes> {
  const account = asked.account;
  const schedule = scheduleOf(measured);
  const deadline = asked.maxBlockNumber ?? latest.number + config.maxDeadlineBlocks;
  const maxFee = risenBaseFee(latest.nextBaseFee, deadline - latest.number);
  const market = marketTip(latest.tips);
  const tip = market < maxFee ? market : maxFee;
  const state = await accountState(chain, account);
  let nonce = state.nonce + asked.authorizations.length;
  // each of its transactions as it is, or as it will be signed, its gas the cap until estimated
  const own = asked.transactions.map((transaction): Decoded | ToSign => {
    if (isSigned(transaction)) {
      nonce = transaction.signed.nonce + 1;
      return transaction;
    }
    return {
      chainId: chain.chainId,
      nonce: nonce++,
      to: transaction.to,
      data: transaction.data,
      value: transaction.value,
      gas: transaction.gas ?? config.maxGas,
      maxFeePerGas: maxFee,
      maxPriorityFeePerGas: tip,
    };
  });
  const estimated = asked.transactions.map((t) => !isSigned(t) && t.gas === null);
  const sending = asked.transactions.length > 0;
  // its calls as a submission's are priced, and their accounts with the account's own
  const quoted = {
    calls: asked.calls.map((each) => ({
      call: each.call,
      callDataLength: each.call.data.length,
      gas: each.gas,
    })),
    authorizations: asked.authorizations,
    authority: null,
  };
  const unpaid = await pricing.signing(measured, NO_PAYER, false);
  const priced = await pricing.priced(measured, quoted, tokens, unpaid, sender);
  const signers = authorities(asked.authorizations);
  const targets = [...priced.calls.map((planned) => planned.call.to), account];
  const accounts = await callAccounts(chain, targets, signers);
  const calls = priced.calls.map((planned) => ({ call: planned.call, gas: planned.gas }));
  const proof = new CheckedByForwarder();
  let filled: (ToSign | null)[] | null = null;
  const quotes: SponsoredTokenQuote[] = [];
  const paymentNonce = nonce;
  for (const token of tokens) {
    /** The payment of `amount` in `token`, to sign, given `gas`. */
    const paying = (amount: bigint, gas: number): ToSign => {
      const call = paymentCall(forwarder, token, amount);
      return {
        chainId: chain.chainId,
        nonce: paymentNonce,
        to: call.to,
        data: call.data,
        value: call.value,
        gas,
        maxFeePerGas: maxFee,
        maxPriorityFeePerGas: tip,
      };
    };
    /** The sponsorship of `transactions`, paying the most in `token`, as it's drafted. */
    const drafting = (transactions: readonly (Decoded | ToSign)[]): Draft<CheckedByForwarder> => {
      const standIn = checkedSponsorship({
        account,
        authorizations: asked.authorizations,
        calls,
        transactions: transactions.map((t) => isSigned(t) ? t : drafted(t, account)),
        token,
        payee: forwarder,
        amount: MOST,
        maxBlockNumber: deadline,
      });
      return sponsored(
        standIn,
        proof,
        signers,
        accounts,
        lookup(measured.tokens, token, "a token's measurement"),
        schedule,
        priced.calls,
        {
          accountNew: state.empty,
          accountHasCode: state.hasCode,
          payeeHolds: measured.payeeHolds.has(token),
          payeeNew: measured.payeeNew,
        },
      );
    };
    // the sequence as it will be sent, each transaction given all the gas it may have: the
    // relay's own first, then the account's, then the payment offered, paying a unit
    const offered = sending ? [...own, paying(1n, config.maxGas)] : own;
    const provisional = proof.plan(chain.chainId, drafting(offered), schedule);
    const first = provisional.steps.filter(isOwn).slice(0, -1);
    const sequence = [
      ...first.map((step) => transactionJson(step, sender, config.maxGas)),
      ...offered.map((t) => isSigned(t) ? signedJson(t) : callJson(account, t, t.gas)),
    ];
    const held = first.reduce((total, step) => total + step.transaction.value, 0n);
    const holding = new Map<Address, bigint>(held > 0n ? [[sender, held]] : []);
    const used = await gasUsed(chain, sequence, holding);
    const theirs = used.slice(first.length);
    const signing = own.map((t, i): Decoded | ToSign =>
      !isSigned(t) && estimated[i] ? { ...t, gas: withHeadroom(theirs[i] ?? 0) } : t
    );
    const wordGas = token === NATIVE_ASSET ? 0 : AMOUNT_WORD_GAS;
    const paymentGas = sending ? withHeadroom(theirs.at(-1) ?? 0) + wordGas : 0;
    const final = sending ? [...signing, paying(1n, paymentGas)] : signing;
    // planned as a sponsorship taken is: a transaction of the relay's given the gas left over
    // is estimated here
    const [planned] = await planDrafts(
      chain,
      [{ status: "fulfilled", value: drafting(final) }],
      schedule,
      sender,
    );
    const plan = planOf(planned);
    pricing.fits(plan);
    pricing.withinCost(plan, latest, deadline - latest.number, "transactions");
    const amount = await pricing.charge(measured, latest, token, plan);
    quotes.push({ token, amount, payment: sending ? paying(amount, paymentGas) : null });
    filled ??= signing.map((t) => isSigned(t) ? null : t);
  }
  return {
    validUntilBlock: latest.number + config.quoteBlocks,
    maxBlockNumber: deadline,
    forwarder,
    gas: priced.calls.map((planned) => planned.gas),
    transactions: filled ?? [],
    quotes,
  };
}
