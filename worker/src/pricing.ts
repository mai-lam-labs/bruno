/**
 * What a call costs on one chain, in each token it's paid in, and whether it fits: the fees a
 * transaction can pay by a quote's last block, our fee, and the margins; the limits on a call
 * and its transactions, and on what a sponsorship may cost; and the tokens taken. As
 * bruno/pricing.py's.
 */

import {
  type Chain,
  ChainError,
  hasCode,
  type Latest,
  marketTip,
  quantity,
  risenBaseFee,
} from "./chain.ts";
import type { ChainConfig } from "./config.ts";
import { risen } from "./datafee.ts";
import { quotedCalldata, transferGas, type UnsignedTransfer } from "./eip3009.ts";
import type { Deployment } from "./erc6492.ts";
import { type Address, type EncodedCall, hex, NATIVE_ASSET } from "./evm.ts";
import { authorizationsGas, type GasSchedule, intrinsic } from "./forks.ts";
import { Fraction } from "./fraction.ts";
import { type Answer, type Call, resultOf } from "./jsonrpc.ts";
import { lookup, type Measurements, scheduleOf, UnavailableError } from "./measurements.ts";
import { type CallAccounts, callAccounts } from "./accountreads.ts";
import {
  basesCost,
  boundsOf,
  callGas,
  type CostBasis,
  type Draft,
  draftOf,
  type Plan,
  planBasis,
  planCost,
  planDrafts,
  planGasLimit,
  planOf,
  proofOf,
  type QuotedProof,
} from "./plan.ts";
import type { Prices } from "./prices.ts";
import { callNeed, type Erc20, type PlannedCall, type Transfer } from "./runner.ts";
import {
  authorities as authoritiesOf,
  InvalidSubmissionError,
  type Submission,
} from "./submission.ts";
import { paying, type Tracked } from "./tracked.ts";
import { type Fees, type SignedAuthorization } from "./transactions.ts";
import { verification } from "./wallets.ts";
import { type Refused, refused } from "./refusals.ts";

/** How far the tip may rise over a quote's blocks, as a multiple of today's: a quote allows for
 * it, and an account is funded for it. */
const TIP_ALLOWANCE = 2n;
/** The largest amount: a quote's runner pushes it, so its initcode is never longer. */
export const MAX_AMOUNT = (1n << 256n) - 1n;
/** What a call whose gas a quote estimates is given over the estimate, as a fraction: state can
 * change before it's included (a user op's limits likewise, userops' GAS_HEADROOM). */
export const ESTIMATE_HEADROOM = { numerator: 11, denominator: 10 };

/** The fees a transaction can pay by a quote's last block, as of `latest`: the next base fee
 * risen for `quoteBlocks` blocks, and the tip doubled; what's paid beyond gas, risen for them
 * too. Accounts are funded to it. */
export function feeCeiling(latest: Latest, quoteBlocks: number): Fees {
  const tip = marketTip(latest.tips);
  return {
    maxFeePerGas: risenBaseFee(latest.nextBaseFee, quoteBlocks) + TIP_ALLOWANCE * tip,
    maxPriorityFeePerGas: tip,
    ...(latest.dataFee === undefined ? {} : { dataFee: risen(latest.dataFee, quoteBlocks) }),
  };
}

/** Transactions of `bases` cost no more at `ceiling`, together, than `maxGas` does at its max
 * fee: InvalidSubmissionError otherwise, naming `transactions`, with the most and what they
 * need, in wei. */
export function costWithin(bases: readonly CostBasis[], ceiling: Fees, maxGas: number): void {
  const most = BigInt(maxGas) * ceiling.maxFeePerGas;
  const needs = basesCost(bases, ceiling);
  if (needs > most) {
    throw new InvalidSubmissionError(
      "transactions",
      refused("sponsored-over", { needs, most }),
      { most, needs },
    );
  }
}

/** What `data` adds to a transaction's intrinsic gas, carried in it. */
function dataGas(data: Uint8Array, schedule: GasSchedule): number {
  return intrinsic(schedule, data).standard - intrinsic(schedule, new Uint8Array()).standard;
}

/** A batch's answer at `index`: ChainError if it's missing. */
function answerAt(answers: readonly Answer[], index: number): Answer {
  const answer = answers[index];
  if (answer === undefined) throw new ChainError("no answer");
  return answer;
}

/** A token or payee the relay doesn't take: why (`refused`). */
export class UnsupportedError extends Error {
  constructor(readonly refused: Refused) {
    super(String(refused));
  }
}

/** A quote in `token`: its `amount`, and for a call's, that amount broken down; and the transfer
 * its payer signs, if it pays by one (Relay.quote fills it in). */
export interface TokenQuote {
  readonly token: Address;
  readonly amount: bigint;
  readonly breakdown?: Breakdown;
  readonly transfer?: UnsignedTransfer;
}

/** A part of a quote: its gas, and its amount in the quote's token. */
export interface Part {
  readonly gas: number;
  readonly amount: bigint;
}

/**
 * A call's quote in parts, for people to read: setting up its account (its deployment and
 * authorizations), the call itself, the payment's transfer and signature check, and the relay's
 * own (its runner and deployer, the intrinsic base, our fee, the margins: the rest of the
 * amount). Each part's gas at the quote's fees and the token's price, rounded down, so the
 * relay's is never below zero and the parts sum to the amount.
 */
export interface Breakdown {
  readonly setup: Part;
  readonly call: Part;
  readonly payment: Part;
  readonly relay: Part;
}

/** A quote in each token, valid until `validUntilBlock`; its calls', the gas each is priced for,
 * in order (none for a user op's, its limits its own). */
export interface Quotes {
  readonly validUntilBlock: number;
  readonly quotes: readonly TokenQuote[];
  readonly gas?: readonly number[];
}

/**
 * One of the calls a quote prices: the call, or only its data's length (`call` null: priced at
 * its worst, every byte nonzero, to the authority or the payee); and its gas, null to estimate
 * it.
 */
export interface QuotedCall {
  readonly call: EncodedCall | null;
  readonly callDataLength: number;
  readonly gas: number | null;
}

/** What a quote prices: its `calls`, and their EIP-7702 authorizations, signed, or the signer of
 * one not signed yet (`authority`). */
export interface Quoted {
  readonly calls: readonly QuotedCall[];
  readonly authorizations: readonly SignedAuthorization[];
  readonly authority: Address | null;
}

/** Quoted calls as they're priced: each as it's planned (a stand-in of its length, if only that
 * was given; its gas given, estimated, or the relay's own bound of it), and what's read of
 * their accounts. */
export interface Priced {
  readonly calls: readonly PlannedCall[];
  readonly accounts: CallAccounts;
}

/**
 * What a quote is told of its payment's signer, each if known: its `address` (the payment then a
 * transfer, EIP-3009), an account to deploy first, and the gas its signature check takes.
 */
export interface Payer {
  readonly address?: Address;
  readonly deployment?: Deployment;
  readonly verifyGas?: number;
}

/** The quoted call's authorizations' signers: InvalidSubmissionError if one can't be recovered. */
export function quotedAuthorities(quoted: Quoted): Address[] {
  return quoted.authority !== null ? [quoted.authority] : authoritiesOf(quoted.authorizations);
}

/** The field each quoted call's length is given by: the call, or its length alone. */
export function lengthFields(quoted: Quoted): string[] {
  return quoted.calls.map((each, index) =>
    each.call !== null ? `calls[${index}]` : `calls[${index}].callDataLength`
  );
}

/**
 * How a quote prices its payment's transfer: whether there is one, and if its signer is a
 * contract, its check's gas (`verifyGas`) and its signature's length (`signatureBytes`); and
 * the gas its deployment's factory call takes, if one is made.
 */
interface Signing {
  readonly transferring: boolean;
  readonly verifyGas: number;
  readonly signatureBytes: number | null;
  readonly deployment: Deployment | null;
  readonly deployGas: number;
  readonly deploys: boolean;
}

export class Pricing {
  private readonly margins: Map<Address, Fraction>;

  constructor(
    private readonly chain: Chain,
    private readonly config: ChainConfig,
    private readonly prices: Prices,
    private readonly payee: Address,
  ) {
    this.margins = new Map(
      config.tokens.map((t) => [t.address, config.marginMultiplier.mul(t.marginMultiplier)]),
    );
  }

  /**
   * How `payer`'s transfer is priced, if `transferring`. Its signer is `payer.address`, else the
   * account its deployment deploys. A contract (one with `verifyGas` given, one deployed, or one
   * with code) has its check priced at `verifyGas`, else at the wallet gas schedule's figure for
   * its deployment's factory, else at the schedule's largest. A deployment is priced as the
   * runner checks it, and its factory call as estimated: for an account with no code, made
   * (InvalidSubmissionError if it reverts); for one with code, made only to prepare it
   * (ERC-6492), which a transfer's wrapped signature may ask, so priced if it can be.
   * InvalidSubmissionError too for `verifyGas` with no transfer to check, or over the most gas a
   * call may have. One read batch, if any.
   */
  async signing(
    measured: Measurements,
    payer: Payer,
    transferring: boolean,
    authorities: readonly Address[] = [],
  ): Promise<Signing> {
    if (payer.verifyGas !== undefined && !transferring) {
      throw new InvalidSubmissionError(
        "verifyGas",
        refused("verify-gas-no-payer"),
      );
    }
    if (payer.verifyGas !== undefined && payer.verifyGas > this.config.maxGas) {
      const most = this.config.maxGas;
      throw new InvalidSubmissionError("verifyGas", refused("gas-over", { most }), {
        most: BigInt(most),
      });
    }
    const deployment = payer.deployment ?? null;
    let signer = payer.address ?? null;
    if (signer === null && transferring && deployment !== null) signer = deployment.account;
    const calls: Call[] = [];
    if (deployment !== null) {
      calls.push(
        { method: "eth_getCode", params: [deployment.account, "latest"] },
        {
          method: "eth_estimateGas",
          params: [{ to: deployment.factory, data: hex(deployment.factoryData) }],
        },
      );
    }
    // the signer's own code, unless it's the account deployed
    const readSigner = signer !== null && (deployment === null || deployment.account !== signer);
    if (readSigner) calls.push({ method: "eth_getCode", params: [signer, "latest"] });
    const answers = calls.length > 0 ? await this.chain.batch(calls) : [];
    const schedule = scheduleOf(measured);
    let deployGas = 0;
    let deploys = false;
    if (deployment !== null) {
      deploys = !hasCode(resultOf(answerAt(answers, 0)));
      const estimate = answerAt(answers, 1);
      if ("error" in estimate) {
        if (deploys) {
          throw new InvalidSubmissionError(
            "deployment",
            refused("factory-fails", { why: estimate.error.nodeMessage }),
          );
        }
      } else if (deploys || transferring) {
        deployGas = Number(quantity(estimate.result)) -
          intrinsic(schedule, deployment.factoryData).standard;
      }
    }
    const signerHasCode = readSigner &&
      hasCode(resultOf(answerAt(answers, answers.length - 1)));
    const contract = transferring && (
      payer.verifyGas !== undefined ||
      (deployment !== null && deployment.account === signer) ||
      signerHasCode ||
      (signer !== null && authorities.includes(signer))
    );
    if (!contract) {
      return { transferring, verifyGas: 0, signatureBytes: null, deployment, deployGas, deploys };
    }
    const known = verification(deployment, schedule);
    return {
      transferring,
      verifyGas: payer.verifyGas ?? known.gas,
      signatureBytes: known.signatureBytes,
      deployment,
      deployGas,
      deploys,
    };
  }

  /**
   * `quoted` as it's priced: each call (its length's stand-in, if only that's given) with its
   * gas, and what's read of their accounts in one batch. A call the relay bounds is given its
   * bound, whatever gas it states, and isn't estimated. One that states no gas is estimated in
   * another batch (callGas), sent by `sender`, the others at their gas: InvalidSubmissionError
   * if a second one states none. `existing`: its authorities are taken to exist and each call
   * to have its gas, nothing read.
   */
  async priced(
    measured: Measurements,
    quoted: Quoted,
    tokens: readonly Address[],
    signing: Signing,
    sender: Address,
    existing = false,
  ): Promise<Priced> {
    const schedule = scheduleOf(measured);
    const authorities = quotedAuthorities(quoted);
    const target = authorities[0] ?? this.payee;
    const calls = quoted.calls.map((each): EncodedCall =>
      each.call ?? {
        to: target,
        data: new Uint8Array(each.callDataLength).fill(0xff),
        value: 0n,
      }
    );
    const accounts: CallAccounts = existing
      ? { delegated: new Set(authorities), empty: 0, delegating: new Set(authorities) }
      : await callAccounts(this.chain, calls.map((call) => call.to), authorities);
    const bounds: (number | null)[] = existing
      ? calls.map(() => null)
      : await boundsOf(this.chain, calls, measured.tokens, schedule, authorities);
    const unstated: number[] = [];
    const planned = calls.map((call, index): PlannedCall => {
      const bound = bounds[index] ?? null;
      const gas = quoted.calls[index]?.gas ?? null;
      if (bound !== null) return { call, gas: callNeed(bound), bounded: true };
      if (gas !== null) return { call, gas, bounded: false };
      unstated.push(index);
      return { call, gas: 0, bounded: false };
    });
    const [index, second] = unstated;
    if (second !== undefined) {
      throw new InvalidSubmissionError(`calls[${second}].gas`, refused("call-no-gas"));
    }
    if (index !== undefined) {
      // as paid in the first token (ETH if none): in another, the call finds that token's
      // balance cold, its estimate the higher
      const first = tokens[0];
      const token = first ?? NATIVE_ASSET;
      const erc20 = first === undefined
        ? null
        : lookup(measured.tokens, first, "a token's measurement");
      const draft = this.draft(measured, quoted, planned, accounts, token, erc20, signing);
      // its deployment estimated only if it's made: its account has no code
      const deployment = signing.deploys ? signing.deployment : null;
      const estimating: Draft<QuotedProof> = {
        ...draft,
        deployment,
        deployGas: deployment === null ? null : signing.deployGas,
      };
      const gas = await this.estimatedGas(quoted, index, estimating, accounts, schedule, sender);
      const call = calls[index];
      if (call === undefined) throw new Error("a call for each quoted");
      planned[index] = { call, gas, bounded: false };
    }
    return { calls: planned, accounts };
  }

  /** `quoted` in each of `tokens`, as it's priced (priced), sent by `sender`: in each, the
   * transaction it would be sent as, drafted as a submission paying the most it can, and planned
   * as a submission is (planDrafts). */
  async quoteAt(
    measured: Measurements,
    latest: Latest,
    quoted: Quoted,
    tokens: readonly Address[],
    signing: Signing,
    sender: Address,
    existing = false,
  ): Promise<Quotes> {
    const schedule = scheduleOf(measured);
    const { calls, accounts } = await this.priced(
      measured,
      quoted,
      tokens,
      signing,
      sender,
      existing,
    );
    const authorities = quotedAuthorities(quoted);
    this.withinLimits(
      calls.map((planned) => planned.gas),
      quoted.calls.map((each) => each.callDataLength),
      lengthFields(quoted),
    );
    // the base fee risen and the tip doubled, as they can be by the quote's last block
    const fees = feeCeiling(latest, this.config.quoteBlocks - 1);
    const drafts = tokens.map((token) => {
      const erc20 = lookup(measured.tokens, token, "a token's measurement");
      return this.draft(measured, quoted, calls, accounts, token, erc20, signing);
    });
    const planned = await planDrafts(
      this.chain,
      drafts.map((value) => ({ status: "fulfilled", value })),
      schedule,
      sender,
    );
    const quotes = [];
    for (const [i, token] of tokens.entries()) {
      const draft = drafts[i];
      if (draft === undefined) throw new Error("a draft for each token");
      const plan = planOf(planned[i]);
      this.fits(plan);
      const amount = await this.charge(measured, latest, token, plan, fees);
      const deployment = signing.deployment;
      const setupGas = signing.deployGas +
        (deployment === null ? 0 : dataGas(deployment.factoryData, schedule)) +
        schedule.authorization * authorities.length +
        authorizationsGas(schedule, accounts.empty, authorities.length);
      let callGasPart = 0;
      for (const planned of calls) {
        callGasPart += draft.proof.callPart(planned.gas) + dataGas(planned.call.data, schedule);
      }
      const paid = draft.transfer;
      const paymentGas = paid === null ? 0 : (paid.gas ?? 0) + dataGas(paid.calldata, schedule);
      const breakdown = await this.breakdown(
        token,
        amount,
        planGasLimit(plan),
        fees,
        setupGas,
        callGasPart,
        paymentGas,
      );
      quotes.push({ token, amount, breakdown });
    }
    return {
      validUntilBlock: latest.number + this.config.quoteBlocks,
      quotes,
      gas: calls.map((planned) => planned.gas),
    };
  }

  /**
   * The transaction `quoted` would be sent as, paid in `token` (`erc20` as measured, null for
   * ETH), drafted as a submission paying the most it can (MAX_AMOUNT), its calls as `planned`: its
   * transfer's calldata a stand-in of its length, its gas bounded, its signature's check at
   * `signing`'s figure; its deployment's factory call at the gas `signing` found; its proof
   * chosen as a submission's is (proofOf).
   */
  private draft(
    measured: Measurements,
    quoted: Quoted,
    planned: readonly PlannedCall[],
    accounts: CallAccounts,
    token: Address,
    erc20: Erc20 | null,
    signing: Signing,
  ): Draft<QuotedProof> {
    let transfer: Transfer | null = null;
    if (signing.transferring && erc20 !== null && erc20.transferGas !== undefined) {
      const schedule = scheduleOf(measured);
      const bound = transferGas(erc20.transferGas, schedule, measured.payeeHolds.has(token));
      transfer = {
        calldata: quotedCalldata(signing.signatureBytes),
        gas: bound + signing.verifyGas,
      };
    }
    const deployment = signing.deployment;
    const submission: Submission = {
      calls: planned.map((each) => ({ call: each.call, gas: each.gas })),
      authorizations: quoted.authorizations,
      token,
      payee: this.payee,
      amount: MAX_AMOUNT,
      maxBlockNumber: 0,
      ...(deployment === null ? {} : { deployment }),
    };
    const proof = proofOf(this.chain.chainId, planned.map((each) => each.call), {
      transfer: transfer === null ? null : { to: token, data: transfer.calldata, value: 0n },
      deployment,
    });
    return {
      ...draftOf(submission, proof, quotedAuthorities(quoted), accounts, erc20, planned),
      transfer,
      deployment,
      deployGas: signing.deployGas,
    };
  }

  /** `amount` in parts (Breakdown): each part's gas at `fees`' max fee and the token's price,
   * rounded down; the relay's, the rest of the gas limit and of the amount. */
  private async breakdown(
    token: Address,
    amount: bigint,
    gasLimit: number,
    fees: Fees,
    setupGas: number,
    callGas: number,
    paymentGas: number,
  ): Promise<Breakdown> {
    const wei = await this.prices.wei(token);
    const part = (gas: number): Part => ({
      gas,
      amount: new Fraction(BigInt(gas) * fees.maxFeePerGas).div(wei).floor(),
    });
    const setup = part(setupGas);
    const call = part(callGas);
    const payment = part(paymentGas);
    const relay = {
      gas: gasLimit - setup.gas - call.gas - payment.gas,
      amount: amount - setup.amount - call.amount - payment.amount,
    };
    return { setup, call, payment, relay };
  }

  /**
   * The gas `quoted`'s `index`th call is given, unstated: its estimate (callGas) as `draft`
   * sends it, with ESTIMATE_HEADROOM. Only for a call given whole, and for an authority's account already
   * delegated, an unsigned authorization's delegate being unknown (InvalidSubmissionError,
   * `gas`).
   */
  private async estimatedGas(
    quoted: Quoted,
    index: number,
    draft: Draft<QuotedProof>,
    accounts: CallAccounts,
    schedule: GasSchedule,
    sender: Address,
  ): Promise<number> {
    const field = `calls[${index}].gas`;
    if (quoted.calls[index]?.call === null) {
      throw new InvalidSubmissionError(field, refused("quote-no-gas"));
    }
    const authority = quoted.authority;
    if (authority !== null && !accounts.delegating.has(authority)) {
      throw new InvalidSubmissionError(field, refused("authority-undelegated", { authority }));
    }
    const estimate = await callGas(this.chain, draft, index, schedule, sender);
    return Math.ceil(
      (estimate * ESTIMATE_HEADROOM.numerator) / ESTIMATE_HEADROOM.denominator,
    );
  }

  /**
   * The plan's transaction within the most gas a transaction may use, everything in it: the
   * runner, a deployment, a transfer and its check, and the call.
   */
  fits(plan: Plan): void {
    const most = this.config.maxGas;
    const needs = planGasLimit(plan);
    if (needs > most) {
      throw new InvalidSubmissionError(
        "calls",
        refused("transaction-over", { needs, most }),
        {
          most: BigInt(most),
          needs: BigInt(needs),
        },
      );
    }
  }

  /**
   * What the client chooses of a sponsorship's cost is bounded when it's quoted and taken, its
   * deadline `blocks` on: the relay's own transactions' gas at the fee ceiling then, and the ETH
   * they send (what the account is sent: its transactions' gas at the max fees it signed, and
   * their value), within `maxGas` at that ceiling. A value, or a max fee over what the deadline
   * needs, takes the room gas would. What the account is sent is fixed from then on. The relay's
   * own gas isn't: it's signed each block at that block's fees, as a call's is, and only where
   * the payment covers its charge there, so a tip risen past this ceiling may take a
   * sponsorship's cost over `most`, never over what a call of `maxGas` costs in the same block.
   */
  withinCost(plan: Plan, latest: Latest, blocks: number): void {
    costWithin(planBasis(plan), feeCeiling(latest, blocks), this.config.maxGas);
  }

  /**
   * The calls' own gas, and their lengths, within the limits, each call's and all of them
   * together: calls given more than a whole transaction may use can never fit in one. A refusal
   * names the call over a limit on its own, or `calls` where they're over it together.
   * `lengthFields`: what gave each call's length, the call itself if not said.
   */
  withinLimits(
    gas: readonly number[],
    callBytes: readonly number[],
    lengthFields: readonly string[] = [],
  ): void {
    const { maxGas, maxCallBytes } = this.config;
    const sum = (of: readonly number[]): number => of.reduce((total, each) => total + each, 0);
    const over = gas.findIndex((each) => each > maxGas);
    if (over !== -1 || sum(gas) > maxGas) {
      throw new InvalidSubmissionError(
        over !== -1 ? `calls[${over}].gas` : "calls",
        refused("gas-over", { most: maxGas }),
        { most: BigInt(maxGas) },
      );
    }
    const long = callBytes.findIndex((each) => each > maxCallBytes);
    if (long !== -1 || sum(callBytes) > maxCallBytes) {
      throw new InvalidSubmissionError(
        long !== -1 ? lengthFields[long] ?? `calls[${long}]` : "calls",
        refused("call-too-long", { most: maxCallBytes }),
        { most: BigInt(maxCallBytes) },
      );
    }
  }

  supported(measured: Measurements, token: Address): void {
    if (!this.margins.has(token)) {
      throw new UnsupportedError(refused("not-accepted", { token }));
    }
    const why = measured.suspended.get(token);
    if (why !== undefined) {
      throw new UnsupportedError(refused("suspended", { token, why }));
    }
  }

  /** The tip each transaction offers: the market's (marketTip). */
  tip(latest: Latest): bigint {
    return marketTip(latest.tips);
  }

  /** The fees of the block after `latest`: exactly its base fee, and the tip; what's paid
   * beyond gas risen for the one block (its L1 inputs may be a new L1 block's). */
  fees(latest: Latest): Fees {
    const tip = this.tip(latest);
    return {
      maxFeePerGas: latest.nextBaseFee + tip,
      maxPriorityFeePerGas: tip,
      ...(latest.dataFee === undefined ? {} : { dataFee: risen(latest.dataFee, 1) }),
    };
  }

  /** Our fee on a transaction of `gasLimit`, in wei: the tip a gas, at least `minFeePerGas`;
   * and at least `minFee`. */
  private async ourFee(measured: Measurements, latest: Latest, gasLimit: number): Promise<bigint> {
    const tip = this.tip(latest);
    const perGas = tip > this.config.minFeePerGas ? tip : this.config.minFeePerGas;
    const fee = perGas * BigInt(gasLimit);
    const minFee = this.config.minFee;
    if (minFee === null) return fee;
    const decimals = measured.decimals.get(minFee.token);
    if (decimals === undefined) {
      throw new UnavailableError(refused("min-fee-suspended", { token: minFee.token }));
    }
    const units = minFee.amount.mul(new Fraction(10n ** BigInt(decimals))).ceil();
    const least = (await this.prices.wei(minFee.token)).mul(units).ceil();
    return fee > least ? fee : least;
  }

  /** What `plan`'s transaction is charged in `token`: its cost at `fees` (the fees of the block
   * after `latest`, if not given), and our fee, margins included. */
  async charge(
    measured: Measurements,
    latest: Latest,
    token: Address,
    plan: Plan,
    fees: Fees = this.fees(latest),
  ): Promise<bigint> {
    const cost = planCost(plan, fees);
    return await this.price(token, cost + await this.ourFee(measured, latest, planGasLimit(plan)));
  }

  /** `wei` in `token`'s base units, margins included, rounded up. */
  async price(token: Address, wei: bigint): Promise<bigint> {
    const margin = lookup(this.margins, token, "a token's margin");
    return new Fraction(wei).div(await this.prices.wei(token)).mul(margin).ceil();
  }

  /** What the submission pays in a block of `baseFee`, in wei, less what the relay's own
   * transactions cost at `fees`. */
  async profit(tracked: Tracked, fees: Fees, baseFee: bigint): Promise<Fraction> {
    const wei = await this.prices.wei(tracked.submission.token);
    return wei.mul(paying(tracked, baseFee)).sub(basesCost(tracked.basis, fees));
  }
}
