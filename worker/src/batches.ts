/**
 * Each batch and each block on one chain: the queue's best mapped onto concurrency accounts,
 * funded from the float, and their transactions signed and sent each block, and what was sent
 * reconciled with its receipts; the batch and the funding in flight kept in the Store. As
 * bruno/batches.py's.
 */

import { derive } from "./accounts.ts";
import { type Builder, sendBundle } from "./builders.ts";
import { atomicParams, type BundleParams, bundles, signFunding, type TopUp } from "./bundle.ts";
import { type Chain, ChainError, type Latest, quantity, type Receipt, receiptOf } from "./chain.ts";
import { JsonRpcError, resultOf } from "./jsonrpc.ts";
import type { ChainConfig } from "./config.ts";
import { type Address, type Hex, hex, NATIVE_ASSET } from "./evm.ts";
import type { GasSchedule } from "./forks.ts";
import { Fraction } from "./fraction.ts";
import {
  BlockUnseenError,
  makePlans,
  type Plan,
  settled,
  SimulationFailedError,
  simulations,
} from "./plan.ts";
import { type Denomination, exists, monitor, survey } from "./maintenance.ts";
import { PriceError } from "./prices.ts";
import { feeCeiling, type Pricing, UnsupportedError } from "./pricing.ts";
import {
  lookup,
  type Measurements,
  Measuring,
  scheduleOf,
  UnavailableError,
} from "./measurements.ts";
import { InvalidSubmissionError } from "./submission.ts";
import { MAX_REVERT_BYTES, paying, Queue, SENT_KEPT, type Tracked } from "./tracked.ts";
import {
  cost as txCost,
  maxCost,
  sign,
  type SignedTransaction,
  type Signer,
  TransactionError,
} from "./transactions.ts";

import type { Store } from "./store.ts";

/** A batch's submission, by its id (it's stored), on concurrency account `account`. */
interface Slot {
  readonly id: Hex;
  readonly account: number;
}

/** A batch's submissions on their accounts, and the accounts awaiting the funding. */
interface Batch {
  readonly slots: readonly Slot[];
  readonly awaiting: readonly number[];
}

/**
 * The float's funding, not yet landed: its top-ups, the accounts they're for, and the float's
 * nonce it's signed at; the transactions sent for it (the latest SENT_KEPT, one a block, each
 * signed at that block's fees), and the block it was last sent for. It's stored before each is
 * sent: an object replaced between storing and sending loses nothing, every copy sent is
 * looked for, and only one at the nonce can land.
 */
interface Funding {
  readonly topUps: readonly TopUp[];
  readonly accounts: readonly number[];
  readonly nonce: number;
  readonly sent: readonly Hex[];
  readonly sentFor: number;
}

/** A tracked submission's transaction, as planned, from `sender`. */
interface Run {
  readonly tracked: Tracked;
  readonly plan: Plan;
  readonly sender: Address;
}

export class Batches {
  private batch: Batch;
  private funding: Funding | null;

  constructor(
    private readonly chain: Chain,
    private readonly config: ChainConfig,
    private readonly floatAccount: Signer,
    private readonly builders: readonly Builder[],
    private readonly store: Store,
    private readonly queue: Queue,
    private readonly pricing: Pricing,
    private readonly measuring: Measuring,
  ) {
    this.batch = JSON.parse(store.get("batch") ?? '{"slots":[],"awaiting":[]}');
    this.funding = parseFunding(store.get("funding") ?? "null");
  }

  /** Whether the float's funding is out, not yet landed. */
  get fundingInFlight(): boolean {
    return this.funding !== null;
  }

  private get payee(): Address {
    return this.config.payee;
  }

  /** One submission's work failing, the rest going on: what it may meet logged; anything else a
   * bug of ours, logged and listed with the chain's problems, so it's seen. As bruno/batches.py's
   * _item. */
  private itemFailed(what: string, e: unknown): void {
    if (itemError(e)) {
      console.warn(`${what}:`, e.message);
      return;
    }
    console.error(`${what}: internal error`, e);
    this.measuring.internalError(what, e);
  }

  /**
   * Make the next batch, as of `latest`: rank the queue, take the top `accounts` that still
   * simulate and cover the next block's price, map them onto accounts, and send the float's
   * funding for those that need it, for the next block (unless a funding is in flight: they
   * wait for the next batch). Nothing queued, nothing read. The queue's reads, its simulations and the
   * accounts' state are each one batch.
   */
  async closeBatch(latest: Latest): Promise<void> {
    const pending = this.queue.pending();
    if (pending.length === 0) {
      this.batch = { slots: [], awaiting: [] };
      this.saveState();
      return;
    }
    const measured = await this.measuring.measurements();
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
      pending.map((t) => t.proof),
      schedule,
      measured.tokens,
      sender,
      measured.payeeHolds,
    );
    const ranked: { profit: Fraction; tracked: Tracked; plan: Plan }[] = [];
    const covered = await this.covered(measured, latest, pending, (t) => t, plans, "ranking");
    for (const { item: tracked, plan } of covered) {
      try {
        const profit = await this.pricing.profit(tracked, fees, latest.nextBaseFee);
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
    this.measuring.recordAccounts(status);
    const free = [...status.accounts];
    const ceiling = feeCeiling(latest, this.config.quoteBlocks);
    const slots: Slot[] = [];
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
        topUps.push({ account: covering.address, amount, newAccount: !exists(covering) });
        awaiting.push(covering.index);
      }
      free.splice(free.indexOf(covering), 1);
      slots.push({ id: tracked.id, account: covering.index });
    }
    this.batch = { slots, awaiting };
    if (topUps.length > 0) {
      const funding = {
        topUps,
        accounts: awaiting,
        nonce: status.floatNonce,
        sent: [],
        sentFor: 0,
      };
      this.funding = funding;
      await this.sendFunding(funding, latest, schedule, status.floatBalance);
    }
    this.saveState();
  }

  /**
   * `latest` is the latest block: reconcile what was sent for it and the funding in flight, then
   * send the funding again if it hasn't landed, and the batch's bundles, for the next. Each slot's transaction is signed if it covers the
   * block's price, still simulates, and its account covers it; the receipts, the slots' reads,
   * their simulations and their accounts' state are each one batch, and none is read when
   * there's none to read.
   */
  async onBlock(latest: Latest): Promise<void> {
    const target = latest.number + 1;
    this.queue.prune();
    await this.reconcile(latest);
    const slots = this.batch.slots.flatMap((slot) => {
      const tracked = this.queue.pendingOf(slot.id);
      if (
        tracked === null || tracked.status !== "pending" ||
        this.batch.awaiting.includes(slot.account)
      ) {
        return [];
      }
      return [{ tracked, account: slot.account, signer: derive(this.floatAccount, slot.account) }];
    });
    if (slots.length === 0) return;
    const measured = await this.measuring.measurements();
    if (measured.unavailable !== null) return; // reconciled, but nothing sent
    const schedule = scheduleOf(measured);
    const fees = this.pricing.fees(latest);
    const plans = await makePlans(
      this.chain,
      slots.map((s) => s.tracked.submission),
      slots.map((s) => s.tracked.proof),
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
    if (ready.length === 0) return;
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
          plan.transaction.to,
          plan.transaction.data,
          0n,
          tracked.submission.authorizations,
        );
        if (state.balance < maxCost(tx)) continue;
        tracked.sent = [...tracked.sent, hex(tx.hash)].slice(-SENT_KEPT);
        tracked.attempts += 1;
        this.queue.save(tracked);
        txs.push(tx);
      } catch (e) {
        this.itemFailed(`signing ${tracked.id}`, e);
      }
    }
    if (txs.length > 0) await this.send(target, txs);
  }

  /** Every builder its bundles of `txs` for block `target`. */
  private async send(target: number, txs: readonly SignedTransaction[]): Promise<void> {
    await this.offer(
      target,
      this.builders.flatMap((builder) =>
        bundles(builder.mode, target, null, txs).map((params) => ({ builder, params }))
      ),
    );
  }

  /**
   * `funding` signed at the float's nonce and the fees of the block after `latest`, and sent to
   * every builder for that block, a bundle of its own in either mode: only one transaction at
   * the nonce can land, so only one of those sent for it. Not sent again for a block it's been
   * sent for. One the float, holding `floatBalance`, can't cover isn't sent and is dropped: the
   * next batch funds what's needed then, the float perhaps topped up. Nothing sent for an
   * earlier block can still land. As bruno/batches.py's _send_funding.
   */
  private async sendFunding(
    funding: Funding,
    latest: Latest,
    schedule: GasSchedule,
    floatBalance: bigint,
  ): Promise<void> {
    const target = latest.number + 1;
    if (funding.sentFor >= target) return;
    const tx = signFunding(
      this.floatAccount,
      this.chain.chainId,
      funding.nonce,
      this.pricing.fees(latest),
      funding.topUps,
      schedule,
    );
    if (floatBalance < maxCost(tx)) {
      console.warn(`funding: the float holds ${floatBalance} wei, the funding ${maxCost(tx)}`);
      this.funding = null;
      this.saveState();
      return;
    }
    // stored before it's sent: a copy sent is always looked for
    const sent = [...funding.sent, hex(tx.hash)].slice(-SENT_KEPT);
    this.funding = { ...funding, sent, sentFor: target };
    this.saveState();
    await this.offer(
      target,
      this.builders.map((builder) => ({ builder, params: atomicParams(target, [tx]) })),
    );
  }

  /** Each bundle in `sends` to its builder, for block `target`: one refused is logged. */
  private async offer(
    target: number,
    sends: readonly { builder: Builder; params: BundleParams }[],
  ): Promise<void> {
    const results = await Promise.allSettled(
      sends.map(({ builder, params }) => sendBundle(builder, params)),
    );
    results.forEach((result, i) => {
      if (result.status === "rejected") {
        console.warn(`block ${target}: ${sends[i]?.builder.url} refused a bundle:`, result.reason);
      }
    });
  }

  private saveState(): void {
    this.store.put("batch", JSON.stringify(this.batch));
    this.store.put("funding", serializeFunding(this.funding));
  }

  /**
   * Whether each pending submission's latest transactions landed, and the funding in flight, as
   * of `latest`: every receipt looked for in one batch, with the float's nonce and balance if
   * there's a funding. Each transaction is looked for again until it's SENT_KEPT sends old: a
   * node without the block yet has no receipt. A submission past its deadline, unlanded,
   * expires; a funding not landed is sent again for the next block.
   */
  private async reconcile(latest: Latest): Promise<void> {
    const target = latest.number + 1;
    const pending = this.queue.pending();
    const funding = this.funding;
    const hashes = [...pending.flatMap((t) => t.sent), ...(funding?.sent ?? [])];
    const floats = funding === null ? [] : ["eth_getTransactionCount", "eth_getBalance"].map(
      (method) => ({ method, params: [this.floatAccount.address, "latest"] }),
    );
    const answers = await this.chain.batch([
      ...hashes.map((hash) => ({ method: "eth_getTransactionReceipt", params: [hash] })),
      ...floats,
    ]);
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
      const [nonce, balance] = answers.slice(hashes.length).map((a) => quantity(resultOf(a)));
      if (nonce === undefined || balance === undefined) {
        throw new ChainError("no answer for the float's nonce and balance");
      }
      if (this.reconcileFunding(funding, receipt, Number(nonce))) {
        const measured = await this.measuring.measurements();
        if (measured.unavailable === null) {
          await this.sendFunding(funding, latest, scheduleOf(measured), balance);
        }
      }
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
          this.queue.finish(tracked, "included");
        } else {
          this.queue.finish(tracked, "rejected", "reverted on-chain");
        }
        return;
      }
    }
    if (target > tracked.submission.maxBlockNumber) {
      this.queue.finish(tracked, "expired");
    }
  }

  /**
   * Whether the funding in flight is still to land, by its transactions' receipts and the
   * float's nonce. Landed, its accounts are ready. One whose nonce the float has used for
   * something else is dropped: the next batch funds what's needed then.
   */
  private reconcileFunding(
    funding: Funding,
    receipt: (hash: Hex) => Receipt | null,
    floatNonce: number,
  ): boolean {
    for (const sent of funding.sent) {
      const landed = receipt(sent);
      if (landed !== null) {
        this.funding = null;
        if (landed.status === 1) {
          const awaiting = this.batch.awaiting.filter((a) => !funding.accounts.includes(a));
          this.batch = { slots: this.batch.slots, awaiting };
        } else {
          console.error(`funding ${sent} reverted`);
        }
        this.saveState();
        return false;
      }
    }
    if (floatNonce > funding.nonce) {
      this.funding = null;
      console.error(`funding: the float's nonce ${funding.nonce} was used by another transaction`);
      this.saveState();
      return false;
    }
    return true;
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
      this.queue.finish(tracked, "rejected", "simulation failed");
      return false;
    });
  }

  /**
   * Of `items`, each a submission tracked (`trackedOf`) and planned as `plans`, those whose amount
   * covers their charge in the block after `latest`, with their plans; each kept its plan's gas
   * limit and size. One whose plan failed simulation is rejected (plannedOrFailed); one that
   * can't be planned or priced sits out, logged as `doing`, and the rest go on; one a bug of ours
   * met sits out too, reported.
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
  get sender(): Address {
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
        this.queue.finish(tracked, "rejected", "simulation failed");
      }
      return null;
    }
    return settled(result);
  }
}

/** Each measured token but ETH, and its decimals and symbol: what the payee's holdings are read
 * of. */
export function payeeTokens(
  measured: Measurements,
): Map<Address, Denomination> {
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

/** Whether `e` is what one submission's work may fail with, without failing the others' in its
 * batch or block: a node's failure, the client's mistake, a price or transaction refused
 * (bruno/batches.py's _ITEM_ERRORS). */
function itemError(e: unknown): e is Error {
  return e instanceof UnsupportedError ||
    e instanceof UnavailableError || e instanceof InvalidSubmissionError ||
    e instanceof SimulationFailedError || e instanceof ChainError || e instanceof JsonRpcError ||
    e instanceof PriceError || e instanceof TransactionError;
}

function serializeFunding(f: Funding | null): string {
  if (f === null) return "null";
  return JSON.stringify({
    ...f,
    topUps: f.topUps.map((t) => ({ ...t, amount: t.amount.toString() })),
  });
}

/** The funding stored, or null; one stored by 0.5 (`raw`, sent to the mempool) is let go: it
 * lands or doesn't by itself, and the next batch funds what's still needed. */
function parseFunding(text: string): Funding | null {
  const raw = JSON.parse(text);
  if (raw === null || raw.topUps === undefined) return null;
  return {
    ...raw,
    topUps: raw.topUps.map((t: { account: Address; amount: string; newAccount: boolean }) => ({
      ...t,
      amount: BigInt(t.amount),
    })),
  };
}
