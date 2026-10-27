/**
 * A submission's transaction: its runner and gas limit under a block's schedule, from what the
 * chain says of its call's target and its authorities, and its simulation. Many submissions'
 * reads, and their simulations, are each one batch.
 */

import {
  type Answer,
  type Call,
  type Chain,
  ChainError,
  hexOf,
  quantity,
  resultOf,
} from "./chain.ts";
import { DEPLOYER, transactionGas } from "./deployer.ts";
import { type Address, bytes, type Hex, hex } from "./evm.ts";
import type { GasSchedule } from "./forks.ts";
import { calldata, ecdsa, transferDeployment, transferGas } from "./eip3009.ts";
import {
  type Erc20,
  frameGas,
  inCallRunner,
  type Payment,
  type Runner,
  RunnerError,
} from "./runner.ts";
import { InvalidSubmissionError, type Submission, toDeploy } from "./submission.ts";
import { authority, authorizationJson, unsignedSize } from "./transactions.ts";

const DELEGATION_PREFIX = "0xef0100";
// how a node says eth_estimateGas's transaction reverts: EIP-1474's execution error, its data
// the revert data
const REVERTED = 3;
// how a node says it hasn't a block it's asked at: EIP-1474's code, and geth's message
const RESOURCE_NOT_FOUND = -32001;
const HEADER_NOT_FOUND = "header not found";

/** The submission's transaction reverts: with `revertData`. */
export class SimulationFailedError extends Error {
  constructor(readonly revertData: Uint8Array) {
    super("simulation failed");
  }
}

/** The node simulating a submission hasn't the block it was asked at yet: nothing is known of
 * whether it reverts. */
export class BlockUnseenError extends Error {
  constructor(readonly block: number) {
    super(`the nodes aren't at block ${block} yet`);
  }
}

/** A submission's runner, its transaction's gas limit under a block's schedule, and the most
 * bytes its transaction takes unsigned (what's paid beyond gas is by size); `estimated` if its
 * gas limit is its transaction's estimate (a step runs code the client chooses). */
export interface Plan {
  readonly runner: Runner;
  readonly gasLimit: number;
  readonly size: number;
  readonly estimated: boolean;
}

/** A submission's transaction, as planned, from `sender`. */
export interface Simulation {
  readonly submission: Submission;
  readonly plan: Plan;
  readonly sender: Address;
}

/** The submission's runner and gas limit under `schedule`, paying in its token as `tokens`
 * reads it, sent by `sender`; a transfer's gas bounded knowing which tokens the payee holds some
 * of. */
export async function makePlan(
  chain: Chain,
  submission: Submission,
  schedule: GasSchedule,
  tokens: ReadonlyMap<Address, Erc20 | null>,
  sender: Address,
  payeeHolds: ReadonlySet<Address> = new Set(),
): Promise<Plan> {
  return settled((await makePlans(chain, [submission], schedule, tokens, sender, payeeHolds))[0]);
}

/** A submission as read, its plan at `index` of makePlans'; `prepare` to call its wrapped
 * signature's factory however its payer's code reads. */
interface Draft {
  readonly index: number;
  readonly submission: Submission;
  readonly authorities: readonly Address[];
  readonly delegated: ReadonlySet<Address>;
  readonly empty: number;
  readonly payerHasCode: boolean;
  readonly erc20: Erc20 | null;
  readonly payeeHolds: boolean;
  readonly prepare: boolean;
}

/**
 * Each submission's runner and gas limit, under `schedule`, sent by `sender`, their reads in one
 * batch (its call's target's code; each authority's balance, nonce and code; a payer's code):
 * each its plan, or why not.
 *
 * A runner whose steps are all the relay's, a key's transfer among them (bounded from its
 * token's measurement), has its gas limit from the gas schedule. One with a step that runs code
 * the client chooses (a deployment, or a contract payer's transfer: a payer with code, or a
 * signature that isn't a key's) has it from one eth_estimateGas of its transaction as it will be
 * sent, in a second batch: an estimate that reverts is the submission's simulation failing. A
 * wrapped signature from a payer with code is estimated first without its deployment, and only
 * if that reverts with it, to prepare the payer (ERC-6492).
 */
export async function makePlans(
  chain: Chain,
  submissions: readonly Submission[],
  schedule: GasSchedule,
  tokens: ReadonlyMap<Address, Erc20 | null>,
  sender: Address,
  payeeHolds: ReadonlySet<Address> = new Set(),
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
      if (s.transfer !== undefined) {
        calls.push({ method: "eth_getCode", params: [s.transfer.payer, "latest"] });
      }
    }
    return first;
  });
  const answers = await chain.batch(calls);
  const drafts: { draft: Draft; plan: Plan }[] = [];
  const plans = submissions.map((submission, index): PromiseSettledResult<Plan> => {
    try {
      const of = authorities[index];
      const first = firsts[index];
      if (of === undefined || first === undefined) throw new ChainError("no plan read");
      if (!Array.isArray(of)) throw of;
      const code = answers[first];
      if (code === undefined) throw new ChainError("no plan read");
      const delegated = new Set(of);
      if (hexOf(resultOf(code)).startsWith(DELEGATION_PREFIX)) {
        delegated.add(submission.call.to);
      }
      const empty = countEmpty(answers, first + 1, of.length);
      if (!tokens.has(submission.token)) {
        throw new InvalidSubmissionError("payment", `${submission.token} is suspended`);
      }
      let payerHasCode = false;
      if (submission.transfer !== undefined) {
        const payerCode = answers[first + 1 + 3 * of.length];
        if (payerCode === undefined) throw new ChainError("no plan read");
        payerHasCode = !["0x", "0x0"].includes(hexOf(resultOf(payerCode)));
      }
      const draft: Draft = {
        index,
        submission,
        authorities: of,
        delegated,
        empty,
        payerHasCode,
        erc20: tokens.get(submission.token) ?? null,
        payeeHolds: payeeHolds.has(submission.token),
        prepare: false,
      };
      const plan = planned(chain.chainId, draft, schedule);
      if (plan.estimated) drafts.push({ draft, plan });
      return { status: "fulfilled", value: plan };
    } catch (reason) {
      return { status: "rejected", reason };
    }
  });
  await estimate(chain, drafts, plans, schedule, sender);
  return plans;
}

/** The draft's runner, and its gas limit from the gas schedule: for one with a step that runs
 * code the client chooses, only a placeholder until it's estimated. */
function planned(chainId: number, draft: Draft, schedule: GasSchedule): Plan {
  const { submission, erc20 } = draft;
  let deployment = toDeploy(submission);
  let payment: Payment = { token: erc20, payee: submission.payee, amount: submission.amount };
  let contractPayer = false;
  if (submission.transfer !== undefined) {
    if (erc20 === null || erc20.transferGas === undefined) {
      throw new InvalidSubmissionError(
        "payment",
        `${submission.token} takes no EIP-3009 transfer`,
      );
    }
    const signed = submission.transfer;
    const wrapperDeployment = transferDeployment(signed);
    contractPayer = !ecdsa(signed) || draft.payerHasCode || wrapperDeployment !== null;
    const gas = contractPayer ? null : transferGas(erc20.transferGas, schedule, draft.payeeHolds);
    payment = { ...payment, transfer: { calldata: calldata(signed), gas } };
    // a wrapped signature's payer that has code: deployed, so prepared only if asked to
    if (wrapperDeployment !== null && draft.payerHasCode && !draft.prepare) {
      deployment = submission.deployment ?? null;
    }
  }
  let runner: Runner;
  try {
    runner = inCallRunner(submission.call, payment, schedule, submission.gas, {
      delegated: draft.delegated,
      deployment,
      prepare: draft.prepare,
    });
  } catch (e) {
    if (e instanceof RunnerError) throw new InvalidSubmissionError("call", e.message);
    throw e;
  }
  const gasLimit = transactionGas(
    runner.initcode,
    frameGas(runner, submission.gas),
    schedule,
    draft.authorities.length,
    draft.empty,
  );
  const size = unsignedSize(chainId, DEPLOYER, runner.initcode, 0n, draft.authorities.length);
  return { runner, gasLimit, size, estimated: deployment !== null || contractPayer };
}

/**
 * Each draft's plan, put in `plans` at its index, given its gas limit by one eth_estimateGas of
 * its transaction, in one batch: one that reverts is its simulation failing, unless a wrapped
 * signature's payer may need preparing, which is then estimated again, prepared.
 */
async function estimate(
  chain: Chain,
  drafts: readonly { draft: Draft; plan: Plan }[],
  plans: PromiseSettledResult<Plan>[],
  schedule: GasSchedule,
  sender: Address,
): Promise<void> {
  if (drafts.length === 0) return;
  const at = chain.seen > 0 ? `0x${chain.seen.toString(16)}` : "latest";
  const answers = await chain.batch(drafts.map(({ draft, plan }) => ({
    method: "eth_estimateGas",
    params: [transaction(draft.submission, plan.runner, sender), at],
  })));
  const again: { draft: Draft; plan: Plan }[] = [];
  for (const [i, { draft, plan }] of drafts.entries()) {
    const answer = answers[i];
    if (answer === undefined) {
      plans[draft.index] = { status: "rejected", reason: new ChainError("no estimate") };
      continue;
    }
    if (!("error" in answer)) {
      plans[draft.index] = {
        status: "fulfilled",
        value: { ...plan, gasLimit: Number(quantity(answer.result)) },
      };
      continue;
    }
    if (answer.error.code !== REVERTED) {
      plans[draft.index] = { status: "rejected", reason: answer.error };
      continue;
    }
    const signed = draft.submission.transfer;
    if (
      !draft.prepare && signed !== undefined && transferDeployment(signed) !== null &&
      draft.payerHasCode
    ) {
      const prepared = { ...draft, prepare: true };
      again.push({ draft: prepared, plan: planned(chain.chainId, prepared, schedule) });
    } else {
      const data = answer.error.data;
      plans[draft.index] = {
        status: "rejected",
        reason: new SimulationFailedError(
          bytes(typeof data === "string" && data.startsWith("0x") ? data as Hex : "0x"),
        ),
      };
    }
  }
  await estimate(chain, again, plans, schedule, sender);
}

/** The submission's transaction with `runner`, from `sender`, as eth_call and eth_estimateGas
 * take it: no fees, so no balance is needed; at `gasLimit`, unless it's being estimated (a
 * limit would cap the estimate's search). */
function transaction(
  submission: Submission,
  runner: Runner,
  sender: Address,
  gasLimit?: number,
): Record<string, unknown> {
  const tx: Record<string, unknown> = { from: sender, to: DEPLOYER, data: hex(runner.initcode) };
  if (gasLimit !== undefined) tx.gas = `0x${gasLimit.toString(16)}`;
  if (submission.authorizations.length > 0) {
    tx.authorizationList = submission.authorizations.map(authorizationJson);
  }
  return tx;
}

/** How many of `authorities` are empty (no balance, no nonce, no code), read in one batch. */
export async function empty(chain: Chain, authorities: readonly Address[]): Promise<number> {
  const answers = await chain.batch(authorities.flatMap(authorityReads));
  return countEmpty(answers, 0, authorities.length);
}

/** The submission's transaction as eth_call from `sender`: throw its revert data. A node
 * without the block is passed over for another, as many times as there are nodes:
 * BlockUnseenError if none had it. */
export async function simulate(
  chain: Chain,
  submission: Submission,
  plan: Plan,
  sender: Address,
): Promise<void> {
  let revert: Uint8Array | BlockUnseenError | null | undefined = new BlockUnseenError(chain.seen);
  for (const _ of chain.nodes()) {
    [revert] = await simulations(chain, [{ submission, plan, sender }]);
    if (!(revert instanceof BlockUnseenError)) break;
  }
  if (revert instanceof BlockUnseenError) throw revert;
  if (revert !== null && revert !== undefined) {
    throw new SimulationFailedError(revert);
  }
}

/** Each submission's transaction as eth_call from its sender, in one batch, at the highest block
 * the chain has seen (a node behind it answers that it hasn't the block, never an older state):
 * each its revert data, null if it doesn't revert, or BlockUnseenError if the node hadn't the
 * block. */
export async function simulations(
  chain: Chain,
  sims: readonly Simulation[],
): Promise<(Uint8Array | BlockUnseenError | null)[]> {
  const at = chain.seen > 0 ? `0x${chain.seen.toString(16)}` : "latest";
  const answers = await chain.batch(sims.map(({ submission, plan, sender }) => ({
    method: "eth_call",
    params: [transaction(submission, plan.runner, sender, plan.gasLimit), at],
  })));
  return answers.map((answer) => {
    if (unseen(answer)) return new BlockUnseenError(chain.seen);
    // the deployer answers the runner's address: anything else (an address without code answers
    // nothing) ran no runner, so checked no payment
    if (!("error" in answer)) {
      const deployed = typeof answer.result === "string" && answer.result.length === 2 + 64;
      return deployed ? null : new Uint8Array();
    }
    const data = answer.error.data;
    return bytes(typeof data === "string" && data.startsWith("0x") ? data as Hex : "0x");
  });
}

/** Whether a node answered an eth_call that it hasn't the block asked at: EIP-1474's resource
 * not found (-32001), geth's "header not found", or no result at all (Sepolia's nodes,
 * 2026-10-26: publicnode and Tenderly the first, ethpandaops the last). A revert is another
 * error (code 3). */
function unseen(answer: Answer): boolean {
  if (!("error" in answer)) return answer.result === null || answer.result === undefined;
  return answer.error.code === RESOURCE_NOT_FOUND || answer.error.message === HEADER_NOT_FOUND;
}

/** A plan that was made: why not, raised. */
export function settled<T>(result: PromiseSettledResult<T> | undefined): T {
  if (result === undefined) throw new ChainError("no result");
  if (result.status === "rejected") throw result.reason;
  return result.value;
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
