/**
 * A submission's simulation: its plan's transactions run as planned, one eth_simulateV1 each,
 * every transaction in the state the one before left, and what its answer says (the first
 * transaction to fail, its revert data; a node without the block asked at). Many submissions'
 * simulations are one batch (bruno/simulation.py).
 */

import { type Chain, ChainError, dataOf, quantity, unseen } from "./chain.ts";
import { type Address, hexQuantity } from "./evm.ts";
import type { Answer, Call } from "./jsonrpc.ts";
import {
  isOwn,
  type Plan,
  planOwn,
  signedJson,
  SimulationFailedError,
  transactionJson,
} from "./plan.ts";
import { type Refused, refused } from "./refusals.ts";
import { isSponsorship, type Sponsorship } from "./sponsorship.ts";
import { InvalidSubmissionError, type Submission } from "./submission.ts";
import type { Fees } from "./transactions.ts";

/** The node simulating a submission hasn't the block it was asked at yet: nothing is known of
 * whether it reverts. */
export class BlockUnseenError extends Error {
  readonly refused: Refused;

  constructor(readonly block: number) {
    const why = refused("block-unseen", { block });
    super(String(why));
    this.refused = why;
  }
}

/** A submission's transactions, as planned, from `sender`. */
/** A submission's transactions to simulate, and how: what each kind asks eth_simulateV1
 * (bruno/simulation.py's Simulating). */
export interface Simulating {
  readonly plan: Plan;
  /** Its eth_simulateV1, at the block `at`. */
  request(at: string): Call;
}

/** A submission's transactions as planned, the relay's from `sender`: no fees or nonces, nothing
 * validated, as when ranking a batch or taking a submission, before an account is chosen.
 * Another account's are as it signed them. */
export class Simulation implements Simulating {
  constructor(
    readonly submission: Submission | Sponsorship,
    readonly plan: Plan,
    readonly sender: Address,
  ) {}

  request(at: string): Call {
    const calls = this.plan.steps.map((step) =>
      isOwn(step) ? transactionJson(step, this.sender, step.gasLimit) : signedJson(step.transaction)
    );
    const sending = planOwn(this.plan).reduce((total, step) => total + step.transaction.value, 0n);
    // the sender is only stood in for the account that will send them: it holds what they
    // send, whatever it holds on the chain
    const block = sending > 0n
      ? { stateOverrides: { [this.sender]: { balance: hexQuantity(sending) } }, calls }
      : { calls };
    return { method: "eth_simulateV1", params: [{ blockStateCalls: [block] }, at] };
  }
}

/** A submission's transactions as they'll be signed: the relay's from `sender`, its account, at
 * its consecutive nonces from `nonce`, at `fees`, and another account's as it signed them;
 * validated as a block takes them (each nonce, fee and balance). */
export class SignedSimulation implements Simulating {
  constructor(
    readonly submission: Submission | Sponsorship,
    readonly plan: Plan,
    readonly sender: Address,
    readonly nonce: number,
    readonly fees: Fees,
  ) {}

  request(at: string): Call {
    let nonce = this.nonce;
    const calls = this.plan.steps.map((step) => {
      if (!isOwn(step)) return signedJson(step.transaction);
      return {
        ...transactionJson(step, this.sender, step.gasLimit),
        nonce: hexQuantity(nonce++),
        maxFeePerGas: hexQuantity(this.fees.maxFeePerGas),
        maxPriorityFeePerGas: hexQuantity(this.fees.maxPriorityFeePerGas),
      };
    });
    return {
      method: "eth_simulateV1",
      params: [{ blockStateCalls: [{ calls }], validation: true }, at],
    };
  }
}

/** eth_simulateV1's error for a transaction whose sender can't pay its gas and value
 * (execution-apis' -38014, as anvil answers it): it answers the whole simulation. */
const INSUFFICIENT_FUNDS = -38014;

/** A node answered a simulation that one of its transactions' senders can't pay its gas and
 * value, in the state the transactions before it left. */
export class UnfundedError extends ChainError {}

/** The submission's transactions simulated from `sender`: throw the revert data of the first that
 * reverts. A node without the block is passed over for another (Chain.batch): BlockUnseenError
 * if none had it. A sponsorship one of whose transactions can't be paid for is the client's to
 * hear of, not a node failing: the relay's own are given what they send, so it's the account's.
 * If the relay's first transaction fails run alone (the account's code failing as it's sent its
 * ETH, or given too little gas), that's its revert; otherwise the account's transactions cost
 * more than it holds and is sent (InvalidSubmissionError). */
export async function simulate(
  chain: Chain,
  submission: Submission | Sponsorship,
  plan: Plan,
  sender: Address,
): Promise<void> {
  const [revert] = await simulations(chain, [new Simulation(submission, plan, sender)]);
  const [first] = planOwn(plan);
  if (revert instanceof UnfundedError && isSponsorship(submission) && first !== undefined) {
    const funding = transactionJson(first, sender, first.gasLimit);
    await gasUsed(chain, [funding], new Map([[sender, first.transaction.value]]));
    throw new InvalidSubmissionError("transactions", refused("sponsored-unfunded"));
  }
  if (revert instanceof BlockUnseenError || revert instanceof ChainError) throw revert;
  if (revert !== null && revert !== undefined) {
    throw new SimulationFailedError(revert);
  }
}

/** A simulated call's answer: its status, its return data, and its error's data, if any. */
interface SimulatedCall {
  readonly status: unknown;
  readonly returnData: unknown;
  readonly error?: { readonly data?: unknown } | null;
  readonly gasUsed: unknown;
}

/**
 * Each submission's transactions simulated, one eth_simulateV1 each (its transactions one after
 * another, each in the state the one before left), as its kind asks (as planned, or as signed),
 * in one batch, at the highest block the chain has seen (a node behind it answers that it hasn't
 * the block, never an older state): each its first revert's data, null if every transaction did
 * what it's sent for (as its proof reads it), BlockUnseenError if the node hadn't the block, or
 * ChainError if its answer isn't one (a transaction as signed that a block wouldn't take among
 * them: its nonce, fee or balance).
 */
export async function simulations(
  chain: Chain,
  sims: readonly Simulating[],
): Promise<(Uint8Array | BlockUnseenError | ChainError | null)[]> {
  const at = chain.seenTag;
  const answers = await chain.batch(sims.map((sim) => sim.request(at)));
  return answers.map((answer, i) => {
    try {
      const sim = sims[i];
      if (sim === undefined) throw new ChainError("no simulation");
      return firstRevert(chain, sim.plan, answer);
    } catch (e) {
      if (!(e instanceof ChainError)) throw e;
      return e; // a node's malformed answer: this simulation's alone
    }
  });
}

/** Whether `transaction`, a call as eth_simulateV1 takes it, fails simulated alone at `block`:
 * reverting, or running out of its gas. BlockUnseenError if no node had the block; ChainError if
 * a node's answer isn't a simulation's. */
/**
 * The gas each of `calls` uses, run one after another at the highest block seen, each in the
 * state the one before left, nothing validated; the accounts in `holding` given those balances
 * first. SimulationFailedError, with its revert data, for the first that fails; BlockUnseenError
 * if no node had the block.
 */
export async function gasUsed(
  chain: Chain,
  calls: readonly Record<string, unknown>[],
  holding: ReadonlyMap<Address, bigint>,
): Promise<number[]> {
  const stateOverrides = Object.fromEntries(
    [...holding].map(([account, wei]) => [account, { balance: hexQuantity(wei) }]),
  );
  const block = holding.size > 0 ? { stateOverrides, calls } : { calls };
  const request: Call = {
    method: "eth_simulateV1",
    params: [{ blockStateCalls: [block] }, chain.seenTag],
  };
  const [answer] = await chain.batch([request]);
  if (answer === undefined) throw new ChainError("eth_simulateV1: no answer");
  if (unseen(answer)) throw new BlockUnseenError(chain.seen);
  if ("error" in answer) {
    throw new ChainError(`eth_simulateV1: ${answer.error.code} ${answer.error.nodeMessage}`);
  }
  const made = simulatedCalls(answer.result);
  if (made.length !== calls.length) {
    throw new ChainError(`eth_simulateV1: ${made.length} calls, ${calls.length} sent`);
  }
  for (const call of made) {
    if (quantity(call.status) !== 1n) {
      const data = call.error?.data;
      throw new SimulationFailedError(
        data === undefined || data === null ? new Uint8Array() : dataOf(data),
      );
    }
  }
  return made.map((call) => Number(quantity(call.gasUsed)));
}

export async function fails(
  chain: Chain,
  transaction: Record<string, unknown>,
  block: number,
): Promise<boolean> {
  const request: Call = {
    method: "eth_simulateV1",
    params: [{ blockStateCalls: [{ calls: [transaction] }] }, hexQuantity(block)],
  };
  const [answer] = await chain.batch([request]);
  if (answer === undefined) throw new ChainError("eth_simulateV1: no answer");
  if (unseen(answer)) throw new BlockUnseenError(block);
  if ("error" in answer) {
    throw new ChainError(`eth_simulateV1: ${answer.error.code} ${answer.error.nodeMessage}`);
  }
  const calls = simulatedCalls(answer.result);
  const [call] = calls;
  if (calls.length !== 1 || call === undefined) {
    throw new ChainError(`eth_simulateV1: ${calls.length} calls, 1 sent`);
  }
  return quantity(call.status) !== 1n;
}

/** A simulation's answer: the first of `plan`'s transactions to fail, its revert data (a call
 * failing, or doing other than what it's sent for, as its proof reads its result); null if all
 * went through; or BlockUnseenError if its node hadn't the block. An error answering the whole
 * simulation isn't a revert, but the node failing (ChainError). */
function firstRevert(
  chain: Chain,
  plan: Plan,
  answer: Answer,
): Uint8Array | BlockUnseenError | null {
  if (unseen(answer)) return new BlockUnseenError(chain.seen);
  if ("error" in answer) {
    const Failed = answer.error.code === INSUFFICIENT_FUNDS ? UnfundedError : ChainError;
    throw new Failed(`eth_simulateV1: ${answer.error.code} ${answer.error.nodeMessage}`);
  }
  const calls = simulatedCalls(answer.result);
  if (calls.length !== plan.steps.length) {
    throw new ChainError(`eth_simulateV1: ${calls.length} calls, ${plan.steps.length} sent`);
  }
  for (const [i, step] of plan.steps.entries()) {
    const call = calls[i];
    if (call === undefined) throw new ChainError("eth_simulateV1: a call missing");
    if (quantity(call.status) !== 1n) {
      const data = call.error?.data;
      return data === undefined || data === null ? new Uint8Array() : dataOf(data);
    }
    // another account's did what it's for if it didn't fail
    if (!isOwn(step)) continue;
    const reverted = step.proof.revertOf(call.returnData);
    if (reverted !== null) return reverted;
  }
  return null;
}

/** The calls of eth_simulateV1's one simulated block: ChainError if `result` isn't one. */
function simulatedCalls(result: unknown): readonly SimulatedCall[] {
  if (!Array.isArray(result) || result.length !== 1) {
    throw new ChainError(`eth_simulateV1: not one block: ${JSON.stringify(result)}`);
  }
  const block: unknown = result[0];
  const calls = typeof block === "object" && block !== null && "calls" in block
    ? block.calls
    : undefined;
  if (!Array.isArray(calls)) throw new ChainError("eth_simulateV1: a block without its calls");
  return calls.map((call: unknown): SimulatedCall => {
    if (typeof call !== "object" || call === null || !("status" in call)) {
      throw new ChainError(`eth_simulateV1: not a call's result: ${JSON.stringify(call)}`);
    }
    const error = "error" in call && typeof call.error === "object" ? call.error : null;
    return {
      status: call.status,
      returnData: "returnData" in call ? call.returnData : undefined,
      gasUsed: "gasUsed" in call ? call.gasUsed : "0x0",
      error: error === null ? null : { data: "data" in error ? error.data : undefined },
    };
  });
}
