/**
 * The relay on one chain: what it offers, its quotes, the submissions it takes, its batches and
 * each block's bundles (docs/development/design/queue.md, price.md). As bruno/relay.py's,
 * its state kept in a Store so it outlives the object.
 */

import { derive } from "./accounts.ts";
import { type Builder, sendBundle } from "./builders.ts";
import { bundles, signFunding, type TopUp } from "./bundle.ts";
import {
  type Chain,
  ChainError,
  type Finality,
  lastingFees,
  type Latest,
  type Receipt,
  receiptOf,
} from "./chain.ts";
import { JsonRpcError, resultOf } from "./jsonrpc.ts";
import type { ChainConfig, Eip712Domain } from "./config.ts";
import { DEPLOYER } from "./deployer.ts";
import { lastBlockBefore } from "./eip3009.ts";
import { type Address, bytes, type Hex, hex, NATIVE_ASSET } from "./evm.ts";
import { Fraction } from "./fraction.ts";
import {
  BlockUnseenError,
  makePlan,
  makePlans,
  type Plan,
  settled,
  simulate,
  SimulationFailedError,
  simulations,
} from "./plan.ts";
import {
  type AccountsStatus,
  monitor,
  statusFromJson,
  statusToJson,
  survey,
} from "./maintenance.ts";
import { PriceError, type Prices } from "./prices.ts";
import { feeCeiling, type Payer, Pricing, type Quotes, UnsupportedError } from "./pricing.ts";
import {
  holding,
  lookup,
  measure,
  type Measurements,
  problems,
  scheduleOf,
  UnavailableError,
} from "./measurements.ts";
import {
  checkAuthorizations,
  InvalidSubmissionError,
  type Submission,
  submissionId,
} from "./submission.ts";
import {
  finish,
  type Finished,
  finishedToJson,
  MAX_REVERT_BYTES,
  paying,
  type Rejection,
  SENT_KEPT,
  type Status,
  storedFromJson,
  type Tracked,
  trackedToJson,
} from "./tracked.ts";
import {
  authorizationJson,
  cost as txCost,
  type Fees,
  maxCost,
  sign,
  type SignedTransaction,
  type Signer,
  TransactionError,
} from "./transactions.ts";
import type { FeePayment } from "./userops.ts";

/** How long after a slot's start its block is looked for. */
const SLOT_OFFSET_SECONDS = 1;
/** How soon measurements that couldn't be taken again are tried again, the last kept. */
const MEASURE_RETRY_MS = 60_000;
/** How many of the last bugs of ours in a submission's work the root lists. */
export const INTERNAL_ERRORS_KEPT = 5;
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

export class UnknownSubmissionError extends RelayError {}

/** Why a chain is unavailable before its first measurement. */
const UNMEASURED = "not measured yet: its nodes didn't answer";

export interface Terms {
  readonly payee: Address;
  readonly tokens: readonly Address[];
  readonly maxGas: number;
  readonly maxCallBytes: number;
}

/** A batch's submissions on their accounts, and the accounts awaiting the funding. */
interface Batch {
  readonly slots: readonly { id: Hex; account: number }[];
  readonly awaiting: readonly number[];
}

/**
 * The float's funding transaction in flight, signed, and the accounts it tops up. It's stored
 * before it's sent, and its bytes sent again each block until it lands: an object replaced
 * between storing and sending loses nothing, and the float's nonce is never signed for twice.
 */
interface Funding {
  readonly hash: Hex;
  readonly raw: Hex;
  readonly accounts: readonly number[];
  readonly sentAt: number;
}

/** A tracked submission's transaction, as planned, from `sender`. */
interface Run {
  readonly tracked: Tracked;
  readonly plan: Plan;
  readonly sender: Address;
}

/** Where the relay keeps its state: synchronous, as a Durable Object's SQLite storage is. */
export interface Store {
  get(key: string): string | undefined;
  put(key: string, value: string): void;
  delete(key: string): void;
  list(prefix: string): string[];
}

export class Relay {
  /** The submissions pending, and those finished, kept `retentionSeconds` as Finished. */
  private readonly pendingById = new Map<Hex, Tracked>();
  private readonly finished = new Map<Hex, Finished>();
  /** The UserOps sent as submissions, pending or kept finished: each one's hash to its id,
   * rebuilt from what's stored. */
  private readonly operations = new Map<Hex, Hex>();
  private batch: Batch;
  private funding: Funding | null;
  /** Submissions are queued one at a time: a duplicate or a full queue is decided once. */
  private queueing: Promise<unknown> = Promise.resolve();
  /** What a call costs, and whether it fits. */
  readonly pricing: Pricing;
  /** The latest block as the x402 challenge last read it, and when the next is due (ms). */
  private challenged: { latest: Latest; due: number } | null = null;
  /** The latest and finalized blocks as a status last read them, and when the next is due (ms). */
  private final: { finality: Finality; due: number } | null = null;
  /** Each held request's submission: called once it's no longer pending. */
  private readonly outcomes = new Map<Hex, (() => void)[]>();
  /** Called when a submission is taken: the Durable Object arms its alarm, so the relay runs
   * while a request is held for it. */
  onTaken: () => Promise<void> = async () => {};
  /** The chain as last measured, and the measuring in flight, if any. */
  private measured: Measurements | null = null;
  /** The last bugs of ours a submission's work met, each its alone: listed with problems. */
  private internalErrors: string[] = [];
  private measuring: Promise<Measurements> | null = null;
  /** With nothing measured, when the nodes may be asked again after they didn't answer (ms). */
  private unmeasuredUntil = 0;
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
    this.pricing = new Pricing(chain, config, prices, this.payee);
    this.domains = new Map(
      config.tokens.flatMap((t) =>
        t.eip3009 === null || t.address === NATIVE_ASSET ? [] : [[t.address, t.eip3009]]
      ),
    );
    for (const value of store.list("submission:")) {
      const stored = storedFromJson(value);
      if ("submission" in stored) {
        this.pendingById.set(stored.id, stored);
      } else {
        this.finished.set(stored.id, stored);
      }
      if (stored.userOperation !== null) this.operations.set(stored.userOperation, stored.id);
    }
    this.batch = JSON.parse(store.get("batch") ?? '{"slots":[],"awaiting":[]}');
    this.funding = JSON.parse(store.get("funding") ?? "null");
    this.internalErrors = JSON.parse(store.get("internalErrors") ?? "[]");
  }

  /** The chain as measured: measured again once `measureSeconds` old. If the nodes don't
   * answer, the last are kept and tried again a minute later; with none, unavailable. What's
   * wrong is stored, for the root. */
  async measurements(): Promise<Measurements> {
    if (this.fresh(this.measured)) return this.measured;
    this.measuring ??= this.remeasure().finally(() => {
      this.measuring = null;
    });
    return await this.measuring;
  }

  /** Measured again: the last kept, tried again a minute later, if the nodes don't answer; with
   * none, unavailable, not tried again for a minute. Why stays in the log: a node's URL isn't a
   * client's. */
  private async remeasure(): Promise<Measurements> {
    if (this.measured === null && Date.now() < this.unmeasuredUntil) {
      throw new UnavailableError("not-measured", UNMEASURED);
    }
    let measured: Measurements;
    try {
      measured = await measure(this.chain, this.config, this.payee);
    } catch (e) {
      if (!(e instanceof ChainError || e instanceof JsonRpcError)) {
        // a bug of ours: reported, nothing measured, not asked again yet
        console.error(`chain ${this.chain.chainId}: measuring: internal error`, e);
        this.internalError("measuring", e);
        this.measured = null;
        this.unmeasuredUntil = Date.now() + MEASURE_RETRY_MS;
        throw new UnavailableError("not-measured", UNMEASURED);
      }
      console.warn(`chain ${this.chain.chainId}: measuring:`, e);
      if (this.measured === null) {
        this.unmeasuredUntil = Date.now() + MEASURE_RETRY_MS;
        throw new UnavailableError("not-measured", UNMEASURED);
      }
      const retryAt = this.config.measureSeconds * 1000 - MEASURE_RETRY_MS;
      measured = { ...this.measured, at: Date.now() - retryAt };
    }
    for (const problem of problems(measured)) {
      console.error(`chain ${this.chain.chainId}: ${problem}`);
    }
    this.store.put("problems", JSON.stringify(problems(measured)));
    this.measured = measured;
    return measured;
  }

  /** One submission's work failing, the rest going on: what it may meet logged; anything else a
   * bug of ours, logged and listed with the chain's problems, so it's seen. */
  private itemFailed(what: string, e: unknown): void {
    if (itemError(e)) {
      console.warn(`${what}:`, e.message);
      return;
    }
    console.error(`${what}: internal error`, e);
    this.internalError(what, e);
  }

  /** A bug of ours, logged where it was caught: listed with the chain's problems (the root reads
   * them stored, as "internalErrors"). */
  private internalError(what: string, e: unknown): void {
    const name = e instanceof Error ? e.name : typeof e;
    this.internalErrors = [...this.internalErrors, `internal error ${what}: ${name}`].slice(
      -INTERNAL_ERRORS_KEPT,
    );
    this.store.put("internalErrors", JSON.stringify(this.internalErrors));
  }

  private fresh(m: Measurements | null): m is Measurements {
    return m !== null && Date.now() - m.at < this.config.measureSeconds * 1000;
  }

  /** The measurements, if the chain can be served; else UnavailableError, why. */
  async available(): Promise<Measurements> {
    const measured = await this.measurements();
    if (measured.unavailable !== null) throw UnavailableError.of(measured.unavailable);
    return measured;
  }

  /** The tokens taken over x402 as measured: each configured one whose transfer was. */
  x402Tokens(measured: Measurements): Address[] {
    return [...this.domains.keys()].filter((t) =>
      measured.tokens.get(t)?.transferGas !== undefined
    );
  }

  /** The float and its accounts as last read, by a batch or the treasury: what the root
   * shows. */
  get accountsStatus(): AccountsStatus | null {
    const stored = this.store.get("accounts");
    return stored === undefined ? null : statusFromJson(stored);
  }

  /** `status` kept as the accounts' latest, unless one read at a later block is. */
  recordAccounts(status: AccountsStatus): void {
    const kept = this.accountsStatus;
    if (kept === null || status.block >= kept.block) {
      this.store.put("accounts", statusToJson(status));
    }
  }

  get payee(): Address {
    return this.config.payee;
  }

  /** What the payee holds of each x402 token, as the treasury last read it. */
  recordPayee(holdings: ReadonlyMap<Address, bigint>): void {
    if (this.measured === null) return;
    this.measured = holding(
      this.measured,
      new Map([...holdings].filter(([token]) => this.domains.has(token))),
    );
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
    const measured = await this.available();
    tokens.forEach((t) => this.pricing.supported(measured, t));
    const latest = await this.latest();
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
    const measured = await this.available();
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
    const latest = await this.latest();
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
    const measured = await this.available();
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
    if (this.known(id)) {
      return { id, isNew: false };
    }
    const latest = await this.latest();
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
    const sender = this.sender;
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
    return await this.queued(async () => {
      if (this.known(id)) { // taken while this one was checked
        return { id, isNew: false };
      }
      const queue = this.pending();
      if (queue.length >= this.config.maxPending) {
        await this.makeRoom(tracked, queue, fees, latest.nextBaseFee);
      }
      this.pendingById.set(id, tracked);
      if (userOperation !== null) this.operations.set(userOperation, id);
      this.save(tracked);
      await this.onTaken();
      return { id, isNew: true };
    });
  }

  /** `work` run once every earlier submission's has finished. */
  private queued<T>(work: () => Promise<T>): Promise<T> {
    const run = this.queueing.then(work, work);
    this.queueing = run.catch(() => undefined);
    return run;
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
      const profit = await this.profit(t, fees, baseFee);
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
    const profit = await this.profit(tracked, fees, baseFee);
    if (worstEvictable === null || profit.compare(bar.profit) <= 0) {
      const cost = txCost(fees, tracked.gasLimit, tracked.size);
      const wei = await this.prices.wei(tracked.submission.token);
      throw new NoCapacityError(
        tracked.submission.token,
        bar.profit.add(cost).div(wei).floor() + 1n,
      );
    }
    this.finish(worstEvictable.tracked, "rejected", "outbid");
  }

  /** Whether the submission `id` was taken: pending, or finished and still kept. */
  private known(id: Hex): boolean {
    return this.pendingById.has(id) || this.finished.has(id);
  }

  /** The submission `id`, pending or finished: UnknownSubmissionError if it's neither. */
  /** The id of the submission carrying the UserOp `opHash`, if the relay took it and hasn't
   * forgotten it. */
  operation(opHash: Hex): Hex | null {
    return this.operations.get(opHash) ?? null;
  }

  status(id: Hex): Tracked | Finished {
    const tracked = this.pendingById.get(id) ?? this.finished.get(id);
    if (tracked === undefined) {
      throw new UnknownSubmissionError(`no submission ${id}`);
    }
    return tracked;
  }

  /** The submission once it's no longer pending: included, rejected or expired. Waiting reads
   * nothing: it's the reconciling the relay does each block that ends it. */
  async outcome(id: Hex): Promise<Finished> {
    if ("submission" in this.status(id)) {
      await new Promise<void>((resolve) => {
        this.outcomes.set(id, [...(this.outcomes.get(id) ?? []), resolve]);
      });
    }
    const finished = this.finished.get(id);
    if (finished === undefined) throw new UnknownSubmissionError(`no submission ${id}`);
    return finished;
  }

  /** A pending x402 submission of the same call by the same payer (its token, call, gas and
   * authorizations), paid by another authorization: a client that timed out and signed again.
   * null if there's none. */
  pendingLike(submission: Submission): Tracked | null {
    const transfer = submission.transfer;
    if (transfer === undefined) return null;
    const key = (s: Submission): string =>
      JSON.stringify([
        s.token,
        s.call.to,
        hex(s.call.data),
        s.gas,
        s.authorizations.map(authorizationJson),
      ]);
    return this.pending().find((t) =>
      t.submission.transfer?.payer === transfer.payer && key(t.submission) === key(submission)
    ) ?? null;
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

  /**
   * Make the next batch, as of `latest`: rank the queue, take the top `accounts` that still
   * simulate and cover the next block's price, map them onto accounts, and send the float's
   * funding for those that need it (unless a funding transaction is in flight: they wait for
   * the next batch). Nothing queued, nothing read. The queue's reads, its simulations and the
   * accounts' state are each one batch.
   */
  async closeBatch(latest: Latest): Promise<void> {
    const pending = this.pending();
    if (pending.length === 0) {
      this.batch = { slots: [], awaiting: [] };
      this.saveState();
      return;
    }
    const measured = await this.measurements();
    if (measured.unavailable !== null) { // nothing sent: the queue waits, to its deadlines
      this.batch = { slots: [], awaiting: [] };
      this.saveState();
      return;
    }
    const schedule = scheduleOf(measured);
    const fees = this.pricing.fees(latest);
    const sender = this.sender;
    const plans = await makePlans(
      this.chain,
      pending.map((t) => t.submission),
      schedule,
      measured.tokens,
      sender,
      measured.payeeHolds,
    );
    const ranked: { profit: Fraction; tracked: Tracked; plan: Plan }[] = [];
    const covered = await this.covered(measured, latest, pending, (t) => t, plans, "ranking");
    for (const { item: tracked, plan } of covered) {
      try {
        const profit = await this.profit(tracked, fees, latest.nextBaseFee);
        ranked.push({ profit, tracked, plan });
      } catch (e) {
        this.itemFailed(`ranking ${tracked.id}`, e); // this one waits; the rest go on
      }
    }
    ranked.sort((a, b) => b.profit.compare(a.profit));
    // every ranked one simulated together: the best that still simulate are chosen
    const simulating = await this.stillSimulate(ranked.map((r) => ({ ...r, sender })));
    const chosen = ranked.filter((_, i) => simulating[i]).slice(0, this.config.accounts);
    if (chosen.length === 0) {
      this.batch = { slots: [], awaiting: [] };
      this.saveState();
      return;
    }
    const status = await survey(
      this.chain,
      this.floatAccount,
      this.config.accounts,
      latest.number,
      this.payee,
      payeeTokens(measured),
    );
    this.recordAccounts(status);
    const free = status.accounts.map(({ index, address, balance, nonce }) => ({
      index,
      address,
      balance,
      exists: balance > 0n || nonce > 0,
    }));
    const tip = this.pricing.tip(latest);
    const ceiling = feeCeiling(latest, this.config.quoteBlocks);
    const slots: { id: Hex; account: number }[] = [];
    const topUps: TopUp[] = [];
    const awaiting: number[] = [];
    for (const { tracked, plan } of chosen) {
      const cost = txCost(ceiling, plan.gasLimit, plan.size);
      let covering = free.find((a) => a.balance >= cost);
      if (covering === undefined) {
        if (this.funding !== null) continue; // one funding transaction at a time
        covering = free[0];
        if (covering === undefined) break;
        const amount = this.config.fundingMultiplier.mul(cost).ceil() - covering.balance;
        topUps.push({ account: covering.address, amount, newAccount: !covering.exists });
        awaiting.push(covering.index);
      }
      free.splice(free.indexOf(covering), 1);
      slots.push({ id: tracked.id, account: covering.index });
    }
    if (topUps.length > 0) {
      const nonce = status.floatNonce;
      const balance = status.floatBalance;
      // valid until it's given up on: its timeout's blocks
      const blocks = Math.ceil(this.config.fundingTimeoutSeconds / this.chain.slotSeconds);
      const fees = lastingFees(latest, tip, blocks);
      const tx = signFunding(this.floatAccount, this.chain.chainId, nonce, fees, topUps, schedule);
      if (balance < maxCost(tx)) {
        // not sent: a node would refuse it. The next batch tries again, the float perhaps
        // topped up by then
        console.warn(`funding: the float holds ${balance} wei, the funding ${maxCost(tx)}`);
      } else {
        this.funding = {
          hash: hex(tx.hash),
          raw: hex(tx.raw),
          accounts: awaiting,
          sentAt: Date.now(),
        };
      }
    }
    this.batch = { slots, awaiting };
    this.saveState();
    if (this.funding !== null && topUps.length > 0) {
      try {
        await this.chain.sendRaw(bytes(this.funding.raw));
      } catch (e) {
        if (!(e instanceof JsonRpcError)) throw e;
        // refused, so not in flight: the next batch tries again
        console.warn(`funding ${this.funding.hash} refused:`, e.message);
        this.funding = null;
        this.saveState();
      }
    }
  }

  /**
   * `latest` is the latest block: reconcile, then send the batch's bundles for the next. Each
   * slot's transaction is signed if it covers the block's price, still simulates, and its
   * account covers it; the receipts, the slots' reads, their simulations and their accounts'
   * state are each one batch.
   */
  async onBlock(latest: Latest): Promise<void> {
    const target = latest.number + 1;
    this.prune();
    await this.reconcile(target);
    const slots = this.batch.slots.flatMap((slot) => {
      const tracked = this.pendingById.get(slot.id);
      if (
        tracked === undefined || tracked.status !== "pending" ||
        this.batch.awaiting.includes(slot.account)
      ) {
        return [];
      }
      return [{ tracked, account: slot.account, signer: derive(this.floatAccount, slot.account) }];
    });
    if (slots.length === 0) return; // nothing to send: nothing more read
    const measured = await this.measurements();
    if (measured.unavailable !== null) return; // reconciled, but nothing sent
    const schedule = scheduleOf(measured);
    const fees = this.pricing.fees(latest);
    const plans = await makePlans(
      this.chain,
      slots.map((s) => s.tracked.submission),
      schedule,
      measured.tokens,
      this.sender,
      measured.payeeHolds,
    );
    const covered =
      (await this.covered(measured, latest, slots, (s) => s.tracked, plans, "signing"))
        .map(({ item: { tracked, account, signer }, plan }) => ({
          tracked,
          account,
          signer,
          plan,
          sender: signer.address,
        }));
    const simulating = await this.stillSimulate(covered);
    const ready = covered.filter((_, i) => simulating[i]);
    const states = await monitor(this.chain, this.floatAccount, ready.map((r) => r.account));
    const txs: SignedTransaction[] = [];
    for (const [i, { tracked, signer, plan }] of ready.entries()) {
      try {
        const state = states[i];
        if (state === undefined) continue;
        const tx = sign(
          signer,
          this.chain.chainId,
          state.nonce,
          fees,
          plan.gasLimit,
          DEPLOYER,
          plan.runner.initcode,
          0n,
          tracked.submission.authorizations,
        );
        if (state.balance < maxCost(tx)) continue;
        tracked.sent = [...tracked.sent, hex(tx.hash)].slice(-SENT_KEPT);
        tracked.attempts += 1;
        this.save(tracked);
        txs.push(tx);
      } catch (e) {
        this.itemFailed(`signing ${tracked.id}`, e);
      }
    }
    if (txs.length === 0) return;
    const sends = this.builders.flatMap((builder) =>
      bundles(builder.mode, target, null, txs).map((params) => ({ builder, params }))
    );
    const results = await Promise.allSettled(
      sends.map(({ builder, params }) => sendBundle(builder, params)),
    );
    results.forEach((result, i) => {
      if (result.status === "rejected") {
        console.warn(`block ${target}: ${sends[i]?.builder.url} refused a bundle:`, result.reason);
      }
    });
  }

  /** Whether there's work for the next block: a submission pending, or funding in flight. */
  get busy(): boolean {
    return this.funding !== null || this.pending().length > 0;
  }

  /** When the next finished submission is due to be forgotten: null if none is. */
  forgetAt(): number | null {
    let first: number | null = null;
    for (const f of this.finished.values()) {
      if (first === null || f.finishedAt < first) first = f.finishedAt;
    }
    return first === null ? null : first + this.config.retentionSeconds * 1000;
  }

  /** Forget the submissions finished more than `retentionSeconds` ago. */
  prune(): void {
    const cutoff = Date.now() - this.config.retentionSeconds * 1000;
    for (const [id, f] of this.finished) {
      if (f.finishedAt <= cutoff) {
        this.finished.delete(id);
        this.store.delete(`submission:${id}`);
        if (f.userOperation !== null) this.operations.delete(f.userOperation);
      }
    }
  }

  private pending(): Tracked[] {
    return [...this.pendingById.values()];
  }

  private save(tracked: Tracked): void {
    this.store.put(`submission:${tracked.id}`, trackedToJson(tracked));
  }

  /** `tracked` finished, as `status`, and stored as only its status needs: a request held for
   * it is answered. */
  private finish(tracked: Tracked, status: Status, reason: Rejection | null = null): void {
    const finished = finish(tracked, status, reason);
    this.pendingById.delete(tracked.id);
    this.finished.set(tracked.id, finished);
    this.store.put(`submission:${tracked.id}`, finishedToJson(finished));
    for (const resolve of this.outcomes.get(tracked.id) ?? []) resolve();
    this.outcomes.delete(tracked.id);
  }

  private saveState(): void {
    this.store.put("batch", JSON.stringify(this.batch));
    this.store.put("funding", JSON.stringify(this.funding));
  }

  /**
   * Whether each pending submission's latest transactions landed, and the funding in flight:
   * every receipt looked for in one batch. Each transaction is looked for again until it's
   * SENT_KEPT sends old: a node without the block yet has no receipt. A submission past its
   * deadline, unlanded, expires.
   */
  private async reconcile(target: number): Promise<void> {
    const pending = this.pending();
    const funding = this.funding;
    const hashes = [...pending.flatMap((t) => t.sent), ...(funding === null ? [] : [funding.hash])];
    const answers = await this.chain.batch(
      hashes.map((hash) => ({ method: "eth_getTransactionReceipt", params: [hash] })),
    );
    const receipts = new Map(hashes.map((hash, i) => [hash, answers[i]]));
    const receipt = (hash: Hex): Receipt | null => {
      const answer = receipts.get(hash);
      if (answer === undefined) throw new ChainError(`no receipt answer for ${hash}`);
      return receiptOf(resultOf(answer));
    };
    for (const tracked of pending) {
      try {
        this.reconcileOne(tracked, target, receipt);
      } catch (e) {
        this.itemFailed(`reconciling ${tracked.id}`, e);
      }
    }
    if (funding !== null) {
      await this.reconcileFunding(funding, receipt(funding.hash));
    }
  }

  private reconcileOne(
    tracked: Tracked,
    target: number,
    receipt: (hash: Hex) => Receipt | null,
  ): void {
    for (const sent of tracked.sent) {
      const landed = receipt(sent);
      if (landed !== null) {
        tracked.transactionHash = sent;
        tracked.blockNumber = landed.blockNumber;
        if (landed.status === 1) {
          this.finish(tracked, "included");
        } else {
          this.finish(tracked, "rejected", "reverted on-chain");
        }
        return;
      }
    }
    if (target > tracked.submission.maxBlockNumber) {
      this.finish(tracked, "expired");
    }
  }

  /** Whether the funding in flight landed (its receipt, or null): its accounts are then ready. */
  private async reconcileFunding(funding: Funding, receipt: Receipt | null): Promise<void> {
    if (receipt !== null) {
      this.funding = null;
      if (receipt.status === 1) {
        const awaiting = this.batch.awaiting.filter((a) => !funding.accounts.includes(a));
        this.batch = { slots: this.batch.slots, awaiting };
      } else {
        console.error(`funding ${funding.hash} reverted`);
      }
    } else if (Date.now() - funding.sentAt > this.config.fundingTimeoutSeconds * 1000) {
      this.funding = null;
      console.error(`funding ${funding.hash} not seen in time`);
    } else {
      // the same bytes again: a node that has them already says so, which is no failure
      try {
        await this.chain.sendRaw(bytes(funding.raw));
      } catch (e) {
        if (!(e instanceof JsonRpcError)) throw e;
      }
    }
    this.saveState();
  }

  /**
   * Which of `runs` still simulate, in one batch; those that don't are rejected, with their
   * revert data. One with a transaction out only sits out: it may have landed, which its
   * receipt tells, and a landed call no longer simulates. So does one whose node hadn't the
   * block: nothing is known of it until the next.
   */
  private async stillSimulate(runs: readonly Run[]): Promise<boolean[]> {
    const reverts = await simulations(
      this.chain,
      runs.map((r) => ({ submission: r.tracked.submission, plan: r.plan, sender: r.sender })),
    );
    return runs.map(({ tracked }, i) => {
      const revert = reverts[i];
      if (revert === null || revert === undefined) return true;
      if (revert instanceof BlockUnseenError || revert instanceof ChainError) {
        console.warn(`simulating ${tracked.id}: ${revert.message}`); // it sits out, unknown yet
        return false;
      }
      if (tracked.sent.length > 0) return false;
      tracked.revertData = hex(revert.slice(0, MAX_REVERT_BYTES));
      this.finish(tracked, "rejected", "simulation failed");
      return false;
    });
  }

  /**
   * Of `items`, each a submission tracked (`trackedOf`) and planned as `plans`, those whose amount
   * covers their charge in the block after `latest`, with their plans; each kept its plan's gas
   * limit and size. One whose plan
   * failed simulation is rejected (plannedOrFailed); one that can't be planned or priced sits
   * out, logged as `doing`, and the rest go on.
   */
  private async covered<T>(
    measured: Measurements,
    latest: Latest,
    items: readonly T[],
    trackedOf: (item: T) => Tracked,
    plans: readonly PromiseSettledResult<Plan>[],
    doing: string,
  ): Promise<{ item: T; plan: Plan }[]> {
    const covered: { item: T; plan: Plan }[] = [];
    for (const [index, item] of items.entries()) {
      const one = trackedOf(item);
      try {
        const plan = this.plannedOrFailed(one, plans[index]);
        if (plan === null) continue;
        one.gasLimit = plan.gasLimit;
        one.size = plan.size;
        const required = await this.pricing.charge(measured, latest, one.submission.token, plan);
        if (paying(one, latest.nextBaseFee) >= required) covered.push({ item, plan });
      } catch (e) {
        this.itemFailed(`${doing} ${one.id}`, e);
      }
    }
    return covered;
  }

  /** The float's account 0: what a submission's transaction is planned and simulated as sent
   * from. */
  private get sender(): Address {
    return derive(this.floatAccount, 0).address;
  }

  /**
   * `tracked`'s plan; or, its estimate having reverted, null, it rejected with that revert data
   * as a simulation that fails is (unless its transaction is out, when it only sits out: it may
   * have landed). Why else there's no plan, thrown.
   */
  private plannedOrFailed(
    tracked: Tracked,
    result: PromiseSettledResult<Plan> | undefined,
  ): Plan | null {
    if (result?.status === "rejected" && result.reason instanceof SimulationFailedError) {
      if (tracked.sent.length === 0) {
        tracked.revertData = hex(result.reason.revertData.slice(0, MAX_REVERT_BYTES));
        this.finish(tracked, "rejected", "simulation failed");
      }
      return null;
    }
    return settled(result);
  }

  /** What the submission pays in a block of `baseFee`, in wei, less its transaction's cost at
   * `fees`. */
  private async profit(tracked: Tracked, fees: Fees, baseFee: bigint): Promise<Fraction> {
    const wei = await this.prices.wei(tracked.submission.token);
    return wei.mul(paying(tracked, baseFee)).sub(txCost(fees, tracked.gasLimit, tracked.size));
  }

  /** The latest block, with what's paid beyond gas on an OP-stack chain: measured first (from
   * the last measurements, read again only once they're old), so it's known which. */
  async latest(): Promise<Latest> {
    let opStack = false;
    try {
      opStack = (await this.measurements()).opStack;
    } catch (e) {
      if (!(e instanceof UnavailableError)) throw e; // nothing priced or sent until measured
    }
    return await this.chain.latest(opStack);
  }
}

/** Each measured token but ETH, and its decimals and symbol: what the payee's holdings are read
 * of. */
export function payeeTokens(
  measured: Measurements,
): Map<Address, { decimals: number; symbol: string }> {
  return new Map(
    [...measured.tokens.keys()].filter((t) => t !== NATIVE_ASSET).map((t) => [
      t,
      {
        decimals: lookup(measured.decimals, t, "a token's decimals"),
        symbol: lookup(measured.symbols, t, "a token's symbol"),
      },
    ]),
  );
}

/** One submission's work failed: logged, and the others' go on. What isn't expected is thrown. */
/** Whether `e` is what one submission's work may fail with, without failing the others' in its
 * batch or block: a node's failure, the client's mistake, a price or transaction refused
 * (bruno/relay.py's _ITEM_ERRORS). */
function itemError(e: unknown): e is Error {
  return e instanceof RelayError || e instanceof UnsupportedError ||
    e instanceof UnavailableError || e instanceof InvalidSubmissionError ||
    e instanceof SimulationFailedError || e instanceof ChainError || e instanceof JsonRpcError ||
    e instanceof PriceError || e instanceof TransactionError;
}
