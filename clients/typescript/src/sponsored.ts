/**
 * A sponsorship (SPEC.md): transactions an account signs and can't pay the gas for, which the
 * relay sends between two of its own, funding the account and collecting its payment. What a
 * client asks about, what the relay answers for the account to sign, and the check that it's
 * what was asked, before a wallet signs it.
 */

import { BrunoError } from "./relay.ts";
import type { Value } from "./tokens.ts";
import type { Hex } from "./types.ts";

/** ERC-7528's address for ETH, where a token's is expected. */
const NATIVE_ASSET = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee";
/** ERC-20's transfer(address,uint256). */
const TRANSFER = "0xa9059cbb";

/** A call the account makes as a transaction of its own: its gas the relay's estimate, unless
 * given; no `to` for a contract creation. */
export interface SponsoredCall {
  readonly to?: Hex;
  readonly data?: Hex;
  readonly value?: bigint;
  readonly gas?: bigint;
}

/** A transaction filled in for its account to sign, EIP-1559's: no `to` for a contract
 * creation. */
export interface TransactionToSign {
  readonly chainId: number;
  readonly nonce: number;
  readonly to?: Hex;
  readonly data: Hex;
  readonly value: bigint;
  readonly gas: bigint;
  readonly maxFeePerGas: bigint;
  readonly maxPriorityFeePerGas: bigint;
}

/** A wallet that signs a transaction without sending it: its raw bytes. */
export interface TransactionSigner {
  readonly address: Hex;
  signTransaction(transaction: TransactionToSign): Promise<Hex>;
}

/**
 * A sponsorship's quote, valid until `validUntilBlock`: the block it's to land by, the forwarder
 * it pays, each of the account's transactions filled in to sign, and, for each token asked, in
 * their order, what it's charged and the payment of it to sign after them. Check it's what was
 * asked before signing it (checkedSponsored).
 */
export interface SponsoredQuote {
  readonly validUntilBlock: bigint;
  readonly maxBlockNumber: bigint;
  readonly forwarder: Hex;
  readonly transactions: readonly TransactionToSign[];
  readonly prices: readonly Value[];
  readonly payments: readonly TransactionToSign[];
}

/** A transaction to sign, as the relay's JSON has it. */
export interface ToSignJson {
  readonly chainId: string;
  readonly nonce: string;
  readonly to?: Hex;
  readonly data: Hex;
  readonly value: string;
  readonly gas: string;
  readonly maxFeePerGas: string;
  readonly maxPriorityFeePerGas: string;
}

export function toSignOf(t: ToSignJson): TransactionToSign {
  return {
    chainId: Number(t.chainId),
    nonce: Number(t.nonce),
    ...(t.to === undefined ? {} : { to: t.to }),
    data: t.data,
    value: BigInt(t.value),
    gas: BigInt(t.gas),
    maxFeePerGas: BigInt(t.maxFeePerGas),
    maxPriorityFeePerGas: BigInt(t.maxPriorityFeePerGas),
  };
}

function same(a: string | undefined, b: string | undefined): boolean {
  return (a ?? "").toLowerCase() === (b ?? "").toLowerCase();
}

/** `value` as one 32-byte word of hex, without its 0x. */
function word(value: bigint | string): string {
  const digits = typeof value === "bigint" ? value.toString(16) : value.replace(/^0x/, "");
  return digits.toLowerCase().padStart(64, "0");
}

/**
 * `quote`'s transactions and its payment in the token at `index`, checked to be what `calls`
 * asked for and no more: each transaction one of the calls as it was given, in their order, on
 * `chainId`, at consecutive nonces; and the payment after them a transfer of the price to the
 * quote's forwarder (ETH: sent it), and nothing else. BrunoError otherwise: a wallet signs only
 * what's returned.
 */
export function checkedSponsored(
  quote: SponsoredQuote,
  calls: readonly SponsoredCall[],
  chainId: number,
  index: number,
): TransactionToSign[] {
  const refuse = (why: string): never => {
    throw new BrunoError(`the quote isn't what was asked: ${why}`, 0, quote);
  };
  const price = quote.prices[index];
  const payment = quote.payments[index];
  if (price === undefined || payment === undefined) return refuse("no payment in that token");
  if (quote.transactions.length !== calls.length) refuse("not a transaction for each call");
  const signing = [...quote.transactions, payment];
  signing.forEach((t, i) => {
    if (t.chainId !== chainId) refuse(`transaction ${i} is for chain ${t.chainId}`);
    const first = signing[0];
    if (first !== undefined && t.nonce !== first.nonce + i) refuse(`transaction ${i}'s nonce`);
  });
  quote.transactions.forEach((t, i) => {
    const call = calls[i];
    if (call === undefined) return;
    if (
      !same(t.to, call.to) || !same(t.data, call.data ?? "0x") || t.value !== (call.value ?? 0n)
    ) {
      refuse(`transaction ${i} isn't the call given`);
    }
    if (call.gas !== undefined && t.gas !== call.gas) refuse(`transaction ${i}'s gas`);
  });
  const token = price.token.address;
  if (same(token, NATIVE_ASSET)) {
    if (!same(payment.to, quote.forwarder) || payment.data !== "0x") refuse("the payment's target");
    if (payment.value !== price.baseUnits) refuse("the payment's amount");
  } else {
    const transfer = `${TRANSFER}${word(quote.forwarder)}${word(price.baseUnits)}`;
    if (!same(payment.to, token) || payment.value !== 0n) refuse("the payment's target");
    if (!same(payment.data, transfer)) refuse("the payment isn't a transfer of the price");
  }
  return signing;
}
