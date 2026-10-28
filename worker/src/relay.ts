/**
 * The relay on one chain: what it offers, its quotes, the submissions it takes, its batches and
 * each block's bundles (docs/development/design/queue.md, price.md). A port of the Python
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
  type Finality,
  hexOf,
  JsonRpcError,
  lastingFees,
  type Latest,
  marketTip,
  quantity,
  type Receipt,
  receiptOf,
  resultOf,
  risen,
} from "./chain.ts";
import type { ChainConfig, Eip712Domain } from "./config.ts";
import { DEPLOYER } from "./deployer.ts";
import { lastBlockBefore, quotedCalldata, transferGas } from "./eip3009.ts";
import type { Deployment } from "./erc6492.ts";
import { verification } from "./wallets.ts";
import {
  type Address,
  bytes,
  type EncodedCall,
  hasCode,
  type Hex,
  hex,
  NATIVE_ASSET,
} from "./evm.ts";
import { Fraction } from "./fraction.ts";
import {
  BlockUnseenError,
  empty,
  makePlan,
  makePlans,
  type Plan,
  runnerPlan,
  settled,
  simulate,
  SimulationFailedError,
  simulations,
} from "./plan.ts";
import { type AccountsStatus, statusFromJson, statusToJson, survey } from "./maintenance.ts";
import { PriceError, type Prices } from "./prices.ts";
import { inCallRunner, type Payment } from "./runner.ts";
import { intrinsic } from "./forks.ts";
import { holding, measure, type Measurements, problems } from "./measurements.ts";
import type { GasSchedule } from "./forks.ts";
import {
  checkAuthorizations,
  InvalidSubmissionError,
  type Submission,
  submissionId,
} from "./submission.ts";
import {
  finish,
  MAX_REVERT_BYTES,
  SENT_KEPT,
  type Status,
  type Tracked,
  trackedFromJson,
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
import { risenDataFee } from "./datafee.ts";

/** How far the tip may rise over a quote's blocks, as a multiple of today's: a quote allows for
 * it, and an account is funded for it. */
const TIP_ALLOWANCE = 2n;
/** The largest amount: a quote's runner pushes it, so its initcode is never longer. */
const MAX_AMOUNT = (1n << 256n) - 1n;
/** How long after a slot's start its block is looked for. */
const SLOT_OFFSET_SECONDS = 1;
/** How soon measurements that couldn't be taken again are tried again, the last kept. */
const MEASURE_RETRY_MS = 60_000;
export class RelayError extends Error {}

export class UnsupportedError extends RelayError {}

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

/** The chain can't be served now: its nodes, its fork or its deployer aren't what the relay
 * needs (measurements), or it hasn't been measured yet. */
export class UnavailableError extends RelayError {}

export interface Terms {
  readonly payee: Address;
  readonly tokens: readonly Address[];
  readonly maxGas: number;
  readonly maxCallBytes: number;
}

/** A batch's answer at `index`: ChainError if it's missing. */
function answerAt(answers: readonly Answer[], index: number): Answer {
  const answer = answers[index];
  if (answer === undefined) throw new ChainError("no answer");
  return answer;
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

/** The fees a transaction can pay by a quote's last block, as of `latest`: the next base fee
 * risen for `quoteBlocks` blocks, and the tip doubled; what's paid beyond gas, risen for them
 * too. Accounts are funded to it. */
export function feeCeiling(latest: Latest, quoteBlocks: number): Fees {
  const tip = marketTip(latest.tips);
  return {
    maxFeePerGas: risen(latest.nextBaseFee, quoteBlocks) + TIP_ALLOWANCE * tip,
    maxPriorityFeePerGas: tip,
    ...(latest.dataFee === undefined ? {} : { dataFee: risenDataFee(latest.dataFee, quoteBlocks) }),
  };
}

export class Relay {
  private readonly tracked = new Map<Hex, Tracked>();
  private batch: Batch;
  private funding: Funding | null;
  /** Submissions are queued one at a time: a duplicate or a full queue is decided once. */
  private queueing: Promise<unknown> = Promise.resolve();
  private readonly margins: Map<Address, Fraction>;
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
  private measuring: Promise<Measurements> | null = null;
  /** With nothing measured, when the nodes may be asked again after they didn't answer (ms). */
  private unmeasuredUntil = 0;
  /** Each token taken over x402, and its EIP-712 domain, as configured. */
  readonly domains: ReadonlyMap<Address, Eip712Domain>;

  constructor(
    readonly chain: Chain,
    readonly config: ChainConfig,
    readonly float: Signer,
    readonly prices: Prices,
    readonly builders: readonly Builder[],
    private readonly store: Store,
  ) {
    this.margins = new Map(
      config.tokens.map((t) => [t.address, config.marginMultiplier.mul(t.marginMultiplier)]),
    );
    this.domains = new Map(
      config.tokens.flatMap((t) =>
        t.eip3009 === null || t.address === NATIVE_ASSET ? [] : [[t.address, t.eip3009]]
      ),
    );
    for (const value of store.list("submission:")) {
      const tracked = trackedFromJson(value);
      this.tracked.set(tracked.id, tracked);
    }
    this.batch = JSON.parse(store.get("batch") ?? '{"slots":[],"awaiting":[]}');
    this.funding = JSON.parse(store.get("funding") ?? "null");
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
      throw new UnavailableError("not measured yet: its nodes didn't answer");
    }
    let measured: Measurements;
    try {
      measured = await measure(this.chain, this.config, this.payee);
    } catch (e) {
      console.warn(`chain ${this.chain.chainId}: measuring:`, e);
      if (this.measured === null) {
        this.unmeasuredUntil = Date.now() + MEASURE_RETRY_MS;
        throw new UnavailableError("not measured yet: its nodes didn't answer");
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

  private fresh(m: Measurements | null): m is Measurements {
    return m !== null && Date.now() - m.at < this.config.measureSeconds * 1000;
  }

  /** The measurements, if the chain can be served; else UnavailableError, why. */
  async available(): Promise<Measurements> {
    const measured = await this.measurements();
    if (measured.unavailable !== null) throw new UnavailableError(measured.unavailable);
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
    this.withinLimits(gas, callBytes);
    const measured = await this.available();
    tokens.forEach((t) => this.supported(measured, t));
    const latest = await this.latest();
    const signing = await this.signing(measured, payer, payer.address !== undefined);
    return await this.quoteAt(
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
    this.withinLimits(gas, callBytes);
    const measured = await this.available();
    const latest = await this.recentLatest();
    const tokens = this.x402Tokens(measured);
    const signing = await this.signing(measured, payer, true);
    return await this.quoteAt(
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
  private async signing(
    measured: Measurements,
    payer: Payer,
    transferring: boolean,
  ): Promise<Signing> {
    if (payer.verifyGas !== undefined && !transferring) {
      throw new InvalidSubmissionError("verifyGas", "only with a payer");
    }
    if (payer.verifyGas !== undefined && payer.verifyGas > this.config.maxGas) {
      throw new InvalidSubmissionError("verifyGas", `over ${this.config.maxGas}`);
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
    if (deployment !== null) {
      const deploys = !hasCode(hexOf(resultOf(answerAt(answers, 0))));
      const estimate = answerAt(answers, 1);
      if ("error" in estimate) {
        if (deploys) {
          throw new InvalidSubmissionError(
            "deployment",
            `its factory call fails: ${estimate.error.nodeMessage}`,
          );
        }
      } else if (deploys || transferring) {
        deployGas = Number(quantity(estimate.result)) -
          intrinsic(schedule, deployment.factoryData).standard;
      }
    }
    const signerHasCode = readSigner &&
      hasCode(hexOf(resultOf(answerAt(answers, answers.length - 1))));
    const contract = transferring && (
      payer.verifyGas !== undefined ||
      (deployment !== null && deployment.account === signer) ||
      signerHasCode
    );
    if (!contract) {
      return { transferring, verifyGas: 0, signatureBytes: null, deployment, deployGas };
    }
    const known = verification(deployment, schedule);
    return {
      transferring,
      verifyGas: payer.verifyGas ?? known.gas,
      signatureBytes: known.signatureBytes,
      deployment,
      deployGas,
    };
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

  private async quoteAt(
    measured: Measurements,
    latest: Latest,
    gas: number,
    callBytes: number,
    authorityAddress: Address | null,
    tokens: readonly Address[],
    signing: Signing,
    existing: boolean,
  ): Promise<Quotes> {
    const schedule = scheduleOf(measured);
    const target = authorityAddress ?? this.payee;
    const call: EncodedCall = {
      to: target,
      data: new Uint8Array(callBytes).fill(0xff),
      value: 0n,
    };
    const authorities = authorityAddress === null ? [] : [authorityAddress];
    const empties = existing ? 0 : await empty(this.chain, authorities);
    // the base fee risen and the tip doubled, as they can be by the quote's last block
    const fees = feeCeiling(latest, this.config.quoteBlocks - 1);
    const quotes = [];
    for (const token of tokens) {
      const erc20 = measured.tokens.get(token) ?? null;
      let payment: Payment = { token: erc20, payee: this.payee, amount: MAX_AMOUNT };
      if (signing.transferring && erc20?.transferGas !== undefined) {
        const bound = transferGas(erc20.transferGas, schedule, measured.payeeHolds.has(token));
        payment = {
          ...payment,
          transfer: {
            calldata: quotedCalldata(signing.signatureBytes),
            gas: bound + signing.verifyGas,
          },
        };
      }
      const runner = inCallRunner(call, payment, schedule, gas, {
        delegated: new Set(authorities),
        deployment: signing.deployment,
      });
      const plan = runnerPlan(
        this.chain.chainId,
        runner,
        gas,
        schedule,
        authorities.length,
        empties,
        {
          extraGas: signing.deployGas,
        },
      );
      quotes.push({ token, amount: await this.charge(measured, latest, token, plan, fees) });
    }
    return { validUntilBlock: latest.number + this.config.quoteBlocks, quotes };
  }

  /** Take `submission`, checked; or throw why not. Its id, and whether it's new. */
  async submit(submission: Submission): Promise<{ id: Hex; isNew: boolean }> {
    this.withinLimits(submission.gas, submission.call.data.length);
    const measured = await this.available();
    this.supported(measured, submission.token);
    if (submission.payee !== this.payee) {
      throw new UnsupportedError(`the payee is ${this.payee}, not ${submission.payee}`);
    }
    checkAuthorizations(
      submission.authorizations,
      this.chain.chainId,
      this.config.maxAuthorizations,
    );
    const transfer = submission.transfer;
    if (transfer !== undefined) {
      if (!this.x402Tokens(measured).includes(submission.token)) {
        throw new UnsupportedError(`${submission.token} takes no EIP-3009 transfer`);
      }
      if (transfer.to !== submission.payee || transfer.value !== submission.amount) {
        throw new InvalidSubmissionError("payment", "the transfer isn't the payment");
      }
    }
    const id = hex(submissionId(submission));
    if (this.tracked.has(id)) {
      return { id, isNew: false };
    }
    const latest = await this.latest();
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
    const fees = this.fees(latest);
    const required = await this.charge(measured, latest, submission.token, plan);
    if (submission.amount < required) {
      throw new UnderpaidError(submission.token, required);
    }
    try {
      await simulate(this.chain, submission, plan, sender);
    } catch (e) {
      // nothing known of it yet: the client tries again
      if (e instanceof BlockUnseenError) throw new UnavailableError(e.message);
      throw e;
    }
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
        await this.makeRoom(tracked, queue, fees);
      }
      this.tracked.set(id, tracked);
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
  private async makeRoom(tracked: Tracked, queue: readonly Tracked[], fees: Fees): Promise<void> {
    let worst: { profit: Fraction; tracked: Tracked } | null = null;
    let worstEvictable: { profit: Fraction; tracked: Tracked } | null = null;
    for (const t of queue) {
      const profit = await this.profit(t, fees);
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
    if (worstEvictable === null || (await this.profit(tracked, fees)).compare(bar.profit) <= 0) {
      const cost = txCost(fees, tracked.gasLimit, tracked.size);
      const wei = await this.prices.wei(tracked.submission.token);
      throw new NoCapacityError(
        tracked.submission.token,
        bar.profit.add(cost).div(wei).floor() + 1n,
      );
    }
    this.finish(worstEvictable.tracked, "rejected", "outbid");
  }

  status(id: Hex): Tracked {
    const tracked = this.tracked.get(id);
    if (tracked === undefined) {
      throw new UnknownSubmissionError(`no submission ${id}`);
    }
    return tracked;
  }

  /** The submission once it's no longer pending: included, rejected or expired. Waiting reads
   * nothing: it's the settlement the relay runs each block that ends it. */
  async outcome(id: Hex): Promise<Tracked> {
    const tracked = this.status(id);
    if (tracked.status === "pending") {
      await new Promise<void>((resolve) => {
        this.outcomes.set(id, [...(this.outcomes.get(id) ?? []), resolve]);
      });
    }
    return tracked;
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
    const fees = this.fees(latest);
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
        ranked.push({ profit: await this.profit(tracked, fees), tracked, plan });
      } catch (e) {
        itemFailed(`ranking ${tracked.id}`, e); // this one waits; the rest go on
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
      this.float,
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
    const tip = this.tip(latest);
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
      const tx = signFunding(this.float, this.chain.chainId, nonce, fees, topUps, schedule);
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
   * `latest` is the latest block: settle, then send the batch's bundles for the next. Each
   * slot's transaction is signed if it covers the block's price, still simulates, and its
   * account covers it; the receipts, the slots' reads, their simulations and their accounts'
   * state are each one batch.
   */
  async onBlock(latest: Latest): Promise<void> {
    const target = latest.number + 1;
    this.prune();
    await this.settle(target);
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
    if (slots.length === 0) return; // nothing to send: nothing more read
    const measured = await this.measurements();
    if (measured.unavailable !== null) return; // settled, but nothing sent
    const schedule = scheduleOf(measured);
    const fees = this.fees(latest);
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
        .map(({ item: { tracked, account }, plan }) => ({
          tracked,
          account,
          plan,
          sender: account.address,
        }));
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

  /** `tracked` finished, as `status`, and stored: a request held for it is answered. */
  private finish(tracked: Tracked, status: Status, reason: string | null = null): void {
    finish(tracked, status, reason);
    this.save(tracked);
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
      if (revert instanceof BlockUnseenError) {
        console.warn(`simulating ${tracked.id}: ${revert.message}`);
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
        const required = await this.charge(measured, latest, one.submission.token, plan);
        if (one.submission.amount >= required) covered.push({ item, plan });
      } catch (e) {
        itemFailed(`${doing} ${one.id}`, e);
      }
    }
    return covered;
  }

  /** The float's account 0: what a submission's transaction is planned and simulated as sent
   * from. */
  private get sender(): Address {
    return derive(this.float, 0).address;
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

  /** The submission's amount in wei, less its transaction's cost at `fees`. */
  private async profit(tracked: Tracked, fees: Fees): Promise<Fraction> {
    const wei = await this.prices.wei(tracked.submission.token);
    return wei.mul(tracked.submission.amount).sub(txCost(fees, tracked.gasLimit, tracked.size));
  }

  private withinLimits(gas: number, callBytes: number): void {
    if (gas > this.config.maxGas) {
      throw new InvalidSubmissionError("gas", `over ${this.config.maxGas}`);
    }
    if (callBytes > this.config.maxCallBytes) {
      throw new InvalidSubmissionError("call", `over ${this.config.maxCallBytes} bytes`);
    }
  }

  private supported(measured: Measurements, token: Address): void {
    if (!this.margins.has(token)) {
      throw new UnsupportedError(`${token} isn't accepted`);
    }
    const why = measured.suspended.get(token);
    if (why !== undefined) throw new UnsupportedError(`${token} is suspended: ${why}`);
  }

  /** The tip each transaction offers: the market's (marketTip). */
  private tip(latest: Latest): bigint {
    return marketTip(latest.tips);
  }

  /** The fees of the block after `latest`: exactly its base fee, and the tip; what's paid
   * beyond gas risen for the one block (its L1 inputs may be a new L1 block's). */
  private fees(latest: Latest): Fees {
    const tip = this.tip(latest);
    return {
      maxFeePerGas: latest.nextBaseFee + tip,
      maxPriorityFeePerGas: tip,
      ...(latest.dataFee === undefined ? {} : { dataFee: risenDataFee(latest.dataFee, 1) }),
    };
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
      throw new UnavailableError(`min_fee's token ${minFee.token} is suspended`);
    }
    const units = minFee.amount.mul(new Fraction(10n ** BigInt(decimals))).ceil();
    const least = (await this.prices.wei(minFee.token)).mul(units).ceil();
    return fee > least ? fee : least;
  }

  /** What `plan`'s transaction is charged in `token`: its cost at `fees` (the fees of the block
   * after `latest`, if not given), and our fee, margins included. */
  private async charge(
    measured: Measurements,
    latest: Latest,
    token: Address,
    plan: Plan,
    fees: Fees = this.fees(latest),
  ): Promise<bigint> {
    const cost = txCost(fees, plan.gasLimit, plan.size);
    return await this.price(token, cost + await this.ourFee(measured, latest, plan.gasLimit));
  }

  /** `wei` in `token`'s base units, margins included, rounded up. */
  private async price(token: Address, wei: bigint): Promise<bigint> {
    const margin = this.margins.get(token) ?? new Fraction(1n);
    return new Fraction(wei).div(await this.prices.wei(token)).mul(margin).ceil();
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
      { decimals: measured.decimals.get(t) ?? 18, symbol: measured.symbols.get(t) ?? t },
    ]),
  );
}

/** The measured schedule of an available chain. */
function scheduleOf(measured: Measurements): GasSchedule {
  if (measured.schedule === null) {
    throw new UnavailableError(measured.unavailable ?? "no schedule measured");
  }
  return measured.schedule;
}

/** One submission's work failed: logged, and the others' go on. What isn't expected is thrown. */
function itemFailed(what: string, e: unknown): void {
  if (
    e instanceof RelayError || e instanceof InvalidSubmissionError ||
    e instanceof SimulationFailedError || e instanceof ChainError || e instanceof JsonRpcError ||
    e instanceof PriceError || e instanceof TransactionError
  ) {
    console.warn(`${what}:`, e.message);
    return;
  }
  throw e;
}
