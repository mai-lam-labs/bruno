/**
 * Each batch and each block on one chain: the queue's best mapped onto concurrency accounts,
 * funded from the float, and their transactions signed and sent each block, and what was sent
 * reconciled with its receipts; the batch and the funding in flight kept in the Store. As
 * bruno/batches.py's.
 */

import { derive } from "./accounts.ts";
import { type Builder, sendBundle } from "./builders.ts";
import { bundles, signFunding, type TopUp } from "./bundle.ts";
import {
  type Chain,
  ChainError,
  lastingFees,
  type Latest,
  type Receipt,
  receiptOf,
} from "./chain.ts";
import { JsonRpcError, resultOf } from "./jsonrpc.ts";
import type { ChainConfig } from "./config.ts";
import { DEPLOYER } from "./deployer.ts";
import { type Address, bytes, type Hex, hex, NATIVE_ASSET } from "./evm.ts";
import { Fraction } from "./fraction.ts";
import {
  BlockUnseenError,
  makePlans,
  type Plan,
  settled,
  SimulationFailedError,
  simulations,
} from "./plan.ts";
import { type Denomination, monitor, survey } from "./maintenance.ts";
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
    this.funding = JSON.parse(store.get("funding") ?? "null");
  }

  /** Whether the float's funding transaction is out, not yet landed or given up on. */
  get fundingInFlight(): boolean {
    return this.funding !== null;
  }

  private get payee(): Address {
    return this.config.payee;
  }

  /** One submission's work failing, the rest going on: what it may meet logged; anything else a
   * bug of ours, logged and listed with the chain's problems, so it's seen. */
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
   * funding for those that need it (unless a funding transaction is in flight: they wait for
   * the next batch). Nothing queued, nothing read. The queue's reads, its simulations and the
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
    this.queue.prune();
    await this.reconcile(target);
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
    if (slots.length === 0) return; // nothing to send: nothing more read
    const measured = await this.measuring.measurements();
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
        this.queue.save(tracked);
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
    const pending = this.queue.pending();
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
      this.queue.finish(tracked, "rejected", "simulation failed");
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
