/**
 * A sponsorship's quote: what an account is to sign, filled in (each transaction's nonce, fees
 * and gas), the payment the relay offers in each token, and what it's charged
 * (docs/development/plans/sponsorship.md). One simulation a token, of the whole sequence with
 * that token's payment in it; priced as a submission of what's quoted would be. As
 * bruno/sponsoring.py's.
 */

import { concatBytes } from "@noble/hashes/utils.js";
import { addressWord, word } from "./abi.ts";
import { type Chain, type Latest, marketTip, risenBaseFee } from "./chain.ts";
import type { ChainConfig } from "./config.ts";
import { type Address, type EncodedCall, hex, hexQuantity, NATIVE_ASSET } from "./evm.ts";
import { NONZERO_BYTE_GAS, ZERO_BYTE_GAS } from "./forks.ts";
import { TRANSFER } from "./forwarder.ts";
import { lookup, type Measurements, scheduleOf } from "./measurements.ts";
import { accountState, fundingOverhead, sandwich, signedJson } from "./plan.ts";
import { ESTIMATE_HEADROOM, type Pricing } from "./pricing.ts";
import { gasUsed } from "./simulation.ts";
import { checkedSponsorship, owned } from "./sponsorship.ts";
import { authorizationJson, type Decoded, type SignedAuthorization } from "./transactions.ts";

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
 * call the relay's first transaction makes into it, and its gas, estimated if null; its own
 * transactions, each signed already or to be; and its deadline, the furthest one if null.
 * Checked as it's made (checkedAsked). */
export interface Asked {
  readonly account: Address;
  readonly authorizations: readonly SignedAuthorization[];
  readonly call: EncodedCall | null;
  readonly gas: number | null;
  readonly transactions: readonly (Decoded | Unsigned)[];
  readonly maxBlockNumber: number | null;
}

/** `asked`, as it's made: what's given is the account's (sponsorship.ts's owned).
 * InvalidSubmissionError otherwise. */
export function checkedAsked(asked: Asked): Asked {
  owned(
    asked.account,
    asked.call,
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
 * the gas the relay's first transaction is given for the account's code (null if it runs none:
 * no call, no authorization, and no code there already), each of its transactions to sign (null
 * for one signed already), and its quote in each token. */
export interface SponsoredQuotes {
  readonly validUntilBlock: number;
  readonly maxBlockNumber: number;
  readonly forwarder: Address;
  readonly gas: number | null;
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
 * `asked`'s quote in each of `tokens`. Each transaction not signed yet is given the account's
 * next nonce (after its authorizations, which the relay's first transaction spends), the max fee
 * a block may ask by the deadline, and the market's tip. Then, a token at a time: with
 * transactions of the account's, the payment in that token is added after them, paying a unit
 * (with none, the account's code pays, in the relay's call, and no payment is offered); the
 * sequence is simulated once, the relay's first transaction then the account's, for the gas each
 * uses (the first's what the account's code takes, if it runs any: called, delegated by it, or
 * there already), a transaction's limit that and ESTIMATE_HEADROOM where none was given (the payment's
 * with what its amount's word may add); and the whole is planned and charged as a sponsorship of
 * it would be. A transaction that fails is the quote's simulation failing.
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
  const data = asked.call === null ? new Uint8Array(0) : asked.call.data;
  // the relay's first transaction runs the account's code: called, delegated by it, or there
  // already, receiving the ETH it's sent
  const runsCode = asked.call !== null || asked.authorizations.length > 0 || state.hasCode;
  const sending = asked.transactions.length > 0;
  const values = own.reduce((total, t) => total + (isSigned(t) ? t.signed.value : t.value), 0n);
  const first: Record<string, unknown> = {
    from: sender,
    to: account,
    data: hex(data),
    value: hexQuantity(sending ? values + 1n : 0n),
    gas: hexQuantity(config.maxGas),
    ...(asked.authorizations.length === 0
      ? {}
      : { authorizationList: asked.authorizations.map(authorizationJson) }),
  };
  const overhead = fundingOverhead(
    schedule,
    data,
    asked.authorizations.length,
    sending,
    state.empty,
  );
  let callGas = asked.gas;
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
    const calls = [
      first,
      ...own.map((t) => isSigned(t) ? signedJson(t) : callJson(account, t, t.gas)),
    ];
    // the payment offered, paying a unit, after the account's own
    if (sending) calls.push(callJson(account, paying(1n, config.maxGas), config.maxGas));
    const holding = new Map<Address, bigint>(sending ? [[sender, values + 1n]] : []);
    const used = await gasUsed(chain, calls, holding);
    const firstUsed = used[0] ?? 0;
    if (callGas === null) {
      callGas = runsCode ? withHeadroom(Math.max(0, firstUsed - overhead)) : 0;
    }
    const signing = own.map((t, i): Decoded | ToSign =>
      !isSigned(t) && estimated[i] ? { ...t, gas: withHeadroom(used[1 + i] ?? 0) } : t
    );
    const wordGas = token === NATIVE_ASSET ? 0 : AMOUNT_WORD_GAS;
    const paymentGas = sending ? withHeadroom(used.at(-1) ?? 0) + wordGas : 0;
    const drafts = signing.map((t) => isSigned(t) ? t : drafted(t, account));
    if (sending) drafts.push(drafted(paying(1n, paymentGas), account));
    const standIn = checkedSponsorship({
      account,
      authorizations: asked.authorizations,
      call: asked.call,
      gas: callGas,
      transactions: drafts,
      token,
      payee: forwarder,
      amount: MOST,
      maxBlockNumber: deadline,
    });
    const plan = sandwich(
      chain.chainId,
      standIn,
      schedule,
      lookup(measured.tokens, token, "a token's measurement"),
      {
        accountEmpty: state.empty,
        payeeHolds: measured.payeeHolds.has(token),
        payeeNew: measured.payeeNew,
      },
    );
    pricing.fits(plan);
    pricing.withinCost(plan, latest, deadline - latest.number);
    const amount = await pricing.charge(measured, latest, token, plan);
    quotes.push({ token, amount, payment: sending ? paying(amount, paymentGas) : null });
    filled ??= signing.map((t) => isSigned(t) ? null : t);
  }
  return {
    validUntilBlock: latest.number + config.quoteBlocks,
    maxBlockNumber: deadline,
    forwarder,
    gas: runsCode ? callGas : null,
    transactions: filled ?? [],
    quotes,
  };
}
