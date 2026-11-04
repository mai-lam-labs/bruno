/**
 * A submission's transaction: its runner and gas limit under a block's schedule, from what the
 * chain says of its call's target and its authorities, and its simulation. Many submissions'
 * reads, and their simulations, are each one batch.
 */

import {
  type Chain,
  ChainError,
  codeOf,
  hasCode,
  quantity,
  revertData,
  wordBytes,
} from "./chain.ts";
import { type Answer, type Call, resultOf, REVERTED } from "./jsonrpc.ts";
import { lookup } from "./measurements.ts";
import { DEPLOYER, runnerGasWithin, transactionGas } from "./deployer.ts";
import type { Deployment } from "./erc6492.ts";
import { type Address, type EncodedCall, hex, hexQuantity } from "./evm.ts";
import type { GasSchedule } from "./forks.ts";
import { transferCalldata, transferDeployment, transferEcdsa, transferGas } from "./eip3009.ts";
import {
  clientGasWithin,
  type Erc20,
  estimatingRunner,
  frameGas,
  inCallRunner,
  type Payment,
  type Runner,
  RunnerError,
} from "./runner.ts";
import {
  authorities as authoritiesOf,
  InvalidSubmissionError,
  type Submission,
  toDeploy,
} from "./submission.ts";
import { authorizationJson, type SignedAuthorization, unsignedSize } from "./transactions.ts";

const DELEGATION_PREFIX = "0xef0100";
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
      return authoritiesOf(s.authorizations);
    } catch (e) {
      if (e instanceof InvalidSubmissionError) return e;
      throw e;
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
      if (hex(codeOf(resultOf(code))).startsWith(DELEGATION_PREFIX)) {
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
        payerHasCode = hasCode(resultOf(payerCode));
      }
      const draft: Draft = {
        index,
        submission,
        authorities: of,
        delegated,
        empty,
        payerHasCode,
        erc20: lookup(tokens, submission.token, "a token's measurement"),
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
    contractPayer = !transferEcdsa(signed) || draft.payerHasCode || wrapperDeployment !== null;
    const gas = contractPayer ? null : transferGas(erc20.transferGas, schedule, draft.payeeHolds);
    payment = { ...payment, transfer: { calldata: transferCalldata(signed), gas } };
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
  return runnerPlan(
    chainId,
    runner,
    submission.gas,
    schedule,
    draft.authorities.length,
    draft.empty,
    {
      estimated: deployment !== null || contractPayer,
    },
  );
}

/**
 * `runner`'s plan: its transaction's gas limit from the gas schedule, its call given `clientGas`,
 * with `authorizations` of which `empty` are from empty accounts, and `extraGas` more in its
 * frame; and its transaction's most bytes unsigned.
 */
export function runnerPlan(
  chainId: number,
  runner: Runner,
  clientGas: number,
  schedule: GasSchedule,
  authorizations: number,
  empty: number,
  { extraGas = 0, estimated = false }: { extraGas?: number; estimated?: boolean } = {},
): Plan {
  const gasLimit = transactionGas(
    runner.initcode,
    frameGas(runner, clientGas) + extraGas,
    schedule,
    authorizations,
    empty,
  );
  const size = unsignedSize(chainId, DEPLOYER, runner.initcode, 0n, authorizations);
  return { runner, gasLimit, size, estimated };
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
  const at = chain.seenTag;
  const answers = await chain.batch(drafts.map(({ draft, plan }) => ({
    method: "eth_estimateGas",
    params: [transaction(draft.submission, plan.runner, sender), at],
  })));
  const again: { draft: Draft; plan: Plan }[] = [];
  for (const [i, { draft, plan }] of drafts.entries()) {
    try {
      again.push(...estimated(chain.chainId, draft, plan, answers[i], plans, schedule));
    } catch (reason) {
      // a node's malformed answer, or a bug of ours: this submission's alone
      plans[draft.index] = { status: "rejected", reason };
    }
  }
  await estimate(chain, again, plans, schedule, sender);
}

/** `draft`'s estimate `answer` read into its plan, in `plans`: its gas limit, or why it has none;
 * or, its payer's wrapped signature to be prepared first, it drafted again (returned)
 * (bruno/plan.py's _estimated). */
function estimated(
  chainId: number,
  draft: Draft,
  plan: Plan,
  answer: Answer | undefined,
  plans: PromiseSettledResult<Plan>[],
  schedule: GasSchedule,
): { draft: Draft; plan: Plan }[] {
  if (answer === undefined) throw new ChainError("no estimate");
  if (!("error" in answer)) {
    const value = { ...plan, gasLimit: Number(quantity(answer.result)) };
    plans[draft.index] = { status: "fulfilled", value };
    return [];
  }
  if (answer.error.code !== REVERTED) {
    plans[draft.index] = { status: "rejected", reason: answer.error };
    return [];
  }
  const signed = draft.submission.transfer;
  if (
    !draft.prepare && signed !== undefined && transferDeployment(signed) !== null &&
    draft.payerHasCode
  ) {
    const prepared = { ...draft, prepare: true };
    return [{ draft: prepared, plan: planned(chainId, prepared, schedule) }];
  }
  plans[draft.index] = {
    status: "rejected",
    reason: new SimulationFailedError(revertData(answer.error)),
  };
  return [];
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
  return deployerCall(runner, sender, submission.authorizations, gasLimit);
}

/** A transaction deploying `runner`, from `sender`, with `authorizations`, as eth_call and
 * eth_estimateGas take it (transaction). */
function deployerCall(
  runner: Runner,
  sender: Address,
  authorizations: readonly SignedAuthorization[],
  gasLimit?: number,
): Record<string, unknown> {
  const tx: Record<string, unknown> = { from: sender, to: DEPLOYER, data: hex(runner.initcode) };
  if (gasLimit !== undefined) tx.gas = hexQuantity(gasLimit);
  if (authorizations.length > 0) tx.authorizationList = authorizations.map(authorizationJson);
  return tx;
}

/** What a quote reads of its call's accounts: those EIP-7702 delegated in its transaction (its
 * authorities, and its target if delegated already), how many of its authorities are empty, and
 * which of them are delegated already (`delegating`). */
export interface CallAccounts {
  readonly delegated: ReadonlySet<Address>;
  readonly empty: number;
  readonly delegating: ReadonlySet<Address>;
}

/** `target`'s code, and each authority's balance, nonce and code, read in one batch. */
export async function callAccounts(
  chain: Chain,
  target: Address,
  authorities: readonly Address[],
): Promise<CallAccounts> {
  const answers = await chain.batch([
    { method: "eth_getCode", params: [target, "latest"] },
    ...authorities.flatMap(authorityReads),
  ]);
  const delegatedCode = (i: number): boolean => {
    const answer = answers[i];
    if (answer === undefined) throw new ChainError("no code read");
    return hex(codeOf(resultOf(answer))).startsWith(DELEGATION_PREFIX);
  };
  const delegated = new Set(authorities);
  if (delegatedCode(0)) delegated.add(target);
  const delegating = new Set(authorities.filter((_, i) => delegatedCode(3 + 3 * i)));
  return { delegated, empty: countEmpty(answers, 1, authorities.length), delegating };
}

/**
 * The gas `call` needs as it will run: made by a runner paying `payment`, in `sender`'s
 * transaction with `authorizations`, after `deployment` (whose factory call takes `deployGas`).
 * One eth_estimateGas of the runner less its payment's transfer and check (estimatingRunner), the
 * gas left for the call in a transaction of that estimate: the runner's own steps each at their
 * bound, as the runner sent has them, so what they don't use is the call's in both.
 * SimulationFailedError if the call reverts.
 */
export async function callGas(
  chain: Chain,
  call: EncodedCall,
  payment: Payment,
  authorizations: readonly SignedAuthorization[],
  accounts: CallAccounts,
  schedule: GasSchedule,
  sender: Address,
  deployment: Deployment | null = null,
  deployGas = 0,
): Promise<number> {
  let runner: Runner;
  try {
    runner = estimatingRunner(call, payment, schedule, {
      delegated: accounts.delegated,
      deployment,
    });
  } catch (e) {
    if (e instanceof RunnerError) throw new InvalidSubmissionError("call", e.message);
    throw e;
  }
  const tx = deployerCall(runner, sender, authorizations);
  const [answer] = await chain.batch([{ method: "eth_estimateGas", params: [tx, "latest"] }]);
  if (answer === undefined) throw new ChainError("no estimate");
  if ("error" in answer) {
    if (answer.error.code === REVERTED) {
      throw new SimulationFailedError(revertData(answer.error));
    }
    throw answer.error;
  }
  const runnerGas = runnerGasWithin(
    runner.initcode,
    Number(quantity(answer.result)),
    schedule,
    authorizations.length,
    accounts.empty,
  );
  return clientGasWithin(runnerGas - runner.overheadGas - deployGas);
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
  let revert: Uint8Array | BlockUnseenError | ChainError | null | undefined = new BlockUnseenError(
    chain.seen,
  );
  for (const _ of chain.nodes()) {
    [revert] = await simulations(chain, [{ submission, plan, sender }]);
    if (!(revert instanceof BlockUnseenError)) break;
  }
  if (revert instanceof BlockUnseenError || revert instanceof ChainError) throw revert;
  if (revert !== null && revert !== undefined) {
    throw new SimulationFailedError(revert);
  }
}

/** Each submission's transaction as eth_call from its sender, in one batch, at the highest block
 * the chain has seen (a node behind it answers that it hasn't the block, never an older state):
 * each its revert data, null if it deploys its runner (the deployer answering its address, 32
 * bytes), BlockUnseenError if the node hadn't the block, or ChainError if its answer isn't
 * one. */
export async function simulations(
  chain: Chain,
  sims: readonly Simulation[],
): Promise<(Uint8Array | BlockUnseenError | ChainError | null)[]> {
  const at = chain.seenTag;
  const answers = await chain.batch(sims.map(({ submission, plan, sender }) => ({
    method: "eth_call",
    params: [transaction(submission, plan.runner, sender, plan.gasLimit), at],
  })));
  return answers.map((answer) => {
    try {
      return revertOf(chain, answer);
    } catch (e) {
      if (!(e instanceof ChainError)) throw e;
      return e; // a node's malformed answer: this simulation's alone
    }
  });
}

/** A simulation's answer: its revert data, null if it went through, or BlockUnseenError if its
 * node hadn't the block. The deployer answers the runner's address, a word; an address without
 * code answers nothing, and ran no runner, so checked no payment; anything else isn't an
 * answer (ChainError). */
function revertOf(chain: Chain, answer: Answer): Uint8Array | BlockUnseenError | null {
  if (unseen(answer)) return new BlockUnseenError(chain.seen);
  if ("error" in answer) return revertData(answer.error);
  if (answer.result === "0x") return new Uint8Array();
  wordBytes(answer.result);
  return null;
}

/** Whether a node answered an eth_call that it hasn't the block asked at: EIP-1474's resource
 * not found (-32001), geth's "header not found", or no result at all (Sepolia's nodes,
 * 2026-10-26: publicnode and Tenderly the first, ethpandaops the last). A revert is another
 * error (code 3). */
function unseen(answer: Answer): boolean {
  if (!("error" in answer)) return answer.result === null || answer.result === undefined;
  return answer.error.code === RESOURCE_NOT_FOUND || answer.error.nodeMessage === HEADER_NOT_FOUND;
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
    if (quantity(balance) === 0n && quantity(nonce) === 0n && !hasCode(code)) {
      empty++;
    }
  }
  return empty;
}
