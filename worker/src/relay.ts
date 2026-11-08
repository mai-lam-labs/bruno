/**
 * The relay on one chain: what it offers, its quotes, the submissions it takes; it holds its
 * queue, pricing, measuring and batches (batches.ts), each owning its own state, kept in a Store
 * so it outlives the object (docs/development/design/queue.md, price.md).
 */

import { type Builder } from "./builders.ts";
import { Batches } from "./batches.ts";
import { type Chain, type Finality, type Latest } from "./chain.ts";
import type { ChainConfig, Eip712Domain } from "./config.ts";
import { lastBlockBefore, type UnsignedTransfer } from "./eip3009.ts";
import { type Address, type Hex, hex, NATIVE_ASSET } from "./evm.ts";
import { Fraction } from "./fraction.ts";
import {
  BlockUnseenError,
  makePlan,
  planGasLimit,
  planSize,
  type Proof,
  simulate,
} from "./plan.ts";
import { type Prices } from "./prices.ts";
import {
  lengthField,
  type Payer,
  Pricing,
  type QuotedCall,
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
} from "./submission.ts";
import type { Store } from "./store.ts";
import { paying, Queue, type Tracked } from "./tracked.ts";
import { cost as txCost, type Fees, type Signer } from "./transactions.ts";
import { Millis, millisOf, Seconds } from "./units.ts";
import { message } from "./refusals.ts";

/** How long after a slot's start its block is looked for. */
export const SLOT_OFFSET = Seconds(1);
export class RelayError extends Error {}

/** What was last read of the chain, and when the next block, which may change it, is due (by
 * the wall clock, as block timestamps are). As bruno/relay.py's _Recent. */
interface Recent<T> {
  readonly read: T;
  readonly due: Millis;
}

/** `read` as of the block at `timestamp`: the next is due a slot and SLOT_OFFSET on. */
function recent<T>(read: T, timestamp: number, slot: Seconds): Recent<T> {
  return { read, due: millisOf(Seconds(timestamp + slot + SLOT_OFFSET)) };
}

function fresh<T>(r: Recent<T> | null): r is Recent<T> {
  return r !== null && Millis.unix() < r.due;
}

export class UnderpaidError extends RelayError {
  constructor(readonly token: Address, readonly required: bigint) {
    super(
      message("underpaid", { required, token }),
    );
  }
}

/** The queue is full of more profitable submissions: `required` would beat the least. */
export class NoCapacityError extends RelayError {
  constructor(readonly token: Address, readonly required: bigint) {
    super(
      message("no-capacity", { required, token }),
    );
  }
}

export interface Terms {
  readonly payee: Address;
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
    store: Store,
  ) {
    this.pricing = new Pricing(chain, config, prices, this.payee);
    this.domains = new Map(
      config.tokens.flatMap((t) =>
        t.eip3009 === null || t.address === NATIVE_ASSET ? [] : [[t.address, t.eip3009]]
      ),
    );
    this.queue = new Queue(store, Millis.ofSeconds(config.retentionSeconds));
    this.measuring = new Measuring(chain, config, store, this.domains);
    this.batches = new Batches(
      chain,
      config,
      floatAccount,
      builders,
      store,
      this.queue,
      this.pricing,
      this.measuring,
    );
  }

  /** The tokens taken over x402 as measured: each configured one whose transfer was. */
  x402Tokens(measured: Measurements): Address[] {
    return [...this.domains.keys()].filter((t) =>
      measured.tokens.get(t)?.transferGas !== undefined
    );
  }

  get payee(): Address {
    return this.config.payee;
  }

  /** How long an x402 payment's request may be held: its deadline's furthest. */
  get x402TimeoutSeconds(): number {
    return this.config.x402DeadlineBlocks * this.chain.slot;
  }

  terms(): Terms {
    return {
      payee: this.payee,
      tokens: this.config.tokens.map((t) => t.address),
      maxGas: this.config.maxGas,
      maxCallBytes: this.config.maxCallBytes,
    };
  }

  /** What `quoted` costs in each of `tokens`, its gas estimated if not given: enough for
   * `quoteBlocks` blocks of base-fee rise, the tip doubled, and our fee; paid by a transfer from
   * `payer`, if it names one, its deployment made first. */
  async quote(
    quoted: QuotedCall,
    tokens: readonly Address[],
    payer: Payer = {},
  ): Promise<Quotes> {
    this.check(quoted);
    const measured = await this.measuring.available();
    tokens.forEach((t) => this.pricing.supported(measured, t));
    const latest = await this.measuring.latest();
    const signing = await this.pricing.signing(measured, payer, payer.address !== undefined);
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
  async x402Quote(quoted: QuotedCall, payer: Payer = {}, existing = false): Promise<Quotes> {
    this.check(quoted);
    const measured = await this.measuring.available();
    const latest = await this.recentLatest();
    const tokens = this.x402Tokens(measured);
    const signing = await this.pricing.signing(measured, payer, true);
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

  /** The gas an x402 payment's call is given: its own, or its estimate, as its challenge priced
   * it, estimated again now it's paid. */
  async x402Gas(quoted: QuotedCall, payer: Payer): Promise<number> {
    this.check(quoted);
    const measured = await this.measuring.available();
    const signing = await this.pricing.signing(measured, payer, true);
    const tokens = this.x402Tokens(measured);
    return (await this.pricing.priced(measured, quoted, tokens, signing, this.batches.sender)).gas;
  }

  /** A quoted call's own checks, as a submission's: its length its call's, its authorizations
   * signed or their signer given, not both, and within the limits. */
  private check(quoted: QuotedCall): void {
    if (quoted.call !== null && quoted.call.data.length !== quoted.callDataLength) {
      throw new InvalidSubmissionError(
        "callDataLength",
        message("call-length-mismatch", {
          given: quoted.callDataLength,
          length: quoted.call.data.length,
        }),
      );
    }
    if (quoted.authority !== null && quoted.authorizations.length > 0) {
      throw new InvalidSubmissionError(
        "authority",
        message("authority-and-list"),
      );
    }
    checkAuthorizations(
      quoted.authorizations,
      this.chain.chainId,
      this.config.maxAuthorizations,
    );
    this.pricing.withinLimits(quoted.gas ?? 0, quoted.callDataLength, lengthField(quoted));
  }

  /** The last block an x402 payment valid after `validAfter` and before `validBefore` (Unix
   * seconds) can be included in: at most `x402DeadlineBlocks` ahead. */
  async x402Deadline(validAfter: bigint, validBefore: bigint): Promise<number> {
    const latest = await this.recentLatest();
    if (validAfter >= BigInt(latest.timestamp + this.chain.slot)) {
      throw new InvalidSubmissionError(
        "validAfter",
        message("valid-after"),
      );
    }
    const last = lastBlockBefore(latest, validBefore, this.chain.slot);
    if (last <= latest.number) {
      throw new InvalidSubmissionError("validBefore", message("valid-before"));
    }
    return Math.min(last, latest.number + this.config.x402DeadlineBlocks);
  }

  /** The latest block, read again only once the next is due: the x402 challenge is answered to
   * anyone who asks, probes included. */
  private async recentLatest(): Promise<Latest> {
    if (fresh(this.challenged)) return this.challenged.read;
    const latest = await this.measuring.latest();
    this.challenged = recent(latest, latest.timestamp, this.chain.slot);
    return latest;
  }

  /** Take `submission`, paying as `proof` has it, checked; or throw why not. `userOperation`:
   * the hash of the UserOp it sends, if it's one. */
  async submit(
    submission: Submission,
    proof: Proof,
    userOperation: Hex | null = null,
  ): Promise<Accepted> {
    this.pricing.withinLimits(submission.gas, submission.call.data.length);
    const measured = await this.measuring.available();
    this.pricing.supported(measured, submission.token);
    if (submission.payee !== this.payee) {
      throw new UnsupportedError(
        "wrong-payee",
        message("wrong-payee", { payee: this.payee, given: submission.payee }),
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
        throw new UnsupportedError(
          "no-eip3009",
          message("no-eip3009", { token: submission.token }),
        );
      }
      if (transfer.to !== submission.payee || transfer.value !== submission.amount) {
        throw new InvalidSubmissionError(
          "payment",
          message("transfer-not-payment"),
        );
      }
    }
    const id = hex(submissionId(submission));
    if (this.queue.known(id)) {
      return { id, isNew: false };
    }
    const latest = await this.measuring.latest();
    const head = latest.number;
    if (submission.maxBlockNumber <= head) {
      throw new InvalidSubmissionError("maxBlockNumber", message("deadline-passed"));
    }
    if (submission.maxBlockNumber > head + this.config.maxDeadlineBlocks) {
      const ahead = this.config.maxDeadlineBlocks;
      throw new InvalidSubmissionError(
        "maxBlockNumber",
        message("deadline-far", { ahead }),
        {
          most: BigInt(head + ahead),
        },
      );
    }
    const schedule = scheduleOf(measured);
    const sender = this.batches.sender;
    const plan = await makePlan(
      this.chain,
      submission,
      proof,
      schedule,
      measured.tokens,
      sender,
      measured.payeeHolds,
    );
    this.pricing.fits(plan);
    const fees = this.pricing.fees(latest);
    const required = await this.pricing.charge(measured, latest, submission.token, plan);
    const tracked: Tracked = {
      id,
      submission,
      gasLimit: planGasLimit(plan),
      size: planSize(plan),
      status: "pending",
      sent: [],
      attempts: 0,
      transactionHash: null,
      blockNumber: null,
      revertData: null,
      userOperation,
      proof,
    };
    if (paying(tracked, latest.nextBaseFee) < required) {
      throw new UnderpaidError(submission.token, required);
    }
    try {
      await simulate(this.chain, submission, plan, sender);
    } catch (e) {
      // nothing known of it yet: the client tries again
      if (e instanceof BlockUnseenError) throw new UnavailableError("block-unseen", e.message);
      throw e;
    }
    return await this.queue.queued(async () => {
      if (this.queue.known(id)) { // taken while this one was checked
        return { id, isNew: false };
      }
      const queue = this.queue.pending();
      if (queue.length >= this.config.maxPending) {
        await this.makeRoom(tracked, queue, fees, latest.nextBaseFee);
      }
      this.queue.add(tracked);
      await this.onTaken();
      return { id, isNew: true };
    });
  }

  /**
   * Evict the least profitable of `queue` for `tracked`, if `tracked` is more profitable; or
   * throw NoCapacityError with what would be taken. A submission whose transaction is out isn't
   * evicted: it may land.
   */
  private async makeRoom(
    tracked: Tracked,
    queue: readonly Tracked[],
    fees: Fees,
    baseFee: bigint,
  ): Promise<void> {
    let worst: { profit: Fraction; tracked: Tracked } | null = null;
    let worstEvictable: { profit: Fraction; tracked: Tracked } | null = null;
    for (const t of queue) {
      const profit = await this.pricing.profit(t, fees, baseFee);
      if (worst === null || profit.compare(worst.profit) < 0) worst = { profit, tracked: t };
      if (
        t.sent.length === 0 &&
        (worstEvictable === null || profit.compare(worstEvictable.profit) < 0)
      ) {
        worstEvictable = { profit, tracked: t };
      }
    }
    const bar = worstEvictable ?? worst;
    // a full queue holds one at least: max_pending is configured at least 1
    if (bar === null) throw new Error("making room in a queue that holds none");
    const profit = await this.pricing.profit(tracked, fees, baseFee);
    if (worstEvictable === null || profit.compare(bar.profit) <= 0) {
      const cost = txCost(fees, tracked.gasLimit, tracked.size);
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
    this.final = recent(finality, finality.timestamp, this.chain.slot);
    return finality;
  }

  /** Whether there's work for the next block: a submission pending, or funding in flight. */
  get busy(): boolean {
    return this.batches.fundingInFlight || this.queue.pending().length > 0;
  }
}
