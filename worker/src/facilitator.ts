/**
 * The hosted x402 facilitator: a merchant's URL, `/x402/facilitator/<payout>/`, with x402 v2's
 * `/supported`, `/verify` and `/settle` under it. What a URL's path says: the payout address,
 * the merchant's terms, each a `name=value` segment, and the endpoint asked for. As
 * bruno/server/facilitator.py's.
 */

import { secp256k1 } from "@noble/curves/secp256k1.js";
import { concatBytes } from "@noble/hashes/utils.js";
import { derive } from "./accounts.ts";
import { ChainError, hasCode, wordOf } from "./chain.ts";
import {
  digest,
  ECDSA_BYTES,
  quotedCalldata,
  type TransferAuthorization,
  transferCalldata,
  transferDeployment,
  transferDigest,
} from "./eip3009.ts";
import { WrapperError } from "./erc6492.ts";
import { type Address, checksum, type Hex, hex } from "./evm.ts";
import { resultOf } from "./jsonrpc.ts";
import { type Refused, refused } from "./refusals.ts";
import { NoCapacityError, type Relay, UnderpaidError } from "./relay.ts";
import { SimulationFailedError } from "./reverts.ts";
import { InvalidSubmissionError, type Submission } from "./submission.ts";
import { boundable } from "./targets.ts";
import { balanceOfCall } from "./tokens.ts";
import { transactionHash } from "./tracked.ts";
import type { FacilitatorRequest, SettleResponse } from "./wire.ts";
import {
  ASSET_TRANSFER_METHOD,
  EXPIRED,
  INVALID_NETWORK,
  INVALID_PAYLOAD,
  INVALID_REQUIREMENTS,
  INVALID_SCHEME,
  INVALID_SIGNATURE,
  INVALID_VERSION,
  network,
  NOT_YET_VALID,
  PAYMENT_FLOW,
  paymentTransfer,
  RECIPIENT_MISMATCH,
  SCHEME,
  transferRefusal,
  VALUE_MISMATCH,
  X402_VERSION,
} from "./x402.ts";

/** The endpoints under a facilitator URL; none is the payout address's own page. */
export const ENDPOINTS = ["supported", "verify", "settle"];
const ADDRESS = /^0x[0-9a-fA-F]{40}$/;
const AMOUNT = /^[0-9]{1,78}$/;

/** A facilitator URL that isn't one: which part (`field`: `payout`, a term's name, or the
 * segment itself), and why. */
export class InvalidRouteError extends Error {
  readonly refused: Refused;

  constructor(readonly field: string, why: string) {
    const refusal = refused("malformed", { why });
    super(`${field}: ${refusal}`);
    this.refused = refusal;
  }
}

/**
 * A merchant's limits on what Bruno does for it, read from its facilitator URL: the `tokens`
 * taken (null: every token the relay takes by EIP-3009), and the most the fee may be in a
 * token, in its base units (`maxFee`: a token without one settles at whatever the fee is).
 */
export interface Terms {
  readonly tokens: ReadonlySet<Address> | null;
  readonly maxFee: ReadonlyMap<Address, bigint>;
}

/** Whether a payment in `token` is within `terms`. */
export function takes(terms: Terms, token: Address): boolean {
  return terms.tokens === null || terms.tokens.has(token);
}

/** Whether a `fee` in `token` is within `terms`: at most its cap, if it has one. */
export function allows(terms: Terms, token: Address, fee: bigint): boolean {
  const most = terms.maxFee.get(token);
  return most === undefined || fee <= most;
}

/** What a facilitator URL's path names, after its prefix: the `payout` address, the merchant's
 * `terms`, and the `endpoint` asked for (one of ENDPOINTS; empty for the payout address's own
 * page). */
export interface Route {
  readonly payout: Address;
  readonly terms: Terms;
  readonly endpoint: string;
}

/**
 * The route a facilitator URL's path `segments` name, each percent-decoded already: the payout
 * address, then the terms, each `name=value` and each at most once, then the endpoint, if any.
 * `tokens=<a>,<b>` and `max-fee=<a>:<n>,<b>:<m>`, an address in any case and an amount in
 * decimal. InvalidRouteError for anything else: a term unknown or given twice, a token twice in
 * one, a cap for a token the terms don't take.
 */
export function routeOf(segments: readonly string[]): Route {
  const [first, ...rest] = segments;
  if (first === undefined) throw new InvalidRouteError("payout", "missing: the payout address");
  const payout = addressOf(first, "payout");
  let endpoint = "";
  const last = rest.at(-1);
  if (last !== undefined && !last.includes("=")) {
    rest.pop();
    endpoint = last;
    if (!ENDPOINTS.includes(endpoint)) throw new InvalidRouteError(endpoint, "an unknown endpoint");
  }
  const given = new Map<string, string>();
  for (const segment of rest) {
    const at = segment.indexOf("=");
    if (at === -1) throw new InvalidRouteError(segment, "not a term: name=value");
    const name = segment.slice(0, at);
    if (given.has(name)) throw new InvalidRouteError(name, "given twice");
    given.set(name, segment.slice(at + 1));
  }
  let tokens: Set<Address> | null = null;
  const maxFee = new Map<Address, bigint>();
  for (const [name, value] of given) {
    if (name === "tokens") {
      const taken = value.split(",").map((each) => addressOf(each, name));
      tokens = new Set(taken);
      if (tokens.size !== taken.length) throw new InvalidRouteError(name, "a token twice");
    } else if (name === "max-fee") {
      for (const each of value.split(",")) {
        const at = each.indexOf(":");
        const capped = addressOf(at === -1 ? each : each.slice(0, at), name);
        const amount = at === -1 ? "" : each.slice(at + 1);
        if (maxFee.has(capped)) throw new InvalidRouteError(name, "a token twice");
        if (!AMOUNT.test(amount) || BigInt(amount) >= 1n << 256n) {
          throw new InvalidRouteError(name, "an amount in the token's base units, decimal");
        }
        maxFee.set(capped, BigInt(amount));
      }
    } else {
      throw new InvalidRouteError(name, "an unknown term");
    }
  }
  if (tokens !== null) {
    for (const capped of maxFee.keys()) {
      if (!tokens.has(capped)) {
        throw new InvalidRouteError("max-fee", "a token the terms don't take");
      }
    }
  }
  return { payout, terms: { tokens, maxFee }, endpoint };
}

/** The address `text` is, in any case: InvalidRouteError naming `field` if it isn't one. */
function addressOf(text: string, field: string): Address {
  if (!ADDRESS.test(text)) throw new InvalidRouteError(field, "an address");
  return checksum(text);
}

/** x402's extension this facilitator answers the merchant by (EXTENSION-RESPONSES): the fee. */
export const EXTENSION = "bruno-fee";
/** How far ahead a settlement's deadline is, at most: /settle is held until it's included or
 * this passes, under the 90 seconds x402's reference client gives a facilitator's answer. */
export const WAIT_BLOCKS = 5;
/** How many slots past the deadline's time the receipt wallet's own authorizations stay valid:
 * slots missed before the deadline's block make its time later than expected. */
const LATE_SLOTS = 2;
/** Why a payment isn't settled, where x402's own codes say nothing: the token isn't taken (by
 * the relay, by EIP-3009 at a gas it bounds, or by the merchant's terms); the fee is over the
 * terms' cap; the payment doesn't exceed the fee; it wasn't included by its deadline, the
 * payment unused, to be tried again. */
export const ASSET_NOT_ACCEPTED = "bruno_asset_not_accepted";
export const FEE_OVER_MAX = "bruno_fee_over_max";
export const PAYMENT_UNDER_FEE = "bruno_payment_under_fee";
export const NOT_INCLUDED = "bruno_not_included";
/** The reference facilitator's code for a payer that's a smart wallet not deployed yet. */
export const UNDEPLOYED_SMART_WALLET = "invalid_exact_evm_payload_undeployed_smart_wallet";
const MAX_UINT256 = (1n << 256n) - 1n;

/** A payment the facilitator won't settle: x402's code for why, or one of Bruno's own. */
export class RefusedError extends Error {
  constructor(readonly reason: string) {
    super(reason);
  }
}

/**
 * A settled payment in parts, in `token`'s base units: what the buyer `paid`, the `fee` Bruno
 * keeps, what the receipt wallet keeps (`kept`: one unit, the first time it holds the token, so
 * its balance's slot stays set), and the merchant's payout, the rest (payoutOf).
 */
export interface Split {
  readonly token: Address;
  readonly paid: bigint;
  readonly fee: bigint;
  readonly kept: bigint;
}

/** The merchant's part of `split`. */
export function payoutOf(split: Split): bigint {
  return split.paid - split.fee - split.kept;
}

/**
 * A payment as Bruno settles it: the `submission` that does (the buyer's transfer to the receipt
 * wallet, then the wallet's to the merchant, and as its payment the wallet's of the fee to
 * Bruno), the buyer's `transfer` and its `digest`, and the payment's `split`.
 */
export interface Settlement {
  readonly submission: Submission;
  readonly transfer: TransferAuthorization;
  readonly digest: Hex;
  readonly split: Split;
}

/** What the facilitator settles on `relay`'s chain, if it takes x402: `exact`, its extension,
 * and the chain's sending accounts. The Worker joins its chains' answers. */
export function supported(relay: Relay): Record<string, unknown> {
  const named = network(relay.chain.chainId);
  if (relay.domains.size === 0) return { kinds: [], extensions: [EXTENSION], signers: {} };
  const accounts = Array.from(
    { length: relay.config.accounts },
    (_, i) => derive(relay.floatAccount, i).address,
  );
  return {
    kinds: [{ x402Version: X402_VERSION, scheme: SCHEME, network: named }],
    extensions: [EXTENSION],
    signers: { [named]: accounts },
  };
}

/** Where `route`'s payout address is paid on `relay`'s chain, if it takes x402. */
export function info(relay: Relay, route: Route): Record<string, unknown> {
  const payTo = relay.domains.size === 0
    ? {}
    : { [network(relay.chain.chainId)]: relay.receiptAccount(route.payout).address };
  return { payout: route.payout, payTo };
}

/** The headers that tell the merchant of `split`, if there's one: EXTENSION-RESPONSES, base64
 * of a JSON object keyed by the extension's name. */
export function splitHeaders(split: Split | null): Record<string, string> {
  if (split === null) return {};
  const told = {
    asset: split.token,
    amount: split.paid.toString(),
    fee: split.fee.toString(),
    kept: split.kept.toString(),
    payout: payoutOf(split).toString(),
  };
  return { "EXTENSION-RESPONSES": btoa(JSON.stringify({ [EXTENSION]: told })) };
}

/**
 * `request`'s payment as Bruno would settle it for `route`'s merchant now, checked against what
 * was required of it and against the terms, its fee the relay's quote for the three transfers:
 * RefusedError why not. Its signature and the buyer's balance aren't checked here: the
 * settlement's simulation finds those.
 */
export async function settlement(
  relay: Relay,
  route: Route,
  request: FacilitatorRequest,
): Promise<Settlement> {
  const payload = request.paymentPayload;
  const required = request.paymentRequirements;
  const accepted = payload.accepted;
  if (request.x402Version !== X402_VERSION || payload.x402Version !== X402_VERSION) {
    throw new RefusedError(INVALID_VERSION);
  }
  if (required.scheme !== SCHEME || accepted.scheme !== SCHEME) {
    throw new RefusedError(INVALID_SCHEME);
  }
  if (accepted.network !== required.network) throw new RefusedError(INVALID_NETWORK);
  const token = required.asset;
  const chainId = relay.chain.chainId;
  const domain = relay.domains.get(token);
  // taken by the relay over x402, at a gas it bounds, and by the merchant's terms
  const keySigned = { to: token, data: quotedCalldata(null), value: 0n };
  if (
    domain === undefined || accepted.asset !== token || !boundable(chainId, keySigned) ||
    !takes(route.terms, token)
  ) {
    throw new RefusedError(ASSET_NOT_ACCEPTED);
  }
  const method = required.extra?.assetTransferMethod;
  const flow = required.extra?.paymentFlow;
  if (
    (method !== undefined && method !== ASSET_TRANSFER_METHOD) ||
    (flow !== undefined && flow !== PAYMENT_FLOW)
  ) {
    throw new RefusedError(INVALID_REQUIREMENTS);
  }
  const receipt = relay.receiptAccount(route.payout);
  const authorization = payload.payload.authorization;
  if (authorization.to !== receipt.address || required.payTo !== receipt.address) {
    throw new RefusedError(RECIPIENT_MISMATCH);
  }
  // exact: the transfer is of the amount required, no more and no less
  if (!/^[0-9]+$/.test(required.amount) || BigInt(required.amount) !== authorization.value) {
    throw new RefusedError(VALUE_MISMATCH);
  }
  if (
    [authorization.value, authorization.validAfter, authorization.validBefore].some((each) =>
      each > MAX_UINT256
    )
  ) {
    throw new RefusedError(INVALID_PAYLOAD);
  }
  const transfer = paymentTransfer(payload);
  let deadline: number;
  try {
    deadline = await relay.x402Deadline(transfer.validAfter, transfer.validBefore, WAIT_BLOCKS);
  } catch (e) {
    if (!(e instanceof InvalidSubmissionError)) throw e;
    throw new RefusedError(e.field === "validAfter" ? NOT_YET_VALID : EXPIRED);
  }
  const at = relay.chain.seenTag;
  const [code, held] = await relay.chain.batch([
    { method: "eth_getCode", params: [transfer.payer, at] },
    balanceOfCall(token, receipt.address, at),
  ], { atomic: true });
  if (code === undefined || held === undefined) throw new ChainError("no receipt read");
  let wrapped: boolean;
  try {
    wrapped = transferDeployment(transfer) !== null;
  } catch (e) {
    if (!(e instanceof WrapperError)) throw e;
    throw new RefusedError(INVALID_SIGNATURE);
  }
  if (wrapped && !hasCode(resultOf(code))) throw new RefusedError(UNDEPLOYED_SMART_WALLET);
  const kept = wordOf(resultOf(held)) === 0n ? 1n : 0n;
  // priced with the wallet's own transfers at their dearest, no byte of theirs zero, so the fee
  // covers them whatever they come to hold
  const paying = { to: token, data: transferCalldata(transfer), value: 0n };
  const payingOut = {
    to: token,
    data: transferCalldata({
      payer: receipt.address,
      to: route.payout,
      value: MAX_UINT256,
      validAfter: MAX_UINT256,
      validBefore: MAX_UINT256,
      nonce: new Uint8Array(32).fill(0xff),
      signature: new Uint8Array(ECDSA_BYTES).fill(0xff),
    }),
    value: 0n,
  };
  const quoted = {
    calls: [paying, payingOut].map((call) => ({
      call,
      callDataLength: call.data.length,
      gas: null,
    })),
    authorizations: [],
    authority: null,
  };
  const quotes = await relay.quote(quoted, [token], { address: receipt.address });
  const [quote] = quotes.quotes;
  if (quote === undefined) throw new ChainError("no quote");
  const split = { token, paid: transfer.value, fee: quote.amount, kept };
  if (payoutOf(split) <= 0n) throw new RefusedError(PAYMENT_UNDER_FEE);
  if (!allows(route.terms, token, split.fee)) throw new RefusedError(FEE_OVER_MAX);
  const latest = await relay.measuring.latest();
  const lasting = (deadline - latest.number + LATE_SLOTS) * relay.chain.slot;
  const validBefore = BigInt(latest.timestamp + lasting);

  /** The receipt wallet's authorization of `value` of the token to `to`, signed. */
  const signed = (to: Address, value: bigint): TransferAuthorization => {
    const nonce = crypto.getRandomValues(new Uint8Array(32));
    const hash = digest(domain, chainId, token, receipt.address, to, value, 0n, validBefore, nonce);
    const signature = secp256k1.sign(hash, receipt.privateKey, {
      prehash: false,
      format: "recovered",
    });
    return {
      payer: receipt.address,
      to,
      value,
      validAfter: 0n,
      validBefore,
      nonce,
      // r ‖ s ‖ v, as a key signs
      signature: concatBytes(signature.slice(1), Uint8Array.of((signature[0] as number) + 27)),
    };
  };

  const out = {
    to: token,
    data: transferCalldata(signed(route.payout, payoutOf(split))),
    value: 0n,
  };
  const submission: Submission = {
    calls: [paying, out].map((call, index) => ({ call, gas: quotes.gas?.[index] ?? null })),
    authorizations: [],
    token,
    payee: relay.payee,
    amount: split.fee,
    maxBlockNumber: deadline,
    transfer: signed(relay.payee, split.fee),
  };
  return {
    submission,
    transfer,
    digest: hex(transferDigest(domain, chainId, token, transfer)),
    split,
  };
}

/** `settlement`, a payment whose own transfer fails its quote's estimate refused for why (a
 * contract payer's, which the relay doesn't bound). */
async function settledAs(
  relay: Relay,
  route: Route,
  request: FacilitatorRequest,
): Promise<Settlement> {
  try {
    return await settlement(relay, route, request);
  } catch (e) {
    if (!(e instanceof SimulationFailedError)) throw e;
    const transfer = paymentTransfer(request.paymentPayload);
    throw new RefusedError(
      await transferRefusal(relay, request.paymentRequirements.asset, transfer),
    );
  }
}

/** x402's /verify, as its answer and the split it tells the merchant. */
export interface Verified {
  readonly response: { isValid: boolean; invalidReason?: string; payer?: Address };
  readonly split: Split | null;
}

/** x402's /settle, likewise. */
export interface Settled {
  readonly response: SettleResponse;
  readonly split: Split | null;
}

/**
 * x402's /verify: whether `request`'s payment would be settled for `route`'s merchant now, its
 * settlement made and simulated and nothing sent; and its split, as a quote.
 */
export async function verify(
  relay: Relay,
  route: Route,
  request: FacilitatorRequest,
): Promise<Verified> {
  const payer = request.paymentPayload.payload.authorization.from;
  const invalid = (invalidReason: string): Verified => ({
    response: { isValid: false, invalidReason, payer },
    split: null,
  });
  let made: Settlement;
  try {
    made = await settledAs(relay, route, request);
  } catch (e) {
    if (!(e instanceof RefusedError)) throw e;
    return invalid(e.reason);
  }
  try {
    await relay.wouldTake(made.submission);
  } catch (e) {
    if (!(e instanceof SimulationFailedError)) throw e;
    return invalid(await transferRefusal(relay, made.split.token, made.transfer));
  }
  return { response: { isValid: true, payer }, split: made.split };
}

/**
 * x402's /settle: `request`'s payment settled for `route`'s merchant, held until its submission
 * is included or its deadline passes. One being settled already, or settled, is answered by
 * that submission; one that wasn't included is tried again. Its split is answered by the call
 * that made the settlement.
 */
export async function settle(
  relay: Relay,
  route: Route,
  request: FacilitatorRequest,
): Promise<Settled> {
  const payer = request.paymentPayload.payload.authorization.from;
  const named = network(relay.chain.chainId);
  const failed = (errorReason: string): Settled => ({
    response: { success: false, transaction: "", network: named, errorReason, payer },
    split: null,
  });
  const token = request.paymentRequirements.asset;
  const domain = relay.domains.get(token);
  if (domain === undefined) return failed(ASSET_NOT_ACCEPTED);
  const buying = paymentTransfer(request.paymentPayload);
  const paid = hex(transferDigest(domain, relay.chain.chainId, token, buying));
  let split: Split | null = null;
  let id = relay.queue.settlement(paid);
  if (id !== null) {
    const earlier = relay.queue.status(id);
    // not included: another try
    if (!("submission" in earlier) && earlier.status !== "included") id = null;
  }
  if (id === null) {
    try {
      const made = await settledAs(relay, route, request);
      id = (await relay.submit(made.submission, null, null, made.digest)).id;
      split = made.split;
    } catch (e) {
      if (e instanceof RefusedError) return failed(e.reason);
      if (e instanceof SimulationFailedError) {
        return failed(await transferRefusal(relay, token, buying));
      }
      // the fee moved, or the queue is full: nothing sent, to be tried again
      if (e instanceof UnderpaidError || e instanceof NoCapacityError) return failed(NOT_INCLUDED);
      throw e;
    }
  }
  const tracked = await relay.queue.outcome(id);
  const transaction = transactionHash(tracked);
  if (tracked.status === "included" && transaction !== null) {
    return {
      response: {
        success: true,
        transaction,
        network: named,
        payer,
        amount: buying.value.toString(),
      },
      split,
    };
  }
  // it stopped simulating: why, if it's known
  if (tracked.revertData !== null) return failed(await transferRefusal(relay, token, buying));
  return failed(NOT_INCLUDED);
}
