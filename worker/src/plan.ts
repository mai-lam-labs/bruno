/**
 * A submission's transaction, by its proof of payment (the runner measuring it, or the
 * EntryPoint's own rules), and its gas limit under a block's schedule, from what the chain says
 * of its call's target and its authorities; and its simulation. Many submissions' reads, and
 * their simulations, are each one batch.
 */

import {
  type Chain,
  ChainError,
  codeOf,
  dataOf,
  hasCode,
  quantity,
  revertData,
  wordBytes,
} from "./chain.ts";
import { type Answer, type Call, resultOf, REVERTED } from "./jsonrpc.ts";
import { lookup } from "./measurements.ts";
import { DEPLOYER, runnerGasWithin, transactionGas } from "./deployer.ts";
import type { Deployment } from "./erc6492.ts";
import { selector } from "./abi.ts";
import { type Address, type EncodedCall, hex, hexQuantity, WARM_ACCOUNT_ACCESS } from "./evm.ts";
import { authorizationsGas, type GasSchedule, intrinsic } from "./forks.ts";
import { transferCalldata, transferDeployment, transferEcdsa, transferGas } from "./eip3009.ts";
import {
  BALANCE_UNREADABLE,
  clientGasWithin,
  type Erc20,
  estimatingRunner,
  frameGas,
  GAS_EXHAUSTED,
  inCallRunner,
  type Payment,
  type Runner,
  RunnerError,
  UNDERPAID,
} from "./runner.ts";
import {
  authorities as authoritiesOf,
  InvalidSubmissionError,
  type Submission,
  toDeploy,
} from "./submission.ts";
import {
  authorizationJson,
  cost,
  type Fees,
  type SignedAuthorization,
  unsignedSize,
} from "./transactions.ts";
import {
  type EntryPoint,
  entryPointAt,
  FAILED_OP,
  type FeePayment,
  handleOpsBeneficiary,
  paidAt,
} from "./userops.ts";
import { message } from "./refusals.ts";

const DELEGATION_PREFIX = "0xef0100";
// how a node says it hasn't a block it's asked at: EIP-1474's code, geth's message, and the
// start of anvil's (a block past its head, as after its snapshot's reverted)
const RESOURCE_NOT_FOUND = -32001;
const HEADER_NOT_FOUND = "header not found";
const BLOCK_OUT_OF_RANGE = "BlockOutOfRangeError";

/** The submission's transaction reverts: with `revertData`, what it says read for its message
 * (revertReason). */
export class SimulationFailedError extends Error {
  constructor(readonly revertData: Uint8Array) {
    super(message("simulation-failed", { reason: revertReason(revertData) }));
  }
}

// Solidity's revert reason and panic
const ERROR = hex(selector("Error(string)"));
const PANIC = hex(selector("Panic(uint256)"));

/** What a simulation's revert data says, and what to do next: Solidity's revert reason or panic,
 * the runner's own errors, the EntryPoint's FailedOp; anything else by its selector. */
export function revertReason(data: Uint8Array): string {
  const head = hex(data.subarray(0, 4));
  if (head === hex(UNDERPAID)) return message("revert-underpaid");
  if (head === hex(GAS_EXHAUSTED)) return message("revert-gas-exhausted");
  if (head === hex(BALANCE_UNREADABLE)) return message("revert-balance-unreadable");
  const failed = head === FAILED_OP ? stringAt(data, 1) : null;
  if (failed !== null) return message("revert-failed-op", { text: failed });
  const text = head === ERROR ? stringAt(data, 0) : null;
  if (text !== null) return message("revert-error", { text });
  if (head === PANIC && data.length === 36) {
    return message("revert-panic", { code: BigInt(hex(data.subarray(4))).toString(16) });
  }
  if (data.length === 0) return message("revert-empty");
  return message("revert-other", { selector: head });
}

/** The ABI-encoded string whose offset is argument `index` of `data`'s arguments, as UTF-8; null
 * if it doesn't decode. */
function stringAt(data: Uint8Array, index: number): string | null {
  const args = data.subarray(4);
  if (args.length < 32 * (index + 1)) return null;
  const offset = BigInt(hex(args.subarray(32 * index, 32 * index + 32)));
  if (offset + 32n > BigInt(args.length)) return null;
  const start = Number(offset) + 32;
  const length = BigInt(hex(args.subarray(start - 32, start)));
  if (BigInt(start) + length > BigInt(args.length)) return null;
  try {
    return new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(
      args.subarray(start, start + Number(length)),
    );
  } catch {
    return null;
  }
}

/** The node simulating a submission hasn't the block it was asked at yet: nothing is known of
 * whether it reverts. */
export class BlockUnseenError extends Error {
  constructor(readonly block: number) {
    super(message("block-unseen", { block }));
  }
}

/** One transaction of a plan, as its concurrency account makes it: its `transaction` (target,
 * calldata and the wei it sends), its result read by its `proof`; its gas limit under a block's
 * schedule, and the most bytes it takes unsigned (what's paid beyond gas is by size). */
export interface Step {
  readonly transaction: EncodedCall;
  readonly proof: Proof;
  readonly gasLimit: number;
  readonly size: number;
}

/** The most `step` costs its sender at `fees`: its gas, the wei it sends, and what's paid beyond
 * gas. */
export function stepCost(step: Step, fees: Fees): bigint {
  return cost(fees, step.gasLimit, step.size, step.transaction.value);
}

/** A submission's transactions, in order (`steps`), simulated together, one after another;
 * `estimated` if a gas limit is its transaction's estimate (a step runs code the client
 * chooses). */
export interface Plan {
  readonly steps: readonly Step[];
  readonly estimated: boolean;
}

/** `plan`'s transactions' gas limits, together. */
export function planGasLimit(plan: Plan): number {
  return plan.steps.reduce((total, step) => total + step.gasLimit, 0);
}

/** `plan`'s transactions' most bytes unsigned, together. */
export function planSize(plan: Plan): number {
  return plan.steps.reduce((total, step) => total + step.size, 0);
}

/** The most `plan`'s transactions cost at `fees`, together, each costed as its own. */
export function planCost(plan: Plan, fees: Fees): bigint {
  return plan.steps.reduce((total, step) => total + stepCost(step, fees), 0n);
}

/** `plan`'s one transaction: a submission's plan, until a plan of several is sent. */
export function onlyStep(plan: Plan): Step {
  const [step, ...rest] = plan.steps;
  if (step === undefined || rest.length > 0) {
    throw new Error(`a plan of ${plan.steps.length} transactions, not one`);
  }
  return step;
}

/** How the relay knows a submission pays it: what it pays in a block, the transaction that shows
 * it, and how that transaction's simulation reads. Whoever takes a submission says which;
 * nothing else does. */
export interface Proof {
  /** What `submission` pays the payee in a block of `baseFee`. */
  paying(submission: Submission, baseFee: bigint): bigint;
  /** `draft`'s transaction, and its gas limit from the gas schedule: if it's to be estimated,
   * only a placeholder until it is. */
  plan(chainId: number, draft: Draft, schedule: GasSchedule): Plan;
  /** A simulation of its transaction that didn't fail, its `result` (eth_call's): null if it did
   * what it's sent for, else its revert data; ChainError if it isn't an answer. */
  revertOf(result: unknown): Uint8Array | null;
  /** It, as the Store keeps it (parseProof reads it back). */
  stored(): StoredProof;
}

/** A proof as the Store keeps it: by its kind, and an op's fee payment, its amounts as strings. */
export type StoredProof =
  | { readonly kind: "runner" }
  | {
    readonly kind: "entryPoint";
    readonly entryPoint: Address;
    readonly feePayment: { gas: string; maxFeePerGas: string; maxPriorityFeePerGas: string };
  };

/** A proof the Store kept (Proof.stored), its entry point among those taken. */
export function parseProof(raw: StoredProof): Proof {
  if (raw.kind === "runner") return new MeasuredByRunner();
  return new ByEntryPoint(entryPointAt(raw.entryPoint), {
    gas: BigInt(raw.feePayment.gas),
    maxFeePerGas: BigInt(raw.feePayment.maxFeePerGas),
    maxPriorityFeePerGas: BigInt(raw.feePayment.maxPriorityFeePerGas),
  });
}

/** Paid as the runner measures it: a runner, deployed through the deployer, makes the
 * submission's transfer, if any, and its call, and reverts unless the payee gained its amount,
 * so a transaction that doesn't pay doesn't land. */
export class MeasuredByRunner implements Proof {
  paying(submission: Submission, _baseFee: bigint): bigint {
    return submission.amount;
  }

  plan(chainId: number, draft: Draft, schedule: GasSchedule): Plan {
    return runnerPlanned(chainId, draft, schedule);
  }

  /** The deployer answers the runner's address, a word; an address without code answers
   * nothing, and ran no runner, so checked no payment: a revert. */
  revertOf(result: unknown): Uint8Array | null {
    if (result === "0x") return new Uint8Array();
    wordBytes(result);
    return null;
  }

  stored(): StoredProof {
    return { kind: "runner" };
  }
}

/** A submission its proof can't be of: a bug of ours, that submission's alone. */
export class ProofError extends Error {}

/** Paid by the EntryPoint's own rules, for the relay's own handleOps of an op paying by its fees
 * (`feePayment`), the payee its beneficiary, sent as is: a handleOps that lands has paid the
 * beneficiary the op's gas at its price (its call reverting or not), and one whose op fails its
 * validation reverts, so doesn't land. A submission that isn't such a handleOps is a bug of
 * ours: ProofError, never sent. */
export class ByEntryPoint implements Proof {
  constructor(readonly entryPoint: EntryPoint, readonly feePayment: FeePayment) {}

  paying(_submission: Submission, baseFee: bigint): bigint {
    return paidAt(this.feePayment, baseFee);
  }

  plan(chainId: number, draft: Draft, schedule: GasSchedule): Plan {
    const { submission } = draft;
    const call = submission.call;
    if (call.to !== this.entryPoint.address) {
      throw new ProofError(`handleOps to ${call.to}, not EntryPoint ${this.entryPoint.version}'s`);
    }
    if (handleOpsBeneficiary(call.data) !== submission.payee) {
      throw new ProofError("a handleOps whose beneficiary isn't the payee");
    }
    return entryPointPlan(
      chainId,
      call,
      submission.gas,
      schedule,
      draft.authorities.length,
      draft.empty,
      this,
    );
  }

  /** handleOps answers nothing; an op it refuses is an error, a revert. */
  revertOf(result: unknown): Uint8Array | null {
    if (result !== "0x") throw new ChainError(`handleOps answered ${JSON.stringify(result)}`);
    return null;
  }

  stored(): StoredProof {
    return {
      kind: "entryPoint",
      entryPoint: this.entryPoint.address,
      feePayment: {
        gas: this.feePayment.gas.toString(),
        maxFeePerGas: this.feePayment.maxFeePerGas.toString(),
        maxPriorityFeePerGas: this.feePayment.maxPriorityFeePerGas.toString(),
      },
    };
  }
}

/** A submission's transaction, as planned, from `sender`. */
export interface Simulation {
  readonly submission: Submission;
  readonly plan: Plan;
  readonly sender: Address;
}

/** The submission's transaction by its `proof`, and its gas limit under `schedule`, paying in
 * its token as `tokens` reads it, sent by `sender`; a transfer's gas bounded knowing which tokens
 * the payee holds some of. */
export async function makePlan(
  chain: Chain,
  submission: Submission,
  proof: Proof,
  schedule: GasSchedule,
  tokens: ReadonlyMap<Address, Erc20 | null>,
  sender: Address,
  payeeHolds: ReadonlySet<Address> = new Set(),
): Promise<Plan> {
  return settled(
    (await makePlans(chain, [submission], [proof], schedule, tokens, sender, payeeHolds))[0],
  );
}

/** A submission as read, its plan by its `proof` at `index` of makePlans'; `prepare` to call its
 * wrapped signature's factory however its payer's code reads. */
export interface Draft {
  readonly index: number;
  readonly submission: Submission;
  readonly proof: Proof;
  readonly authorities: readonly Address[];
  readonly delegated: ReadonlySet<Address>;
  readonly empty: number;
  readonly payerHasCode: boolean;
  readonly erc20: Erc20 | null;
  readonly payeeHolds: boolean;
  readonly prepare: boolean;
}

/**
 * Each submission's transaction by its proof (`proofs`, one each), and its gas limit, under
 * `schedule`, sent by `sender`, their reads in one batch (its call's target's code; each
 * authority's balance, nonce and code; a payer's code): each its plan, or why not.
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
  proofs: readonly Proof[],
  schedule: GasSchedule,
  tokens: ReadonlyMap<Address, Erc20 | null>,
  sender: Address,
  payeeHolds: ReadonlySet<Address> = new Set(),
): Promise<PromiseSettledResult<Plan>[]> {
  if (proofs.length !== submissions.length) throw new Error("a proof for each submission");
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
      const proof = proofs[index];
      if (of === undefined || first === undefined) throw new ChainError("no plan read");
      if (proof === undefined) throw new Error("no proof");
      if (!Array.isArray(of)) throw of;
      const code = answers[first];
      if (code === undefined) throw new ChainError("no plan read");
      const delegated = new Set(of);
      if (hex(codeOf(resultOf(code))).startsWith(DELEGATION_PREFIX)) {
        delegated.add(submission.call.to);
      }
      const empty = countEmpty(answers, first + 1, of.length);
      if (!tokens.has(submission.token)) {
        throw new InvalidSubmissionError(
          "payment",
          message("token-suspended", { token: submission.token }),
        );
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
        proof,
        authorities: of,
        delegated,
        empty,
        payerHasCode,
        erc20: lookup(tokens, submission.token, "a token's measurement"),
        payeeHolds: payeeHolds.has(submission.token),
        prepare: false,
      };
      const plan = proof.plan(chain.chainId, draft, schedule);
      if (plan.estimated) drafts.push({ draft, plan });
      return { status: "fulfilled", value: plan };
    } catch (reason) {
      return { status: "rejected", reason };
    }
  });
  await estimate(chain, drafts, plans, schedule, sender);
  return plans;
}

/** The draft's runner's transaction, and its gas limit from the gas schedule: for one with a
 * step that runs code the client chooses, only a placeholder until it's estimated. */
function runnerPlanned(chainId: number, draft: Draft, schedule: GasSchedule): Plan {
  const { submission, erc20 } = draft;
  let deployment = toDeploy(submission);
  let payment: Payment = { token: erc20, payee: submission.payee, amount: submission.amount };
  let contractPayer = false;
  if (submission.transfer !== undefined) {
    if (erc20 === null || erc20.transferGas === undefined) {
      throw new InvalidSubmissionError(
        "payment",
        message("no-eip3009", { token: submission.token }),
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
  const transaction = { to: DEPLOYER, data: runner.initcode, value: 0n };
  return { steps: [{ transaction, proof: new MeasuredByRunner(), gasLimit, size }], estimated };
}

/**
 * `call`, a handleOps, sent as is, by `proof`: its transaction's gas limit from the gas schedule,
 * the call given `callGas` (handleOps' own gas, a node's estimate from its beneficiary, which the
 * EntryPoint pays last, less its intrinsic gas), with `authorizations` of which `empty` are from
 * empty accounts; and its most bytes unsigned. Sent from a concurrency account, the beneficiary
 * is cold when it's paid: its access as estimated was warm.
 */
export function entryPointPlan(
  chainId: number,
  call: EncodedCall,
  callGas: number,
  schedule: GasSchedule,
  authorizations: number,
  empty: number,
  proof: Proof,
): Plan {
  const { standard, floor } = intrinsic(schedule, call.data, { authorizations });
  const beneficiary = schedule.coldAccountAccess - WARM_ACCOUNT_ACCESS;
  const execution = callGas + beneficiary + authorizationsGas(schedule, empty, authorizations);
  const gasLimit = Math.max(standard + execution, floor);
  const size = unsignedSize(chainId, call.to, call.data, 0n, authorizations);
  return { steps: [{ transaction: call, proof, gasLimit, size }], estimated: false };
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
    params: [transaction(draft.submission, onlyStep(plan).transaction, sender), at],
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
    const step = { ...onlyStep(plan), gasLimit: Number(quantity(answer.result)) };
    const value = { ...plan, steps: [step] };
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
    return [{ draft: prepared, plan: runnerPlanned(chainId, prepared, schedule) }];
  }
  plans[draft.index] = {
    status: "rejected",
    reason: new SimulationFailedError(revertData(answer.error)),
  };
  return [];
}

/** The submission's `call`, from `sender`, as eth_call and eth_estimateGas take it: no fees, so
 * no balance is needed; at `gasLimit`, unless it's being estimated (a limit would cap the
 * estimate's search). */
function transaction(
  submission: Submission,
  call: EncodedCall,
  sender: Address,
  gasLimit?: number,
): Record<string, unknown> {
  return callJson(call, sender, submission.authorizations, gasLimit);
}

/** `call`, from `sender`, with `authorizations`, as eth_call and eth_estimateGas take it
 * (transaction). */
function callJson(
  call: EncodedCall,
  sender: Address,
  authorizations: readonly SignedAuthorization[],
  gasLimit?: number,
): Record<string, unknown> {
  const tx: Record<string, unknown> = { from: sender, to: call.to, data: hex(call.data) };
  if (call.value > 0n) tx.value = hexQuantity(call.value);
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
  const tx = callJson({ to: DEPLOYER, data: runner.initcode, value: 0n }, sender, authorizations);
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

/**
 * Each submission's transactions simulated from its sender, one eth_simulateV1 each (its
 * transactions one after another, each in the state the one before left), in one batch, at the
 * highest block the chain has seen (a node behind it answers that it hasn't the block, never an
 * older state): each its first revert's data, null if every transaction did what it's sent for
 * (as its proof reads it), BlockUnseenError if the node hadn't the block, or ChainError if its
 * answer isn't one. No fees or nonces checked: the transactions are simulated as planned, not as
 * signed.
 */
export async function simulations(
  chain: Chain,
  sims: readonly Simulation[],
): Promise<(Uint8Array | BlockUnseenError | ChainError | null)[]> {
  const at = chain.seenTag;
  const answers = await chain.batch(sims.map(({ submission, plan, sender }) => ({
    method: "eth_simulateV1",
    params: [
      {
        blockStateCalls: [{
          calls: plan.steps.map((step) =>
            transaction(submission, step.transaction, sender, step.gasLimit)
          ),
        }],
      },
      at,
    ],
  })));
  return answers.map((answer, i) => {
    try {
      const sim = sims[i];
      if (sim === undefined) throw new ChainError("no simulation");
      return revert(chain, sim.plan, answer);
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
function revert(chain: Chain, plan: Plan, answer: Answer): Uint8Array | BlockUnseenError | null {
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
