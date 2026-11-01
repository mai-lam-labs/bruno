/**
 * The relay on one chain: what it offers, its quotes, the submissions it takes; it holds its
 * queue, pricing, measuring and batches (batches.ts), each owning its own state, kept in a Store
 * so it outlives the object (docs/development/design/queue.md, price.md).
 */

import { type Builder } from "./builders.ts";
import { Batches } from "./batches.ts";
import { type Chain, type Finality, type Latest } from "./chain.ts";
import type { ChainConfig, Eip712Domain } from "./config.ts";
import { lastBlockBefore } from "./eip3009.ts";
import { type Address, type Hex, hex, NATIVE_ASSET } from "./evm.ts";
import { Fraction } from "./fraction.ts";
import { BlockUnseenError, makePlan, simulate } from "./plan.ts";
import { type Prices } from "./prices.ts";
import { type Payer, Pricing, type Quotes, UnsupportedError } from "./pricing.ts";
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
import type { FeePayment } from "./userops.ts";

/** How long after a slot's start its block is looked for. */
const SLOT_OFFSET_SECONDS = 1;
export class RelayError extends Error {}

export class UnderpaidError extends RelayError {
  constructor(readonly token: Address, readonly required: bigint) {
    super(`underpaid: ${required} of ${token} is required`);
  }
}

/** The queue is full of more profitable submissions: `required` would beat the least. */
export class NoCapacityError extends RelayError {
  constructor(readonly token: Address, readonly required: bigint) {
    super(`no capacity: ${required} of ${token} would be taken`);
  }
}

export interface Terms {
  readonly payee: Address;
  readonly tokens: readonly Address[];
  readonly maxGas: number;
  readonly maxCallBytes: number;
}

export class Relay {
  /** The submissions taken, pending and finished. */
  readonly queue: Queue;
  /** What a call costs, and whether it fits. */
  readonly pricing: Pricing;
  /** The latest block as the x402 challenge last read it, and when the next is due (ms). */
  private challenged: { latest: Latest; due: number } | null = null;
  /** The latest and finalized blocks as a status last read them, and when the next is due (ms). */
  private final: { finality: Finality; due: number } | null = null;
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
    this.queue = new Queue(store, config.retentionSeconds);
    this.measuring = new Measuring(chain, config, store, new Set(this.domains.keys()));
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
    return this.config.x402DeadlineBlocks * this.chain.slotSeconds;
  }

  terms(): Terms {
    return {
      payee: this.payee,
      tokens: this.config.tokens.map((t) => t.address),
      maxGas: this.config.maxGas,
      maxCallBytes: this.config.maxCallBytes,
    };
  }

  /** What a call costs in each of `tokens`: enough for `quoteBlocks` blocks of base-fee rise,
   * the tip doubled, and our fee; paid by a transfer from `payer`, if it names one, its
   * deployment made first. */
  async quote(
    gas: number,
    callBytes: number,
    authorityAddress: Address | null,
    tokens: readonly Address[],
    payer: Payer = {},
  ): Promise<Quotes> {
    this.pricing.withinLimits(gas, callBytes);
    const measured = await this.measuring.available();
    tokens.forEach((t) => this.pricing.supported(measured, t));
    const latest = await this.measuring.latest();
    const signing = await this.pricing.signing(measured, payer, payer.address !== undefined);
    return await this.pricing.quoteAt(
      measured,
      latest,
      gas,
      callBytes,
      authorityAddress,
      tokens,
      signing,
      false,
    );
  }

  /** `quote` in each token accepted over x402, the runner making its transfer: as of the latest
   * block, read at most once a slot however many ask. `existing`: the authority is taken to
   * exist, unread. */
  async x402Quote(
    gas: number,
    callBytes: number,
    authorityAddress: Address | null,
    payer: Payer = {},
    existing = false,
  ): Promise<Quotes> {
    this.pricing.withinLimits(gas, callBytes);
    const measured = await this.measuring.available();
    const latest = await this.recentLatest();
    const tokens = this.x402Tokens(measured);
    const signing = await this.pricing.signing(measured, payer, true);
    return await this.pricing.quoteAt(
      measured,
      latest,
      gas,
      callBytes,
      authorityAddress,
      tokens,
      signing,
      existing,
    );
  }

  /** The last block an x402 payment valid after `validAfter` and before `validBefore` (Unix
   * seconds) can be included in: at most `x402DeadlineBlocks` ahead. */
  async x402Deadline(validAfter: bigint, validBefore: bigint): Promise<number> {
    const latest = await this.recentLatest();
    if (validAfter >= BigInt(latest.timestamp + this.chain.slotSeconds)) {
      throw new InvalidSubmissionError("validAfter", "not valid by the next block");
    }
    const last = lastBlockBefore(latest, validBefore, this.chain.slotSeconds);
    if (last <= latest.number) {
      throw new InvalidSubmissionError("validBefore", "passed");
    }
    return Math.min(last, latest.number + this.config.x402DeadlineBlocks);
  }

  /** The latest block, read again only once the next is due: the x402 challenge is answered to
   * anyone who asks, probes included. */
  private async recentLatest(): Promise<Latest> {
    if (this.challenged !== null && Date.now() < this.challenged.due) {
      return this.challenged.latest;
    }
    const latest = await this.measuring.latest();
    const due = (latest.timestamp + this.chain.slotSeconds + SLOT_OFFSET_SECONDS) * 1000;
    this.challenged = { latest, due };
    return latest;
  }

  /** Take `submission`, checked; or throw why not. `userOperation`: the hash of the UserOp it
   * sends, if it's one; `feePayment`, what it pays if it pays by its fees. */
  async submit(
    submission: Submission,
    userOperation: Hex | null = null,
    feePayment: FeePayment | null = null,
  ): Promise<{ id: Hex; isNew: boolean }> {
    this.pricing.withinLimits(submission.gas, submission.call.data.length);
    const measured = await this.measuring.available();
    this.pricing.supported(measured, submission.token);
    if (submission.payee !== this.payee) {
      throw new UnsupportedError(
        "wrong-payee",
        `the payee is ${this.payee}, not ${submission.payee}`,
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
        throw new UnsupportedError("no-eip3009", `${submission.token} takes no EIP-3009 transfer`);
      }
      if (transfer.to !== submission.payee || transfer.value !== submission.amount) {
        throw new InvalidSubmissionError("payment", "the transfer isn't the payment");
      }
    }
    const id = hex(submissionId(submission));
    if (this.queue.known(id)) {
      return { id, isNew: false };
    }
    const latest = await this.measuring.latest();
    const head = latest.number;
    if (submission.maxBlockNumber <= head) {
      throw new InvalidSubmissionError("maxBlockNumber", "passed");
    }
    if (submission.maxBlockNumber > head + this.config.maxDeadlineBlocks) {
      const ahead = this.config.maxDeadlineBlocks;
      throw new InvalidSubmissionError("maxBlockNumber", `over ${ahead} blocks ahead`, {
        most: BigInt(head + ahead),
      });
    }
    const schedule = scheduleOf(measured);
    const sender = this.batches.sender;
    const plan = await makePlan(
      this.chain,
      submission,
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
      gasLimit: plan.gasLimit,
      size: plan.size,
      status: "pending",
      sent: [],
      attempts: 0,
      transactionHash: null,
      blockNumber: null,
      revertData: null,
      userOperation,
      feePayment,
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
    if (this.final !== null && Date.now() < this.final.due) return this.final.finality;
    const finality = await this.chain.finality();
    const due = (finality.timestamp + this.chain.slotSeconds + SLOT_OFFSET_SECONDS) * 1000;
    this.final = { finality, due };
    return finality;
  }

  /** Whether there's work for the next block: a submission pending, or funding in flight. */
  get busy(): boolean {
    return this.batches.fundingInFlight || this.queue.pending().length > 0;
  }
}
