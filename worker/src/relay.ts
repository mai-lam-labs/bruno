/**
 * The relay on one chain: what it offers, its quotes, the submissions it takes, its batches and
 * each block's bundles (DESIGN.md, Queue and batches; Funding; Price). A port of the Python
 * reference (bruno/relay.py), its state kept in a Store so it outlives the object.
 */

import { derive } from "./accounts.ts";
import { type Builder, sendBundle } from "./builders.ts";
import { bundles, signFunding, type TopUp } from "./bundle.ts";
import {
  type Answer,
  type Call,
  type Chain,
  ChainError,
  hexOf,
  JsonRpcError,
  lastingFees,
  type Latest,
  quantity,
  type Receipt,
  receiptOf,
  resultOf,
} from "./chain.ts";
import type { ChainConfig } from "./config.ts";
import { DEPLOYER, transactionGas } from "./deployer.ts";
import { type Address, bytes, checksum, type EncodedCall, type Hex, hex } from "./evm.ts";
import { type GasSchedule, intrinsic } from "./forks.ts";
import { Fraction } from "./fraction.ts";
import { PriceError, type Prices } from "./prices.ts";
import { type Erc20, frameGas, inCallRunner, type Runner, RunnerError } from "./runner.ts";
import { type Submission, submissionId } from "./submission.ts";
import {
  authority,
  type Fees,
  maxCost,
  sign,
  type SignedAuthorization,
  type SignedTransaction,
  type Signer,
  TransactionError,
} from "./transactions.ts";

/** EIP-1559: the base fee rises by at most an eighth a block. */
const MAX_BASE_FEE_RISE = new Fraction(9n, 8n);
/** The largest amount: a quote's runner pushes it, so its initcode is never longer. */
const MAX_AMOUNT = (1n << 256n) - 1n;
const DELEGATION_PREFIX = "0xef0100";
/** How many of a submission's latest transactions are looked for: past a node lagging a block. */
const SENT_KEPT = 4;
/** The most of a failed simulation's revert data kept with a rejected submission. */
const MAX_REVERT_BYTES = 1_024;
/** secp256k1's order, and EIP-7702's bounds: s at most half of it, the nonce under 2**64 - 1. */
const SECP256K1_N = 0xfffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141n;
const MAX_AUTHORIZATION_NONCE = (1n << 64n) - 1n;

export class RelayError extends Error {}

export class InvalidSubmissionError extends RelayError {
  constructor(readonly field: string, message: string) {
    super(`${field}: ${message}`);
  }
}

export class UnsupportedError extends RelayError {}

export class UnderpaidError extends RelayError {
  constructor(readonly token: Address, readonly required: bigint) {
    super(`underpaid: ${required} of ${token} is required`);
  }
}

export class SimulationFailedError extends RelayError {
  constructor(readonly revertData: Uint8Array) {
    super("simulation failed");
  }
}

/** The queue is full of more profitable submissions: `required` would beat the least. */
export class NoCapacityError extends RelayError {
  constructor(readonly token: Address, readonly required: bigint) {
    super(`no capacity: ${required} of ${token} would be taken`);
  }
}

export class UnknownSubmissionError extends RelayError {}

export type Status = "pending" | "included" | "expired" | "rejected";

/** A submission taken, and what's become of it. */
export interface Tracked {
  readonly id: Hex;
  readonly submission: Submission;
  gasLimit: number;
  status: Status;
  /** Its latest transactions, each looked for until it's SENT_KEPT sends old. */
  sent: Hex[];
  attempts: number;
  transactionHash: Hex | null;
  blockNumber: number | null;
  reason: string | null;
  revertData: Hex | null;
  /** When it stopped being pending (Date.now()): it's forgotten `retentionSeconds` after. */
  finishedAt: number | null;
}

function finish(tracked: Tracked, status: Status, reason: string | null = null): void {
  tracked.status = status;
  tracked.reason = reason;
  tracked.finishedAt = Date.now();
}

export interface Terms {
  readonly payee: Address;
  readonly tokens: readonly Address[];
  readonly maxGas: number;
  readonly maxCallBytes: number;
}

export interface Quotes {
  readonly validUntilBlock: number;
  readonly quotes: readonly { token: Address; amount: bigint }[];
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

interface Plan {
  readonly runner: Runner;
  readonly gasLimit: number;
}

/** A submission's transaction, as planned, from `sender`. */
interface Simulation {
  readonly submission: Submission;
  readonly plan: Plan;
  readonly sender: Address;
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
  private readonly tracked = new Map<Hex, Tracked>();
  private batch: Batch;
  private funding: Funding | null;
  /** Submissions are queued one at a time: a duplicate or a full queue is decided once. */
  private queueing: Promise<unknown> = Promise.resolve();
  private readonly margins: Map<Address, Fraction>;

  constructor(
    readonly chain: Chain,
    private readonly config: ChainConfig,
    private readonly float: Signer,
    private readonly tokens: ReadonlyMap<Address, Erc20 | null>,
    private readonly prices: Prices,
    private readonly builders: readonly Builder[],
    private readonly store: Store,
  ) {
    this.margins = new Map(
      config.tokens.map((t) => [t.address, config.marginMultiplier.mul(t.marginMultiplier)]),
    );
    for (const value of store.list("submission:")) {
      const tracked = trackedFromJson(value);
      this.tracked.set(tracked.id, tracked);
    }
    this.batch = JSON.parse(store.get("batch") ?? '{"slots":[],"awaiting":[]}');
    this.funding = JSON.parse(store.get("funding") ?? "null");
  }

  get payee(): Address {
    return this.config.payee;
  }

  terms(): Terms {
    return {
      payee: this.payee,
      tokens: [...this.tokens.keys()],
      maxGas: this.config.maxGas,
      maxCallBytes: this.config.maxCallBytes,
    };
  }

  /** What a call costs in each of `tokens`: enough for `quoteBlocks` blocks of base-fee rise. */
  async quote(
    gas: number,
    callBytes: number,
    authorityAddress: Address | null,
    tokens: readonly Address[],
  ): Promise<Quotes> {
    this.withinLimits(gas, callBytes);
    tokens.forEach((t) => this.supported(t));
    const latest = await this.chain.latest();
    const schedule = this.chain.scheduleAfter(latest);
    const target = authorityAddress ?? this.payee;
    const call: EncodedCall = {
      to: target,
      data: new Uint8Array(callBytes).fill(0xff),
      value: 0n,
    };
    const authorities = authorityAddress === null ? [] : [authorityAddress];
    const empty = await this.empty(authorities);
    const maxFee = this.maxFee(latest, this.config.quoteBlocks - 1);
    const quotes = [];
    for (const token of tokens) {
      const payment = { token: this.erc20(token), payee: this.payee, amount: MAX_AMOUNT };
      const runner = inCallRunner(call, payment, schedule, new Set(authorities));
      const limit = transactionGas(
        runner.initcode,
        frameGas(runner, gas),
        schedule,
        authorities.length,
        empty,
      );
      quotes.push({ token, amount: await this.price(token, BigInt(limit) * maxFee) });
    }
    return { validUntilBlock: latest.number + this.config.quoteBlocks, quotes };
  }

  /** Take `submission`, checked; or throw why not. Its id, and whether it's new. */
  async submit(submission: Submission): Promise<{ id: Hex; isNew: boolean }> {
    this.withinLimits(submission.gas, submission.call.data.length);
    this.supported(submission.token);
    if (submission.payee !== this.payee) {
      throw new UnsupportedError(`the payee is ${this.payee}, not ${submission.payee}`);
    }
    this.validAuthorizations(submission.authorizations);
    const id = hex(submissionId(submission));
    if (this.tracked.has(id)) {
      return { id, isNew: false };
    }
    const latest = await this.chain.latest();
    const head = latest.number;
    if (submission.maxBlockNumber <= head) {
      throw new InvalidSubmissionError("maxBlockNumber", "passed");
    }
    if (submission.maxBlockNumber > head + this.config.maxDeadlineBlocks) {
      throw new InvalidSubmissionError(
        "maxBlockNumber",
        `over ${this.config.maxDeadlineBlocks} blocks ahead`,
      );
    }
    const schedule = this.chain.scheduleAfter(latest);
    const plan = await this.plan(submission, schedule);
    const fee = this.fees(latest).maxFeePerGas;
    const required = await this.price(submission.token, BigInt(plan.gasLimit) * fee);
    if (submission.amount < required) {
      throw new UnderpaidError(submission.token, required);
    }
    await this.simulate(submission, plan, derive(this.float, 0).address);
    const tracked: Tracked = {
      id,
      submission,
      gasLimit: plan.gasLimit,
      status: "pending",
      sent: [],
      attempts: 0,
      transactionHash: null,
      blockNumber: null,
      reason: null,
      revertData: null,
      finishedAt: null,
    };
    return await this.queued(async () => {
      if (this.tracked.has(id)) { // taken while this one was checked
        return { id, isNew: false };
      }
      const queue = this.pending();
      if (queue.length >= this.config.maxPending) {
        await this.makeRoom(tracked, queue, fee);
      }
      this.tracked.set(id, tracked);
      this.save(tracked);
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
  private async makeRoom(tracked: Tracked, queue: readonly Tracked[], fee: bigint): Promise<void> {
    let worst: { profit: Fraction; tracked: Tracked } | null = null;
    let worstEvictable: { profit: Fraction; tracked: Tracked } | null = null;
    for (const t of queue) {
      const profit = await this.profit(t, fee);
      if (worst === null || profit.compare(worst.profit) < 0) worst = { profit, tracked: t };
      if (
        t.sent.length === 0 &&
        (worstEvictable === null || profit.compare(worstEvictable.profit) < 0)
      ) {
        worstEvictable = { profit, tracked: t };
      }
    }
    const bar = worstEvictable ?? worst;
    if (bar === null) return;
    if (worstEvictable === null || (await this.profit(tracked, fee)).compare(bar.profit) <= 0) {
      const cost = BigInt(tracked.gasLimit) * fee;
      const wei = await this.prices.wei(tracked.submission.token);
      throw new NoCapacityError(
        tracked.submission.token,
        bar.profit.add(cost).div(wei).floor() + 1n,
      );
    }
    finish(worstEvictable.tracked, "rejected", "outbid");
    this.save(worstEvictable.tracked);
  }

  /**
   * At most `maxAuthorizations`, each one EIP-7702 would take: its chain id this chain's or 0,
   * yParity 0 or 1, r and s in range with s low, its nonce under 2**64 - 1.
   */
  private validAuthorizations(authorizations: readonly SignedAuthorization[]): void {
    if (authorizations.length > this.config.maxAuthorizations) {
      throw new InvalidSubmissionError(
        "authorizationList",
        `over ${this.config.maxAuthorizations}`,
      );
    }
    for (const a of authorizations) {
      if (a.chainId !== 0n && a.chainId !== BigInt(this.chain.chainId)) {
        throw new InvalidSubmissionError("authorizationList", `chain id ${a.chainId}`);
      }
      if (a.yParity !== 0 && a.yParity !== 1) {
        throw new InvalidSubmissionError("authorizationList", "yParity is 0 or 1");
      }
      if (!(a.r > 0n && a.r < SECP256K1_N && a.s > 0n && a.s <= SECP256K1_N / 2n)) {
        throw new InvalidSubmissionError("authorizationList", "r or s out of range");
      }
      if (a.nonce >= MAX_AUTHORIZATION_NONCE) {
        throw new InvalidSubmissionError("authorizationList", "nonce out of range");
      }
    }
  }

  status(id: Hex): Tracked {
    const tracked = this.tracked.get(id);
    if (tracked === undefined) {
      throw new UnknownSubmissionError(`no submission ${id}`);
    }
    return tracked;
  }

  /**
   * Make the next batch, as of `latest`: rank the queue, take the top `accounts` that still
   * simulate and cover the next block's price, map them onto accounts, and send the float's
   * funding for those that need it (unless a funding transaction is in flight: they wait for
   * the next batch). The queue's reads, its simulations and the accounts' state are each one
   * batch.
   */
  async closeBatch(latest: Latest): Promise<void> {
    const schedule = this.chain.scheduleAfter(latest);
    const fee = this.fees(latest).maxFeePerGas;
    const pending = this.pending();
    const plans = await this.plans(pending.map((t) => t.submission), schedule);
    const ranked: { profit: Fraction; tracked: Tracked; plan: Plan }[] = [];
    for (const [i, tracked] of pending.entries()) {
      try {
        const plan = settled(plans[i]);
        tracked.gasLimit = plan.gasLimit;
        const required = await this.price(tracked.submission.token, BigInt(plan.gasLimit) * fee);
        if (tracked.submission.amount >= required) {
          ranked.push({ profit: await this.profit(tracked, fee), tracked, plan });
        }
      } catch (e) {
        itemFailed(`ranking ${tracked.id}`, e); // this one waits; the rest go on
      }
    }
    ranked.sort((a, b) => b.profit.compare(a.profit));
    // every ranked one simulated together: the best that still simulate are chosen
    const sender = derive(this.float, 0).address;
    const simulating = await this.stillSimulate(ranked.map((r) => ({ ...r, sender })));
    const chosen = ranked.filter((_, i) => simulating[i]).slice(0, this.config.accounts);
    const addresses = Array.from(
      { length: this.config.accounts },
      (_, index) => derive(this.float, index).address,
    );
    const free = (await this.chain.accounts(addresses)).map(({ balance, nonce }, index) => ({
      index,
      address: addresses[index] as Address,
      balance,
      exists: balance > 0n || nonce > 0,
    }));
    const ceiling = this.maxFee(latest, this.config.quoteBlocks);
    const slots: { id: Hex; account: number }[] = [];
    const topUps: TopUp[] = [];
    const awaiting: number[] = [];
    for (const { tracked, plan } of chosen) {
      const cost = BigInt(plan.gasLimit) * ceiling;
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
      const nonce = await this.chain.nonce(this.float.address);
      const fees = lastingFees(latest, this.config.priorityFee);
      const tx = signFunding(this.float, this.chain.chainId, nonce, fees, topUps, schedule);
      this.funding = {
        hash: hex(tx.hash),
        raw: hex(tx.raw),
        accounts: awaiting,
        sentAt: Date.now(),
      };
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
   * `latest` is the latest block: settle, then send the batch's bundles for the next. Each
   * slot's transaction is signed if it covers the block's price, still simulates, and its
   * account covers it; the receipts, the slots' reads, their simulations and their accounts'
   * state are each one batch.
   */
  async onBlock(latest: Latest): Promise<void> {
    const target = latest.number + 1;
    this.prune();
    await this.settle(target);
    const schedule = this.chain.scheduleAfter(latest);
    const fees = this.fees(latest);
    const slots = this.batch.slots.flatMap((slot) => {
      const tracked = this.tracked.get(slot.id);
      if (
        tracked === undefined || tracked.status !== "pending" ||
        this.batch.awaiting.includes(slot.account)
      ) {
        return [];
      }
      return [{ tracked, account: derive(this.float, slot.account) }];
    });
    const plans = await this.plans(slots.map((s) => s.tracked.submission), schedule);
    const covered: { tracked: Tracked; account: Signer; plan: Plan; sender: Address }[] = [];
    for (const [i, { tracked, account }] of slots.entries()) {
      try {
        const plan = settled(plans[i]);
        tracked.gasLimit = plan.gasLimit;
        const gasCost = BigInt(plan.gasLimit) * fees.maxFeePerGas;
        if (tracked.submission.amount >= await this.price(tracked.submission.token, gasCost)) {
          covered.push({ tracked, account, plan, sender: account.address });
        }
      } catch (e) {
        itemFailed(`signing ${tracked.id}`, e); // this one sits out the block; the rest go on
      }
    }
    const simulating = await this.stillSimulate(covered);
    const ready = covered.filter((_, i) => simulating[i]);
    const states = await this.chain.accounts(ready.map((r) => r.sender));
    const txs: SignedTransaction[] = [];
    for (const [i, { tracked, account, plan }] of ready.entries()) {
      try {
        const state = states[i];
        if (state === undefined) continue;
        const tx = sign(
          account,
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
        itemFailed(`signing ${tracked.id}`, e);
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
    for (const tracked of this.tracked.values()) {
      if (tracked.finishedAt !== null && (first === null || tracked.finishedAt < first)) {
        first = tracked.finishedAt;
      }
    }
    return first === null ? null : first + this.config.retentionSeconds * 1000;
  }

  /** Forget the submissions finished more than `retentionSeconds` ago. */
  prune(): void {
    const cutoff = Date.now() - this.config.retentionSeconds * 1000;
    for (const [id, tracked] of this.tracked) {
      if (tracked.finishedAt !== null && tracked.finishedAt <= cutoff) {
        this.tracked.delete(id);
        this.store.delete(`submission:${id}`);
      }
    }
  }

  private pending(): Tracked[] {
    return [...this.tracked.values()].filter((t) => t.status === "pending");
  }

  private save(tracked: Tracked): void {
    this.store.put(`submission:${tracked.id}`, trackedToJson(tracked));
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
  private async settle(target: number): Promise<void> {
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
        this.settleOne(tracked, target, receipt);
      } catch (e) {
        itemFailed(`settling ${tracked.id}`, e);
      }
    }
    if (funding !== null) {
      await this.settleFunding(funding, receipt(funding.hash));
    }
  }

  private settleOne(
    tracked: Tracked,
    target: number,
    receipt: (hash: Hex) => Receipt | null,
  ): void {
    for (const sent of tracked.sent) {
      const landed = receipt(sent);
      if (landed !== null) {
        tracked.transactionHash = sent;
        tracked.blockNumber = landed.blockNumber;
        if (landed.status === "0x1") {
          finish(tracked, "included");
        } else {
          finish(tracked, "rejected", "reverted on-chain");
        }
        this.save(tracked);
        return;
      }
    }
    if (target > tracked.submission.maxBlockNumber) {
      finish(tracked, "expired");
      this.save(tracked);
    }
  }

  /** Whether the funding in flight landed (its receipt, or null): its accounts are then ready. */
  private async settleFunding(funding: Funding, receipt: Receipt | null): Promise<void> {
    if (receipt !== null) {
      this.funding = null;
      if (receipt.status === "0x1") {
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
   * receipt tells, and a landed call no longer simulates.
   */
  private async stillSimulate(runs: readonly Run[]): Promise<boolean[]> {
    const reverts = await this.simulations(
      runs.map((r) => ({ submission: r.tracked.submission, plan: r.plan, sender: r.sender })),
    );
    return runs.map(({ tracked }, i) => {
      const revert = reverts[i];
      if (revert === null || revert === undefined) return true;
      if (tracked.sent.length > 0) return false;
      finish(tracked, "rejected", "simulation failed");
      tracked.revertData = hex(revert.slice(0, MAX_REVERT_BYTES));
      this.save(tracked);
      return false;
    });
  }

  /** The submission's amount in wei, less its transaction's cost at `fee` a gas. */
  private async profit(tracked: Tracked, fee: bigint): Promise<Fraction> {
    const wei = await this.prices.wei(tracked.submission.token);
    return wei.mul(tracked.submission.amount).sub(BigInt(tracked.gasLimit) * fee);
  }

  private withinLimits(gas: number, callBytes: number): void {
    if (gas > this.config.maxGas) {
      throw new InvalidSubmissionError("gas", `over ${this.config.maxGas}`);
    }
    if (callBytes > this.config.maxCallBytes) {
      throw new InvalidSubmissionError("call", `over ${this.config.maxCallBytes} bytes`);
    }
  }

  private supported(token: Address): void {
    if (!this.tokens.has(token)) {
      throw new UnsupportedError(`${token} isn't accepted`);
    }
  }

  private erc20(token: Address): Erc20 | null {
    return this.tokens.get(token) ?? null;
  }

  /** The fees of the block after `latest`: exactly its base fee, and the priority fee. */
  private fees(latest: Latest): Fees {
    const priority = this.config.priorityFee;
    return { maxFeePerGas: latest.nextBaseFee + priority, maxPriorityFeePerGas: priority };
  }

  /** The most a fee can be `blocks` blocks after the next: risen an eighth a block. */
  private maxFee(latest: Latest, blocks: number): bigint {
    let rise = new Fraction(1n);
    for (let i = 0; i < blocks; i++) rise = rise.mul(MAX_BASE_FEE_RISE);
    return rise.mul(latest.nextBaseFee).ceil() + this.config.priorityFee;
  }

  /** `wei` in `token`'s base units, margins included, rounded up. */
  private async price(token: Address, wei: bigint): Promise<bigint> {
    const margin = this.margins.get(token) ?? new Fraction(1n);
    return new Fraction(wei).div(await this.prices.wei(token)).mul(margin).ceil();
  }

  /** How many of `authorities` are empty (no balance, no nonce, no code), read in one batch. */
  private async empty(authorities: readonly Address[]): Promise<number> {
    const answers = await this.chain.batch(authorities.flatMap(authorityReads));
    return countEmpty(answers, 0, authorities.length);
  }

  /** The submission's runner and gas limit, against `schedule`. */
  private async plan(submission: Submission, schedule: GasSchedule): Promise<Plan> {
    return settled((await this.plans([submission], schedule))[0]);
  }

  /**
   * Each submission's runner and gas limit, against `schedule`, their reads in one batch (its
   * call's target's code; each authority's balance, nonce and code): each its plan, or why not.
   */
  private async plans(
    submissions: readonly Submission[],
    schedule: GasSchedule,
  ): Promise<PromiseSettledResult<Plan>[]> {
    const authorities = submissions.map((s): Address[] | InvalidSubmissionError => {
      try {
        return s.authorizations.map(authority);
      } catch (e) {
        return new InvalidSubmissionError("authorizationList", `unrecoverable: ${e}`);
      }
    });
    const calls: Call[] = [];
    const firsts = submissions.map((s, i) => {
      const first = calls.length;
      const of = authorities[i];
      if (Array.isArray(of)) {
        calls.push({ method: "eth_getCode", params: [s.call.to, "latest"] });
        calls.push(...of.flatMap(authorityReads));
      }
      return first;
    });
    const answers = await this.chain.batch(calls);
    return submissions.map((submission, i): PromiseSettledResult<Plan> => {
      try {
        const of = authorities[i];
        const first = firsts[i];
        if (of === undefined || first === undefined) throw new ChainError("no plan read");
        if (!Array.isArray(of)) throw of;
        const code = answers[first];
        if (code === undefined) throw new ChainError("no plan read");
        const delegated = new Set(of);
        if (hexOf(resultOf(code)).startsWith(DELEGATION_PREFIX)) {
          delegated.add(submission.call.to);
        }
        const empty = countEmpty(answers, first + 1, of.length);
        return {
          status: "fulfilled",
          value: this.planned(submission, schedule, of, delegated, empty),
        };
      } catch (reason) {
        return { status: "rejected", reason };
      }
    });
  }

  private planned(
    submission: Submission,
    schedule: GasSchedule,
    authorities: readonly Address[],
    delegated: ReadonlySet<Address>,
    empty: number,
  ): Plan {
    const payment = {
      token: this.erc20(submission.token),
      payee: submission.payee,
      amount: submission.amount,
    };
    let runner: Runner;
    try {
      runner = inCallRunner(submission.call, payment, schedule, delegated);
    } catch (e) {
      if (e instanceof RunnerError) throw new InvalidSubmissionError("call", e.message);
      throw e;
    }
    const gasLimit = transactionGas(
      runner.initcode,
      frameGas(runner, submission.gas),
      schedule,
      authorities.length,
      empty,
    );
    return { runner, gasLimit };
  }

  /** The submission's transaction as eth_call from `sender`: throw its revert data. */
  private async simulate(submission: Submission, plan: Plan, sender: Address): Promise<void> {
    const [revert] = await this.simulations([{ submission, plan, sender }]);
    if (revert !== null && revert !== undefined) {
      throw new SimulationFailedError(revert);
    }
  }

  /** Each submission's transaction as eth_call from its sender, in one batch: each its revert
   * data, or null if it doesn't revert. */
  private async simulations(sims: readonly Simulation[]): Promise<(Uint8Array | null)[]> {
    const answers = await this.chain.batch(sims.map(({ submission, plan, sender }) => {
      const tx: Record<string, unknown> = {
        from: sender,
        to: DEPLOYER,
        data: hex(plan.runner.initcode),
        gas: `0x${plan.gasLimit.toString(16)}`,
      };
      if (submission.authorizations.length > 0) {
        tx.authorizationList = submission.authorizations.map(authorizationJson);
      }
      return { method: "eth_call", params: [tx, "latest"] };
    }));
    return answers.map((answer) => {
      if (!("error" in answer)) return null;
      const data = answer.error.data;
      return bytes(typeof data === "string" && data.startsWith("0x") ? data as Hex : "0x");
    });
  }
}

/** An authority's reads, to tell whether it's empty: its balance, nonce and code. */
function authorityReads(account: Address): Call[] {
  return [
    { method: "eth_getBalance", params: [account, "latest"] },
    { method: "eth_getTransactionCount", params: [account, "latest"] },
    { method: "eth_getCode", params: [account, "latest"] },
  ];
}

/** How many of the `count` authorities whose reads start at `first` are empty. */
function countEmpty(answers: readonly Answer[], first: number, count: number): number {
  let empty = 0;
  for (let i = 0; i < count; i++) {
    const [balance, nonce, code] = [0, 1, 2].map((k) => {
      const answer = answers[first + 3 * i + k];
      if (answer === undefined) throw new ChainError("no authority read");
      return resultOf(answer);
    });
    if (quantity(balance) === 0n && quantity(nonce) === 0n && hexOf(code) === "0x") {
      empty++;
    }
  }
  return empty;
}

/** A plan that was made: why not, raised. */
function settled<T>(result: PromiseSettledResult<T> | undefined): T {
  if (result === undefined) throw new ChainError("no result");
  if (result.status === "rejected") throw result.reason;
  return result.value;
}

/** An EIP-7702 authorization in a transaction's JSON. */
export function authorizationJson(a: SignedAuthorization): Record<string, string> {
  const q = (v: bigint | number): string => `0x${v.toString(16)}`;
  return {
    chainId: q(a.chainId),
    address: a.address,
    nonce: q(a.nonce),
    yParity: q(a.yParity),
    r: q(a.r),
    s: q(a.s),
  };
}

/** A token's balanceOf gas, measured: eth_estimateGas of balanceOf(holder), less intrinsic. */
export async function measureErc20(
  chain: Chain,
  token: Address,
  holder: Address,
  schedule: GasSchedule,
): Promise<Erc20> {
  const data = Uint8Array.from([
    ...bytes("0x70a08231"),
    ...new Uint8Array(12),
    ...bytes(holder),
  ]);
  const estimate = Number(
    BigInt(String(await chain.rpc("eth_estimateGas", [{ to: token, data: hex(data) }]))),
  );
  return { address: token, balanceGas: estimate - intrinsic(schedule, data).standard };
}

function trackedToJson(t: Tracked): string {
  const s = t.submission;
  return JSON.stringify({
    ...t,
    submission: {
      call: { to: s.call.to, data: hex(s.call.data) },
      authorizations: s.authorizations.map(authorizationJson),
      gas: s.gas,
      token: s.token,
      payee: s.payee,
      amount: s.amount.toString(),
      maxBlockNumber: s.maxBlockNumber,
    },
  });
}

function trackedFromJson(text: string): Tracked {
  const raw = JSON.parse(text);
  const s = raw.submission;
  return {
    ...raw,
    submission: {
      call: { to: checksum(s.call.to), data: bytes(s.call.data), value: 0n },
      authorizations: s.authorizations.map((a: Record<string, string>) => ({
        chainId: BigInt(a.chainId as string),
        address: checksum(a.address as string),
        nonce: BigInt(a.nonce as string),
        yParity: Number(a.yParity),
        r: BigInt(a.r as string),
        s: BigInt(a.s as string),
      })),
      gas: s.gas,
      token: checksum(s.token),
      payee: checksum(s.payee),
      amount: BigInt(s.amount),
      maxBlockNumber: s.maxBlockNumber,
    },
  };
}

/** One submission's work failed: logged, and the others' go on. What isn't expected is thrown. */
function itemFailed(what: string, e: unknown): void {
  if (
    e instanceof RelayError || e instanceof ChainError || e instanceof JsonRpcError ||
    e instanceof PriceError || e instanceof TransactionError
  ) {
    console.warn(`${what}:`, e.message);
    return;
  }
  throw e;
}
