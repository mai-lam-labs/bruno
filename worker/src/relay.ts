/**
 * The relay on one chain: what it offers, its quotes, the submissions it takes; it holds its
 * queue, pricing, measuring and batches (batches.ts), each owning its own state, kept in a Store
 * so it outlives the object (docs/development/design/queue.md, price.md).
 */

import { type Builder } from "./builders.ts";
import { Batches } from "./batches.ts";
import { type Chain, type Finality, type Latest, risenBaseFee } from "./chain.ts";
import type { ChainConfig, Eip712Domain } from "./config.ts";
import { lastBlockBefore, transferCalldata, type UnsignedTransfer } from "./eip3009.ts";
import { runnerSalt, saltKey } from "./deployer.ts";
import { receiptAccount, receiptRoot } from "./receipt.ts";
import { type Address, bytes, type Hex, hex, NATIVE_ASSET } from "./evm.ts";
import { Fraction } from "./fraction.ts";
import { forwarderOf } from "./forwarder.ts";
import {
  basesCost,
  CheckedByForwarder,
  makePlan,
  type Plan,
  planBasis,
  type Proof,
  proofOf,
} from "./plan.ts";
import { type Asked, isSigned, type SponsoredQuotes, sponsoredQuotes } from "./sponsoring.ts";
import { isSponsorship, type Sponsorship, sponsorshipId } from "./sponsorship.ts";
import { BlockUnseenError, simulate } from "./simulation.ts";
import { type Prices } from "./prices.ts";
import {
  lengthFields,
  NO_PAYER,
  type Payer,
  Pricing,
  type Quoted,
  quotedAuthorities,
  type Quotes,
  type TokenQuote,
  UnsupportedError,
} from "./pricing.ts";
import { type Measurements, Measuring, scheduleOf, UnavailableError } from "./measurements.ts";
import {
  checkAuthorizations,
  InvalidSubmissionError,
  type Submission,
  submissionId,
  toDeploy,
} from "./submission.ts";
import type { Store } from "./store.ts";
import { paying, Queue, type Tracked } from "./tracked.ts";
import { type Decoded, type Fees, type Signer } from "./transactions.ts";
import { Millis, Seconds } from "./units.ts";
import type { FeePayment } from "./userops.ts";
import { type Refused, refused } from "./refusals.ts";

/** How soon a step that fails (the nodes not answering, say) is tried again: twice as long after
 * each failure in a row, up to MAX_RETRY. */
export const RETRY = Seconds(1);
export const MAX_RETRY = Seconds(300);

export class RelayError extends Error {}

/** What was last read of the chain, and when the next block, which may change it, is due (by
 * the wall clock, as block timestamps are). As bruno/relay.py's _Recent. */
interface Recent<T> {
  readonly read: T;
  readonly due: Millis;
}

/** `read` as of the block at `timestamp`: the next is due when `chain` can read it. */
function recent<T>(read: T, timestamp: number, chain: Chain): Recent<T> {
  return { read, due: chain.nextBlockAt(timestamp) };
}

function fresh<T>(r: Recent<T> | null): r is Recent<T> {
  return r !== null && Millis.unix() < r.due;
}

export class UnderpaidError extends RelayError {
  readonly refused: Refused;

  constructor(readonly token: Address, readonly required: bigint) {
    const why = refused("underpaid", { required, token });
    super(String(why));
    this.refused = why;
  }
}

/** The queue is full of more profitable submissions: `required` would beat the least. */
export class NoCapacityError extends RelayError {
  readonly refused: Refused;

  constructor(readonly token: Address, readonly required: bigint) {
    const why = refused("no-capacity", { required, token });
    super(String(why));
    this.refused = why;
  }
}

export interface Terms {
  readonly payee: Address;
  readonly forwarder: Address;
  readonly tokens: readonly Address[];
  readonly maxGas: number;
  readonly maxCallBytes: number;
}

/** A submission's id, and whether it's new: one already taken is taken once. */
export interface Accepted {
  readonly id: Hex;
  readonly isNew: boolean;
}

export class Relay {
  /** The submissions taken, pending and finished. */
  readonly queue: Queue;
  /** Where a sponsorship's payment is made, and checked. */
  readonly forwarder: Address;
  /** What a call costs, and whether it fits. */
  readonly pricing: Pricing;
  /** The latest block as the x402 challenge last read it. */
  private challenged: Recent<Latest> | null = null;
  /** The latest and finalized blocks as a status last read them. */
  private final: Recent<Finality> | null = null;
  /** Called when a submission is taken: the Durable Object arms its alarm, so the relay runs
   * while a request is held for it. */
  onTaken: () => Promise<void> = async () => {};
  /** What's known of the chain as last read. */
  readonly measuring: Measuring;
  /** Each batch, mapped onto the concurrency accounts, and the funding in flight. */
  readonly batches: Batches;
  /** Each token taken over x402, and its EIP-712 domain, as configured. */
  readonly domains: ReadonlyMap<Address, Eip712Domain>;

  constructor(
    readonly chain: Chain,
    readonly config: ChainConfig,
    readonly floatAccount: Signer,
    readonly prices: Prices,
    readonly builders: readonly Builder[],
    private readonly store: Store,
  ) {
    this.forwarder = forwarderOf(config.payee).address;
    this.pricing = new Pricing(chain, config, prices, this.payee);
    this.domains = new Map(
      config.tokens.flatMap((t) =>
        t.eip3009 === null || t.address === NATIVE_ASSET ? [] : [[t.address, t.eip3009]]
      ),
    );
    this.queue = new Queue(store, Millis.ofSeconds(config.retentionSeconds));
    this.measuring = new Measuring(chain, config, this.forwarder, store);
    this.batches = new Batches(
      chain,
      config,
      floatAccount,
      builders,
      this.forwarder,
      store,
      this.queue,
      this.pricing,
      this.measuring,
    );
  }

  /** How long an x402 payment's request may be held: its deadline's furthest. */
  get x402TimeoutSeconds(): number {
    return this.config.x402DeadlineBlocks * this.chain.slot;
  }

  get payee(): Address {
    return this.config.payee;
  }

  terms(): Terms {
    return {
      payee: this.payee,
      forwarder: this.forwarder,
      tokens: this.config.tokens.map((t) => t.address),
      maxGas: this.config.maxGas,
      maxCallBytes: this.config.maxCallBytes,
    };
  }

  /** What `quoted` costs in each of `tokens`, its gas estimated if not given: enough for
   * `quoteBlocks` blocks of base-fee rise, the tip doubled, and our fee; paid by a transfer from
   * `payer`, if it names one, its deployment made first. */
  async quote(
    quoted: Quoted,
    tokens: readonly Address[],
    payer: Payer = NO_PAYER,
  ): Promise<Quotes> {
    this.check(quoted);
    const measured = await this.measuring.available();
    tokens.forEach((t) => this.pricing.supported(measured, t));
    const latest = await this.measuring.latest();
    const signing = await this.pricing.signing(
      measured,
      payer,
      payer.address !== undefined,
      quotedAuthorities(quoted),
    );
    const quotes = await this.pricing.quoteAt(
      measured,
      latest,
      quoted,
      tokens,
      signing,
      this.batches.sender,
    );
    const signer = payer.address;
    if (signer === undefined) return quotes;
    return {
      ...quotes,
      quotes: quotes.quotes.map((q) => this.toSign(q, signer, latest, measured)),
    };
  }

  /** `quote` with the transfer its payer signs, if its token takes one: to the payee, of the
   * amount, valid from now for the quote's blocks and the longest deadline, its nonce fresh. */
  private toSign(
    quote: TokenQuote,
    payer: Address,
    latest: Latest,
    measured: Measurements,
  ): TokenQuote {
    const domain = this.domains.get(quote.token);
    if (domain === undefined || !this.x402Tokens(measured).includes(quote.token)) return quote;
    const lasting = (this.config.quoteBlocks + this.config.maxDeadlineBlocks) * this.chain.slot;
    const transfer: UnsignedTransfer = {
      domain,
      chainId: this.chain.chainId,
      token: quote.token,
      payer,
      to: this.payee,
      value: quote.amount,
      validAfter: 0n,
      validBefore: BigInt(latest.timestamp + lasting),
      nonce: crypto.getRandomValues(new Uint8Array(32)),
    };
    return { ...quote, transfer };
  }

  /** `quote` in each token accepted over x402, the runner making its transfer: as of the latest
   * block, read at most once a slot however many ask. `existing`: the authority is taken to
   * exist, unread. */
  async x402Quote(quoted: Quoted, payer: Payer = {}, existing = false): Promise<Quotes> {
    this.check(quoted);
    const measured = await this.measuring.available();
    const latest = await this.recentLatest();
    const tokens = this.x402Tokens(measured);
    const signing = await this.pricing.signing(measured, payer, true, quotedAuthorities(quoted));
    return await this.pricing.quoteAt(
      measured,
      latest,
      quoted,
      tokens,
      signing,
      this.batches.sender,
      existing,
    );
  }

  /** The gas each of an x402 payment's calls is given: its own, the relay's bound, or its
   * estimate, as its challenge priced it, estimated again now it's paid. */
  async x402Gas(quoted: Quoted, payer: Payer): Promise<number[]> {
    this.check(quoted);
    const measured = await this.measuring.available();
    const signing = await this.pricing.signing(measured, payer, true, quotedAuthorities(quoted));
    const tokens = this.x402Tokens(measured);
    const sender = this.batches.sender;
    const priced = await this.pricing.priced(measured, quoted, tokens, signing, sender);
    return priced.calls.map((planned) => planned.gas);
  }

  /** A quoted call's own checks, as a submission's: its length its call's, its authorizations
   * signed or their signer given, not both, and within the limits. */
  private check(quoted: Quoted): void {
    quoted.calls.forEach((each, index) => {
      if (each.call !== null && each.call.data.length !== each.callDataLength) {
        throw new InvalidSubmissionError(
          `calls[${index}].callDataLength`,
          refused("call-length-mismatch", {
            given: each.callDataLength,
            length: each.call.data.length,
          }),
        );
      }
    });
    if (quoted.authority !== null && quoted.authorizations.length > 0) {
      throw new InvalidSubmissionError(
        "authority",
        refused("authority-and-list"),
      );
    }
    checkAuthorizations(
      quoted.authorizations,
      this.chain.chainId,
      this.config.maxAuthorizations,
    );
    this.pricing.withinLimits(
      quoted.calls.map((each) => each.gas ?? 0),
      quoted.calls.map((each) => each.callDataLength),
      lengthFields(quoted),
    );
  }

  /** The tokens taken over x402 as measured: each configured one whose transfer was. */
  x402Tokens(measured: Measurements): Address[] {
    return [...this.domains.keys()].filter((t) =>
      measured.tokens.get(t)?.transferGas !== undefined
    );
  }

  /** The last block an x402 payment valid after `validAfter` and before `validBefore` (Unix
   * seconds) can be included in: at most `blocks` ahead, `x402DeadlineBlocks` if not given. */
  async x402Deadline(
    validAfter: bigint,
    validBefore: bigint,
    blocks: number | null = null,
  ): Promise<number> {
    const latest = await this.recentLatest();
    if (validAfter >= BigInt(latest.timestamp + this.chain.slot)) {
      throw new InvalidSubmissionError(
        "validAfter",
        refused("valid-after"),
      );
    }
    const last = lastBlockBefore(latest, validBefore, this.chain.slot);
    if (last <= latest.number) {
      throw new InvalidSubmissionError("validBefore", refused("valid-before"));
    }
    return Math.min(last, latest.number + (blocks ?? this.config.x402DeadlineBlocks));
  }

  /** The receipt wallet of the payout address `payout` on this chain, which a merchant's buyers
   * pay and the hosted facilitator settles from (receipt.ts). */
  receiptAccount(payout: Address): Signer {
    return receiptAccount(receiptRoot(this.floatAccount), payout);
  }

  /** The latest block, read again only once the next is due, or once a later block has been
   * seen: the x402 challenge is answered to anyone who asks, probes included, and a payment's
   * deadline counts from this block. */
  private async recentLatest(): Promise<Latest> {
    const last = this.challenged;
    if (fresh(last) && last.read.number >= this.chain.seen) return last.read;
    const latest = await this.measuring.latest();
    this.challenged = recent(latest, latest.timestamp, this.chain);
    return latest;
  }

  /** Take `submission`, checked, sent and its payment known as proofOf has it; or throw why not.
   * `feePayment`: what an op paying by its fees pays, its call the EntryPoint's handleOps of it;
   * `userOperation`: the hash of the UserOp it sends, if it's one; `settlement`: the digest of
   * the payment it settles, if it's a facilitator's. */
  async submit(
    submission: Submission,
    feePayment: FeePayment | null = null,
    userOperation: Hex | null = null,
    settlement: Hex | null = null,
  ): Promise<Accepted> {
    const id = hex(submissionId(submission));
    const planned = await this.planned(submission, id, feePayment);
    if (planned === null) return { id, isNew: false };
    const tracked = tracking(submission, id, planned, userOperation, settlement);
    return await this.take(tracked, planned.plan, planned.measured, planned.latest);
  }

  /** `submission` checked, priced and simulated as submit does, and nothing taken: throw why it
   * wouldn't be. */
  async wouldTake(submission: Submission): Promise<void> {
    const id = hex(submissionId(submission));
    const planned = await this.planned(submission, id, null);
    if (planned === null) return;
    const taking = tracking(submission, id, planned, null, null);
    await this.checkTaken(taking, planned.plan, planned.measured, planned.latest);
  }

  /** `submission`'s own checks, then its proof and its plan as of the latest block: null if
   * it's taken already. */
  private async planned(
    submission: Submission,
    id: Hex,
    feePayment: FeePayment | null,
  ): Promise<Planning | null> {
    this.pricing.withinLimits(
      submission.calls.map((each) => each.gas ?? 0),
      submission.calls.map((each) => each.call.data.length),
    );
    const measured = await this.measuring.available();
    this.pricing.supported(measured, submission.token);
    if (submission.payee !== this.payee) {
      throw new UnsupportedError(
        refused("wrong-payee", { payee: this.payee, given: submission.payee }),
      );
    }
    checkAuthorizations(
      submission.authorizations,
      this.chain.chainId,
      this.config.maxAuthorizations,
    );
    const transfer = submission.transfer;
    if (transfer !== undefined) {
      if (!this.x402Tokens(measured).includes(submission.token)) {
        throw new UnsupportedError(refused("no-eip3009", { token: submission.token }));
      }
      if (transfer.to !== submission.payee || transfer.value !== submission.amount) {
        throw new InvalidSubmissionError(
          "payment",
          refused("transfer-not-payment"),
        );
      }
    }
    if (this.queue.known(id)) return null;
    const latest = await this.measuring.latest();
    this.checkDeadline(submission.maxBlockNumber, latest.number);
    const schedule = scheduleOf(measured);
    const sender = this.batches.sender;
    const paidBy = transfer === undefined
      ? null
      : { to: submission.token, data: transferCalldata(transfer), value: 0n };
    const proof = proofOf(this.chain.chainId, submission.calls.map((each) => each.call), {
      transfer: paidBy,
      deployment: toDeploy(submission),
      feePayment,
    });
    const plan = await makePlan(
      this.chain,
      submission,
      proof,
      schedule,
      measured.tokens,
      sender,
      measured.payeeHolds,
      false,
      runnerSalt(saltKey(this.floatAccount), bytes(id), latest.number + 1),
    );
    return { proof, plan, measured, latest };
  }

  /**
   * What the sponsorship `asked` about costs in each of `tokens`, and what its account signs for
   * it (sponsoredQuotes): its calls within the limits, as a submission's, and its
   * authorizations and its transactions signed already checked as a sponsorship of them will
   * be.
   */
  async quoteSponsorship(asked: Asked, tokens: readonly Address[]): Promise<SponsoredQuotes> {
    this.pricing.withinLimits(
      asked.calls.map((each) => each.gas ?? 0),
      asked.calls.map((each) => each.call.data.length),
    );
    const measured = await this.measuring.sponsoring();
    for (const token of tokens) this.pricing.supported(measured, token);
    checkAuthorizations(asked.authorizations, this.chain.chainId, this.config.maxAuthorizations);
    const latest = await this.measuring.latest();
    const deadline = asked.maxBlockNumber ?? latest.number + this.config.maxDeadlineBlocks;
    this.checkDeadline(deadline, latest.number);
    const signed = asked.transactions.flatMap((t, index) =>
      isSigned(t) ? [{ index, transaction: t }] : []
    );
    this.checkSigned(signed, deadline, latest);
    try {
      return await sponsoredQuotes(
        this.chain,
        this.config,
        this.pricing,
        measured,
        latest,
        asked,
        tokens,
        this.batches.sender,
        this.forwarder,
      );
    } catch (e) {
      // nothing known of it yet: the client tries again
      if (e instanceof BlockUnseenError) throw new UnavailableError(e.refused);
      throw e;
    }
  }

  /** Each of a sponsorship's signed `transactions`, with its place among them, is for this
   * chain, at a max fee a block may ask by `deadline`: InvalidSubmissionError otherwise. */
  private checkSigned(
    transactions: readonly { index: number; transaction: Decoded }[],
    deadline: number,
    latest: Latest,
  ): void {
    const needs = risenBaseFee(latest.nextBaseFee, deadline - latest.number);
    for (const { index, transaction } of transactions) {
      const field = `transactions[${index}]`;
      if (transaction.chainId !== BigInt(this.chain.chainId)) {
        throw new InvalidSubmissionError(
          field,
          refused("transaction-chain", { chain_id: transaction.chainId.toString() }),
        );
      }
      const given = transaction.signed.maxFeePerGas;
      if (given < needs) {
        throw new InvalidSubmissionError(
          field,
          refused("transaction-fee", { given: given.toString(), needs: needs.toString() }),
        );
      }
    }
  }

  /**
   * Take `sponsorship`, checked, priced as it is and simulated whole; or throw why not. Its
   * calls are within the limits, as a submission's; it pays the relay's forwarder; its
   * authorizations and transactions are for this chain, each transaction's max fee what a block
   * may ask by its deadline; and its account has no other pending.
   */
  async sponsor(sponsorship: Sponsorship): Promise<Accepted> {
    this.pricing.withinLimits(
      sponsorship.calls.map((each) => each.gas ?? 0),
      sponsorship.calls.map((each) => each.call.data.length),
    );
    const measured = await this.measuring.sponsoring();
    this.pricing.supported(measured, sponsorship.token);
    if (sponsorship.payee !== this.forwarder) {
      throw new UnsupportedError(
        refused("wrong-payee", { payee: this.forwarder, given: sponsorship.payee }),
      );
    }
    checkAuthorizations(
      sponsorship.authorizations,
      this.chain.chainId,
      this.config.maxAuthorizations,
    );
    const id = hex(sponsorshipId(sponsorship));
    if (this.queue.known(id)) {
      return { id, isNew: false };
    }
    const latest = await this.measuring.latest();
    this.checkDeadline(sponsorship.maxBlockNumber, latest.number);
    this.checkSigned(
      sponsorship.transactions.map((t, index) => ({ index, transaction: t })),
      sponsorship.maxBlockNumber,
      latest,
    );
    if (this.queue.sponsoring(sponsorship.account) !== null) {
      throw new UnavailableError(refused("sponsored-in-flight", { account: sponsorship.account }));
    }
    const proof = new CheckedByForwarder();
    const plan = await makePlan(
      this.chain,
      sponsorship,
      proof,
      scheduleOf(measured),
      measured.tokens,
      this.batches.sender,
      measured.payeeHolds,
      measured.payeeNew,
    );
    const tracked = tracking(sponsorship, id, { proof, plan, measured, latest }, null, null);
    return await this.take(tracked, plan, measured, latest);
  }

  /** A deadline after `head` and at most `maxDeadlineBlocks` past it: InvalidSubmissionError
   * otherwise. */
  private checkDeadline(maxBlockNumber: number, head: number): void {
    if (maxBlockNumber <= head) {
      throw new InvalidSubmissionError("maxBlockNumber", refused("deadline-passed"));
    }
    if (maxBlockNumber > head + this.config.maxDeadlineBlocks) {
      const ahead = this.config.maxDeadlineBlocks;
      throw new InvalidSubmissionError(
        "maxBlockNumber",
        refused("deadline-far", { ahead }),
        {
          most: BigInt(head + ahead),
        },
      );
    }
  }

  /** What `tracked` is of, planned, is within the most gas and the most it may cost, pays its
   * charge, and simulates; or throw why not. */
  private async checkTaken(
    tracked: Tracked,
    plan: Plan,
    measured: Measurements,
    latest: Latest,
  ): Promise<void> {
    const taken = tracked.submission;
    this.pricing.fits(plan);
    this.pricing.withinCost(
      plan,
      latest,
      taken.maxBlockNumber - latest.number,
      isSponsorship(taken) ? "transactions" : "calls",
    );
    const required = await this.pricing.charge(measured, latest, taken.token, plan);
    if (paying(tracked, latest.nextBaseFee) < required) {
      throw new UnderpaidError(taken.token, required);
    }
    try {
      await simulate(this.chain, taken, plan, this.batches.sender);
    } catch (e) {
      // nothing known of it yet: the client tries again
      if (e instanceof BlockUnseenError) throw new UnavailableError(e.refused);
      throw e;
    }
  }

  /** `tracked`, checked (checkTaken), into the queue; or throw why not. */
  private async take(
    tracked: Tracked,
    plan: Plan,
    measured: Measurements,
    latest: Latest,
  ): Promise<Accepted> {
    const taken = tracked.submission;
    const id = tracked.id;
    await this.checkTaken(tracked, plan, measured, latest);
    const fees = this.pricing.fees(latest);
    return await this.queue.queued(async () => {
      if (this.queue.known(id)) { // taken while this one was checked
        return { id, isNew: false };
      }
      if (isSponsorship(taken) && this.queue.sponsoring(taken.account) !== null) {
        throw new UnavailableError(refused("sponsored-in-flight", { account: taken.account }));
      }
      if (this.queue.pending().length >= this.config.maxPending) {
        await this.makeRoom(tracked, fees, latest.nextBaseFee);
      }
      this.queue.add(tracked);
      await this.onTaken();
      return { id, isNew: true };
    });
  }

  /**
   * Evict the least profitable of the queue for `tracked`, if `tracked` is more profitable; or
   * throw NoCapacityError with what would be taken. A submission whose transaction is out isn't
   * evicted: it may land. Every profit is read first (a price may be read of the chain), and the
   * queue taken as it is then: the relay's block goes on meanwhile, and may have sent some, or
   * finished them, making room.
   */
  private async makeRoom(tracked: Tracked, fees: Fees, baseFee: bigint): Promise<void> {
    const read: { profit: Fraction; tracked: Tracked }[] = [];
    for (const t of this.queue.pending()) {
      read.push({ profit: await this.pricing.profit(t, fees, baseFee), tracked: t });
    }
    const offered = await this.pricing.profit(tracked, fees, baseFee);
    // nothing awaited from here to the eviction: what's pending and sent stays as it's read
    const profits = read.filter((p) => p.tracked.status === "pending");
    if (profits.length < this.config.maxPending) return;
    let worst: { profit: Fraction; tracked: Tracked } | null = null;
    let worstEvictable: { profit: Fraction; tracked: Tracked } | null = null;
    for (const p of profits) {
      if (worst === null || p.profit.compare(worst.profit) < 0) worst = p;
      if (
        p.tracked.sent.length === 0 &&
        (worstEvictable === null || p.profit.compare(worstEvictable.profit) < 0)
      ) {
        worstEvictable = p;
      }
    }
    const bar = worstEvictable ?? worst;
    // a full queue holds one at least: max_pending is configured at least 1
    if (bar === null) throw new Error("making room in a queue that holds none");
    if (worstEvictable === null || offered.compare(bar.profit) <= 0) {
      const cost = basesCost(tracked.basis, fees);
      const wei = await this.prices.wei(tracked.submission.token);
      throw new NoCapacityError(
        tracked.submission.token,
        bar.profit.add(cost).div(wei).floor() + 1n,
      );
    }
    this.queue.finish(worstEvictable.tracked, "rejected", "outbid");
  }

  /** The latest and finalized blocks, read again only once the next block is due: a status's
   * confirmations, however many ask. */
  async finality(): Promise<Finality> {
    if (fresh(this.final)) return this.final.read;
    const finality = await this.chain.finality();
    this.final = recent(finality, finality.timestamp, this.chain);
    return finality;
  }

  /** Whether there's work for the next block: a submission pending, or funding in flight. */
  get busy(): boolean {
    return this.batches.fundingInFlight || this.queue.pending().length > 0;
  }

  /** The block, if there's a new one, and the batch, if it's due, both as of one read of the
   * latest block: when the next step is due. The head it last saw and when its batch is due are
   * stored, so they outlive the object. */
  async step(): Promise<Millis> {
    const latest = await this.measuring.latest();
    if (String(latest.number) !== this.store.get("head")) {
      await this.batches.onBlock(latest);
      this.store.put("head", String(latest.number));
    }
    if (Millis.unix() >= Number(this.store.get("batchAt") ?? 0)) {
      // due again a batch later whether this one succeeds or not: a failing batch isn't
      // retried every step
      const batch = Millis.ofSeconds(this.config.batchSeconds);
      this.store.put("batchAt", String(Millis.unix() + batch));
      await this.batches.closeBatch(latest);
    }
    const slot = this.chain.nextBlockAt(latest.timestamp);
    const batchAt = Millis(Number(this.store.get("batchAt")));
    const minStep = Millis.ofSeconds(this.config.minStepSeconds);
    return Millis(Math.max(Math.min(slot, batchAt), Millis.unix() + minStep));
  }
}

/** A submission or a sponsorship as planned at a block: how its payment is known, its plan, and
 * the measurements and block it was planned by. */
interface Planning {
  readonly proof: Proof;
  readonly plan: Plan;
  readonly measured: Measurements;
  readonly latest: Latest;
}

/** `taken`, as it's tracked from when it's planned: pending, nothing of it sent. */
function tracking(
  taken: Submission | Sponsorship,
  id: Hex,
  planned: Planning,
  userOperation: Hex | null,
  settlement: Hex | null,
): Tracked {
  return {
    id,
    submission: taken,
    basis: planBasis(planned.plan),
    status: "pending",
    sent: [],
    attempts: 0,
    transactionHashes: [],
    blockNumber: null,
    revertData: null,
    userOperation,
    settlement,
    proof: planned.proof,
  };
}
