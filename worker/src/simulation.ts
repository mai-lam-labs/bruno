/**
 * A submission's simulation: its plan's transactions run as planned, one eth_simulateV1 each,
 * every transaction in the state the one before left, and what its answer says (the first
 * transaction to fail, its revert data; a node without the block asked at). Many submissions'
 * simulations are one batch (bruno/simulation.py).
 */

import { type Chain, ChainError, dataOf, quantity } from "./chain.ts";
import { type Address, hexQuantity } from "./evm.ts";
import type { Answer, Call } from "./jsonrpc.ts";
import { type Plan, SimulationFailedError, transactionJson } from "./plan.ts";
import { message } from "./refusals.ts";
import type { Submission } from "./submission.ts";
import type { Fees } from "./transactions.ts";

// how a node says it hasn't a block it's asked at: EIP-1474's code, geth's message, and the
// start of anvil's (a block past its head, as after its snapshot's reverted)
const RESOURCE_NOT_FOUND = -32001;
const HEADER_NOT_FOUND = "header not found";
const BLOCK_OUT_OF_RANGE = "BlockOutOfRangeError";

/** The node simulating a submission hasn't the block it was asked at yet: nothing is known of
 * whether it reverts. */
export class BlockUnseenError extends Error {
  constructor(readonly block: number) {
    super(message("block-unseen", { block }));
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

/** A submission's transactions as planned, from `sender`: no fees or nonces, nothing validated,
 * as when ranking a batch or taking a submission, before an account is chosen. */
export class Simulation implements Simulating {
  constructor(
    readonly submission: Submission,
    readonly plan: Plan,
    readonly sender: Address,
  ) {}

  request(at: string): Call {
    const calls = this.plan.steps.map((step) =>
      transactionJson(this.submission, step.transaction, this.sender, step.gasLimit)
    );
    return { method: "eth_simulateV1", params: [{ blockStateCalls: [{ calls }] }, at] };
  }
}

/** A submission's transactions as they'll be signed: from `sender`, its account, at its
 * consecutive nonces from `nonce`, at `fees`, validated as a block takes them (each nonce, fee
 * and balance). */
export class SignedSimulation implements Simulating {
  constructor(
    readonly submission: Submission,
    readonly plan: Plan,
    readonly sender: Address,
    readonly nonce: number,
    readonly fees: Fees,
  ) {}

  request(at: string): Call {
    const calls = this.plan.steps.map((step, i) => ({
      ...transactionJson(this.submission, step.transaction, this.sender, step.gasLimit),
      nonce: hexQuantity(this.nonce + i),
      maxFeePerGas: hexQuantity(this.fees.maxFeePerGas),
      maxPriorityFeePerGas: hexQuantity(this.fees.maxPriorityFeePerGas),
    }));
    return {
      method: "eth_simulateV1",
      params: [{ blockStateCalls: [{ calls }], validation: true }, at],
    };
  }
}

/** The submission's transactions simulated from `sender`: throw the revert data of the first that
 * reverts. A node without the block is passed over for another, as many times as there are
 * nodes: BlockUnseenError if none had it. */
export async function simulate(
  chain: Chain,
  submission: Submission,
  plan: Plan,
  sender: Address,
): Promise<void> {
  let revert: Uint8Array | BlockUnseenError | ChainError | null | undefined = new BlockUnseenError(
    chain.seen,
  );
  for (const _ of chain.nodes()) {
    [revert] = await simulations(chain, [new Simulation(submission, plan, sender)]);
    if (!(revert instanceof BlockUnseenError)) break;
  }
  if (revert instanceof BlockUnseenError || revert instanceof ChainError) throw revert;
  if (revert !== null && revert !== undefined) {
    throw new SimulationFailedError(revert);
  }
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
    throw new ChainError(`eth_simulateV1: ${answer.error.code} ${answer.error.nodeMessage}`);
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
    const reverted = step.proof.revertOf(call.returnData);
    if (reverted !== null) return reverted;
  }
  return null;
}

/** A simulated call's answer: its status, its return data, and its error's data, if any. */
interface SimulatedCall {
  readonly status: unknown;
  readonly returnData: unknown;
  readonly error?: { readonly data?: unknown } | null;
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
      error: error === null ? null : { data: "data" in error ? error.data : undefined },
    };
  });
}

/** Whether a node answered a simulation or an estimate that it hasn't the block asked at: EIP-1474's resource
 * not found (-32001), geth's "header not found", anvil's BlockOutOfRangeError, or no result at
 * all (Sepolia's nodes, 2026-10-26: publicnode and Tenderly the first, ethpandaops the last). A revert is another
 * error (code 3). */
function unseen(answer: Answer): boolean {
  if (!("error" in answer)) return answer.result === null || answer.result === undefined;
  const message = answer.error.nodeMessage;
  return answer.error.code === RESOURCE_NOT_FOUND || message === HEADER_NOT_FOUND ||
    message.startsWith(BLOCK_OUT_OF_RANGE);
}
