/**
 * A submission's transactions, by its proof of payment (the runner measuring it, the EntryPoint's
 * own rules, or its transfer sent with its call), and their gas limits under a block's schedule,
 * from what the chain says of its call's target and its authorities. The one path: a quote, the bundler's estimates
 * and a submission are each drafted (Draft), their proof chosen in one place (proofOf), and
 * planned alike (planDrafts). Many submissions' reads are one batch; their simulation is
 * simulation.ts's.
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
import { ethGas, forwarding, tokenGas } from "./forwarder.ts";
import { funding, isSponsorship, type Sponsorship } from "./sponsorship.ts";
import { direct } from "./targets.ts";
import { transferCalldata, transferDeployment, transferEcdsa, transferGas } from "./eip3009.ts";
import {
  BALANCE_UNREADABLE,
  callNeed,
  clientGasWithin,
  type Erc20,
  estimatingRunner,
  frameGas,
  GAS_EXHAUSTED,
  inCallRunner,
  type Payment,
  type Runner,
  RunnerError,
  type Transfer,
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
  type Decoded,
  type Fees,
  type SignedAuthorization,
  unsignedSize,
} from "./transactions.ts";
import {
  type EntryPoint,
  entryPointAt,
  FAILED_OP,
  FAILED_OP_WITH_REVERT,
  type FeePayment,
  handleOpsBeneficiary,
  paidAt,
} from "./userops.ts";
import { message, type Refused, refused } from "./refusals.ts";

const DELEGATION_PREFIX = "0xef0100";
/** An amount whose word has no zero byte: the dearest calldata an amount can be. */
const WIDEST_AMOUNT = (1n << 256n) - 1n;
/** The submission's transaction reverts: with `revertData`, what it says read for its message
 * (revertReason). */
export class SimulationFailedError extends Error {
  readonly refused: Refused;

  constructor(readonly revertData: Uint8Array) {
    const why = revertReason(revertData);
    super(message("simulation-failed", { reason: String(why) }));
    this.refused = why;
  }
}

// Solidity's revert reason and panic
const ERROR = hex(selector("Error(string)"));
const PANIC = hex(selector("Panic(uint256)"));

/** What a simulation's revert data says, as its refusal: Solidity's revert reason or panic, the
 * runner's own errors, the EntryPoint's FailedOp and FailedOpWithRevert (the revert inside it
 * read as any is); anything else by its selector. */
export function revertReason(data: Uint8Array): Refused {
  const head = hex(data.subarray(0, 4));
  if (head === hex(UNDERPAID)) return refused("revert-underpaid");
  if (head === hex(GAS_EXHAUSTED)) return refused("revert-gas-exhausted");
  if (head === hex(BALANCE_UNREADABLE)) return refused("revert-balance-unreadable");
  const failed = head === FAILED_OP ? stringAt(data, 1) : null;
  if (failed !== null) return refused("revert-failed-op", { text: failed });
  const reason = head === FAILED_OP_WITH_REVERT ? stringAt(data, 1) : null;
  const inner = head === FAILED_OP_WITH_REVERT ? bytesAt(data, 2) : null;
  if (reason !== null && inner !== null) {
    const said = String(revertReason(inner));
    return refused("revert-failed-op-with-revert", { text: reason, inner: said });
  }
  const text = head === ERROR ? stringAt(data, 0) : null;
  if (text !== null) return refused("revert-error", { text });
  if (head === PANIC && data.length === 36) {
    return refused("revert-panic", { code: BigInt(hex(data.subarray(4))).toString(16) });
  }
  if (data.length === 0) return refused("revert-empty");
  return refused("revert-other", { selector: head });
}

/** The ABI-encoded string whose offset is argument `index` of `data`'s arguments, as UTF-8; null
 * if it doesn't decode. */
function stringAt(data: Uint8Array, index: number): string | null {
  const raw = bytesAt(data, index);
  if (raw === null) return null;
  try {
    return new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(raw);
  } catch {
    return null;
  }
}

/** The ABI-encoded bytes (or string) whose offset is argument `index` of `data`'s arguments; null
 * if they don't decode. */
function bytesAt(data: Uint8Array, index: number): Uint8Array | null {
  const args = data.subarray(4);
  if (args.length < 32 * (index + 1)) return null;
  const offset = BigInt(hex(args.subarray(32 * index, 32 * index + 32)));
  if (offset + 32n > BigInt(args.length)) return null;
  const start = Number(offset) + 32;
  const length = BigInt(hex(args.subarray(start - 32, start)));
  if (BigInt(start) + length > BigInt(args.length)) return null;
  return args.subarray(start, start + Number(length));
}

/** What a transaction's cost is worked out from: its gas limit, its most bytes unsigned, and the
 * wei it sends (bruno/plan.py's CostBasis). */
export interface CostBasis {
  readonly gasLimit: number;
  readonly size: number;
  readonly value: bigint;
}

/** One transaction of a plan, as its concurrency account makes it: its `transaction` (target,
 * calldata and the wei it sends), its result read by its `proof`; its gas limit under a block's
 * schedule, and the most bytes it takes unsigned (what's paid beyond gas is by size); the
 * `authorizations` it carries, the submission's or none; `estimated` if its gas limit is its
 * estimate (it runs code the client chooses), only a placeholder until then. */
export interface Step {
  readonly transaction: EncodedCall;
  readonly proof: Proof;
  readonly gasLimit: number;
  readonly size: number;
  readonly authorizations: readonly SignedAuthorization[];
  readonly estimated: boolean;
}

/** The most a transaction of `basis` costs its sender at `fees`: its gas, the wei it sends, and
 * what's paid beyond gas. */
export function basisCost(basis: CostBasis, fees: Fees): bigint {
  return cost(fees, basis.gasLimit, basis.size, basis.value);
}

/** What `step`'s cost is worked out from. */
export function stepBasis(step: Step): CostBasis {
  return { gasLimit: step.gasLimit, size: step.size, value: step.transaction.value };
}

/** The most transactions of `bases` cost at `fees`, together, each costed as its own. */
export function basesCost(bases: readonly CostBasis[], fees: Fees): bigint {
  return bases.reduce((total, basis) => total + basisCost(basis, fees), 0n);
}

/**
 * One transaction of a plan that another account signed (a sponsored account's own), sent as it
 * is, in its place among the relay's. It costs the relay nothing itself: its sender pays for it,
 * out of what a step before it sent.
 */
export interface SignedStep {
  readonly transaction: Decoded;
}

/** Whether `step` is the relay's own, which it signs and pays for. */
export function isOwn(step: Step | SignedStep): step is Step {
  return "proof" in step;
}

/** A step's gas limit: the relay's as planned, another account's as it was signed. */
export function stepGasLimit(step: Step | SignedStep): number {
  return isOwn(step) ? step.gasLimit : step.transaction.signed.gas;
}

/** A submission's transactions, in order (`steps`): the relay's own (planOwn), from one
 * concurrency account at its consecutive nonces, and any another account signed, in their places;
 * simulated together, one after another. One transaction goes in the block's bundle, a builder
 * free to leave it out; several are a bundle of their own, which lands whole or not at all. */
export interface Plan {
  readonly steps: readonly (Step | SignedStep)[];
}

/** The transactions of `plan` the relay signs and pays for, in order. */
export function planOwn(plan: Plan): Step[] {
  return plan.steps.filter(isOwn);
}

/** Whether one of `plan`'s steps' gas limit is its estimate. */
export function planEstimated(plan: Plan): boolean {
  return planOwn(plan).some((step) => step.estimated);
}

/** A submission's plan, or why it has none: the client's mistake (InvalidSubmissionError), its
 * simulation failing, a node's failure (JsonRpcError, ChainError), or a bug of ours
 * (bruno/plan.py's Planned). */
export type Planned = PromiseSettledResult<Plan>;

/** `plan`'s transactions' gas limits, together, whoever signs them. */
export function planGasLimit(plan: Plan): number {
  return plan.steps.reduce((total, step) => total + stepGasLimit(step), 0);
}

/** What each of the relay's own transactions in `plan` has its cost worked out from, in order. */
export function planBasis(plan: Plan): CostBasis[] {
  return planOwn(plan).map(stepBasis);
}

/** The most `plan`'s transactions cost at `fees`, together, each costed as its own. */
export function planCost(plan: Plan, fees: Fees): bigint {
  return basesCost(planBasis(plan), fees);
}

/** How the relay knows what it has taken pays it: what it pays in a block, and how its
 * transactions' simulation reads. */
export interface Proof {
  /** What's paid to the payee in a block of `baseFee`, the `amount` having been offered. */
  paying(amount: bigint, baseFee: bigint): bigint;
  /** A simulation of one of its transactions that didn't fail, its `result` (eth_call's): null
   * if it did what it's sent for, else its revert data; ChainError if it isn't an answer. */
  revertOf(result: unknown): Uint8Array | null;
  /** It, as the Store keeps it (parseProof reads it back). */
  stored(): StoredProof;
}

/**
 * A submission's proof, and what the submission is sent as: its transactions (a runner; the
 * EntryPoint's handleOps as it is; or its call and its transfer, each sent as it is). proofOf
 * chooses it; nothing else does.
 */
export interface DraftedProof extends Proof {
  /** `draft`'s transactions, and their gas limits from the gas schedule: one to be estimated,
   * only a placeholder until it is. */
  plan(chainId: number, draft: Draft, schedule: GasSchedule): Plan;
}

/**
 * A proof a quote can be given: how its call's gas follows from an estimate, when the quote isn't
 * given it, and what its call takes of its transaction's gas. An op paying by its fees isn't
 * quoted: its estimate prices it.
 */
export interface QuotedProof extends DraftedProof {
  /** The transaction whose estimate gives `draft`'s call its gas (callGas). */
  estimating(draft: Draft, schedule: GasSchedule): EncodedCall;
  /** The most gas `draft`'s call can be given in a transaction of `estimate`. */
  callGasOf(estimate: number, draft: Draft, schedule: GasSchedule): number;
  /** The gas its transaction spends for its call to be given `clientGas`, the call's data apart
   * (a quote's breakdown). */
  callPart(clientGas: number): number;
}

/** A proof as the Store keeps it: by its kind, and an op's fee payment, its amounts as strings. */
export type StoredProof =
  | { readonly kind: "runner" }
  | { readonly kind: "transfer" }
  | { readonly kind: "forwarder" }
  | {
    readonly kind: "entryPoint";
    readonly entryPoint: Address;
    readonly feePayment: { gas: string; maxFeePerGas: string; maxPriorityFeePerGas: string };
  };

/** A proof the Store kept (Proof.stored), its entry point among those taken. */
export function parseProof(raw: StoredProof): Proof {
  if (raw.kind === "runner") return new MeasuredByRunner();
  if (raw.kind === "transfer") return new PaidByTransfer();
  if (raw.kind === "forwarder") return new CheckedByForwarder();
  return new ByEntryPoint(entryPointAt(raw.entryPoint), {
    gas: BigInt(raw.feePayment.gas),
    maxFeePerGas: BigInt(raw.feePayment.maxFeePerGas),
    maxPriorityFeePerGas: BigInt(raw.feePayment.maxPriorityFeePerGas),
  });
}

/** Paid as the runner measures it: a runner, deployed through the deployer, makes the
 * submission's deployment and transfer, if any, and its call, and reverts unless the payee gained
 * its amount, so a transaction that doesn't pay doesn't land. */
export class MeasuredByRunner implements QuotedProof {
  paying(amount: bigint, _baseFee: bigint): bigint {
    return amount;
  }

  plan(chainId: number, draft: Draft, schedule: GasSchedule): Plan {
    return runnerPlanned(chainId, draft, schedule, this);
  }

  /** The deployer answers the runner's address, a word; an address without code answers
   * nothing, and ran no runner, so checked no payment: a revert. */
  revertOf(result: unknown): Uint8Array | null {
    if (result === "0x") return new Uint8Array();
    wordBytes(result);
    return null;
  }

  estimating(draft: Draft, schedule: GasSchedule): EncodedCall {
    return { to: DEPLOYER, data: estimatingRunnerOf(draft, schedule).initcode, value: 0n };
  }

  /** The runner's own steps each at their bound, as the runner sent has them, so what they
   * don't use is the call's in both. */
  callGasOf(estimate: number, draft: Draft, schedule: GasSchedule): number {
    const runner = estimatingRunnerOf(draft, schedule);
    const signed = draft.submission.authorizations.length;
    const runnerGas = runnerGasWithin(
      runner.initcode,
      estimate,
      schedule,
      signed,
      draft.accounts.empty,
    );
    return clientGasWithin(runnerGas - runner.overheadGas - (draft.deployGas ?? 0));
  }

  callPart(clientGas: number): number {
    return callNeed(clientGas);
  }

  stored(): StoredProof {
    return { kind: "runner" };
  }
}

/** Paid by the EntryPoint's own rules, for the relay's own handleOps of an op paying by its fees
 * (`feePayment`), the payee its beneficiary, sent as is: a handleOps that lands has paid the
 * beneficiary the op's gas at its price (its call reverting or not), and one whose op fails its
 * validation reverts, so doesn't land. A submission that isn't such a handleOps is a bug of
 * ours: ProofError, never sent. */
export class ByEntryPoint implements DraftedProof {
  constructor(readonly entryPoint: EntryPoint, readonly feePayment: FeePayment) {}

  paying(_amount: bigint, baseFee: bigint): bigint {
    return paidAt(this.feePayment, baseFee);
  }

  plan(chainId: number, draft: Draft, schedule: GasSchedule): Plan {
    const call = draft.submission.call;
    if (call.to !== this.entryPoint.address) {
      throw new ProofError(`handleOps to ${call.to}, not EntryPoint ${this.entryPoint.version}'s`);
    }
    if (handleOpsBeneficiary(call.data) !== draft.submission.payee) {
      throw new ProofError("a handleOps whose beneficiary isn't the payee");
    }
    return entryPointPlan(chainId, draft, schedule, this);
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

/**
 * Paid by its signed transfer, its call and the transfer each sent as it is, by the concurrency
 * account, to targets it may call directly (targets.direct): the call first, then the transfer,
 * so a call may earn what pays for it. Two transactions, so a bundle of their own: neither lands
 * without the other. The call is the transaction, so the EVM gives it its gas, and the relay's
 * overhead is the two transactions' own. The transfer moves exactly the amount to the payee, as
 * its payer signed.
 */
export class PaidByTransfer implements QuotedProof {
  paying(amount: bigint, _baseFee: bigint): bigint {
    return amount;
  }

  plan(chainId: number, draft: Draft, schedule: GasSchedule): Plan {
    return transferPlanned(chainId, draft, schedule, this);
  }

  /** Each is a call to a target that is what it's listed as: whatever it answers, it was
   * made. */
  revertOf(result: unknown): Uint8Array | null {
    dataOf(result);
    return null;
  }

  estimating(draft: Draft, _schedule: GasSchedule): EncodedCall {
    return draft.submission.call;
  }

  /** What's left of `estimate` after the transaction's own gas and its authorizations', each at
   * its bound, as the transaction sent has them. */
  callGasOf(estimate: number, draft: Draft, schedule: GasSchedule): number {
    const signed = draft.submission.authorizations.length;
    const call = draft.submission.call;
    const { standard } = intrinsic(schedule, call.data, { authorizations: signed });
    const authorizations = authorizationsGas(schedule, draft.accounts.empty, signed);
    return Math.max(0, estimate - standard - authorizations);
  }

  callPart(clientGas: number): number {
    return clientGas;
  }

  stored(): StoredProof {
    return { kind: "transfer" };
  }
}

/**
 * A sponsorship's payment is checked by the forwarder, called by the relay's last transaction:
 * it lands only if the payee gained the amount, and its bundle with it. Nothing proofOf chooses:
 * a sponsored account makes its own calls, so there's no call of the relay's to isolate.
 */
export class CheckedByForwarder implements Proof {
  paying(amount: bigint, _baseFee: bigint): bigint {
    return amount;
  }

  /** Each of the relay's transactions did what it's sent for if it didn't fail: the forwarder
   * reverts unless the payee gained the amount. */
  revertOf(result: unknown): Uint8Array | null {
    dataOf(result);
    return null;
  }

  stored(): StoredProof {
    return { kind: "forwarder" };
  }
}

/** A submission its proof can't be of: a bug of ours, that submission's alone. */
export class ProofError extends Error {}

/** What decides how a call is sent, beside its chain and itself (proofOf). */
interface Sending {
  /** Its signed transfer, its token called with its calldata, if it's paid by one. */
  readonly transfer?: EncodedCall | null;
  /** The account it deploys first, if any. */
  readonly deployment?: Deployment | null;
  /** What it pays by its fees, if it's an op paying by them. */
  readonly feePayment?: FeePayment | null;
}

/**
 * How `call` is sent on the chain `chainId`, and its payment known, decided here alone, for a
 * quote and a submission alike, by what it calls and how it pays. An op paying by its fees
 * (`feePayment`) goes to its EntryPoint as it is, which pays or reverts. A call to a target the
 * concurrency account may call directly (targets.direct), paid by a signed transfer (`transfer`,
 * its token called with its calldata) the account may send directly too, is sent as it is with
 * its transfer, if it deploys no account (`deployment`). Any other call needs a runner: for
 * isolation, its target not being listed, or to check what the payee gains.
 */
export function proofOf(
  chainId: number,
  call: EncodedCall,
  sending?: Sending & { readonly feePayment?: null },
): QuotedProof;
export function proofOf(chainId: number, call: EncodedCall, sending: Sending): DraftedProof;
export function proofOf(
  chainId: number,
  call: EncodedCall,
  { transfer = null, deployment = null, feePayment = null }: Sending = {},
): DraftedProof {
  if (feePayment !== null) return new ByEntryPoint(entryPointAt(call.to), feePayment);
  if (
    transfer !== null && deployment === null && direct(chainId, call) && direct(chainId, transfer)
  ) {
    return new PaidByTransfer();
  }
  return new MeasuredByRunner();
}

/**
 * What a plan is made from: its `submission` (a quote's stand-in, paying the most it can, its
 * transfer only as `transfer` here), its `proof`, and what's read of its call's accounts
 * (`accounts`, its `authorities` among them) and its token (`erc20`, null for ETH); its
 * payment's transfer, if it's paid by one, its gas bounded or null (estimated); the account deployed
 * first, if any, its factory call's gas known (`deployGas`, a quote's, estimated apart) or null
 * (estimated with the rest); `prepare` to call a wrapped signature's factory however its payer's
 * code reads (`payerHasCode`).
 */
export interface Draft<P extends DraftedProof = DraftedProof> {
  readonly submission: Submission;
  readonly proof: P;
  readonly authorities: readonly Address[];
  readonly accounts: CallAccounts;
  readonly erc20: Erc20 | null;
  readonly transfer: Transfer | null;
  readonly deployment: Deployment | null;
  readonly deployGas: number | null;
  readonly payerHasCode: boolean;
  readonly prepare: boolean;
}

/** A draft with only what's read of its call: no transfer, deployment or preparing. */
export function draftOf<P extends DraftedProof>(
  submission: Submission,
  proof: P,
  authorities: readonly Address[],
  accounts: CallAccounts,
  erc20: Erc20 | null,
): Draft<P> {
  return {
    submission,
    proof,
    authorities,
    accounts,
    erc20,
    transfer: null,
    deployment: null,
    deployGas: null,
    payerHasCode: false,
    prepare: false,
  };
}

/**
 * What was `taken`, its transactions by its `proof`, and their gas limits under `schedule`,
 * paying in its token as `tokens` reads it, sent by `sender`; a transfer's gas bounded knowing
 * which tokens the payee holds some of (`payeeHolds`), and whether ETH forwarded to it would
 * create its account (`payeeNew`).
 */
export async function makePlan(
  chain: Chain,
  taken: Submission | Sponsorship,
  proof: Proof,
  schedule: GasSchedule,
  tokens: ReadonlyMap<Address, Erc20 | null>,
  sender: Address,
  payeeHolds: ReadonlySet<Address> = new Set(),
  payeeNew = false,
): Promise<Plan> {
  return planOf(
    (await makePlans(chain, [taken], [proof], schedule, tokens, sender, payeeHolds, payeeNew))[0],
  );
}

/** The plan that was made: why not, raised. */
export function planOf(planned: Planned | undefined): Plan {
  if (planned === undefined) throw new ChainError("no result");
  if (planned.status === "rejected") throw planned.reason;
  return planned.value;
}

/**
 * Each one taken, its transactions by its proof (`proofs`, one each), and their gas limits under
 * `schedule`, sent by `sender`, their reads in one batch: a submission's call's accounts and its
 * payer's code, then it drafted (drafted) and planned (planDrafts); a sponsorship's account,
 * then it planned as it is (sandwich). Each its plan, or why not.
 */
export async function makePlans(
  chain: Chain,
  taken: readonly (Submission | Sponsorship)[],
  proofs: readonly Proof[],
  schedule: GasSchedule,
  tokens: ReadonlyMap<Address, Erc20 | null>,
  sender: Address,
  payeeHolds: ReadonlySet<Address> = new Set(),
  payeeNew = false,
): Promise<Planned[]> {
  if (proofs.length !== taken.length) throw new Error("a proof for each one taken");
  const authorities = taken.map((one): Address[] | InvalidSubmissionError => {
    try {
      return authoritiesOf(one.authorizations);
    } catch (e) {
      if (e instanceof InvalidSubmissionError) return e;
      throw e;
    }
  });
  // at the highest block seen, as the estimates after them are
  const at = chain.seenTag;
  const calls: Call[] = [];
  const firsts = taken.map((one, i) => {
    const first = calls.length;
    const of = authorities[i];
    if (!Array.isArray(of)) return first;
    if (isSponsorship(one)) {
      calls.push(...authorityReads(one.account, at));
      return first;
    }
    calls.push(...accountReads(one.call.to, of, at));
    if (one.transfer !== undefined) {
      calls.push({ method: "eth_getCode", params: [one.transfer.payer, at] });
    }
    return first;
  });
  const answers = await chain.batch(calls);
  const planned = new Map<number, Planned>();
  const drafts: PromiseSettledResult<Draft>[] = [];
  const draftedAt: number[] = [];
  taken.forEach((one, index) => {
    try {
      const of = authorities[index];
      const first = firsts[index];
      const proof = proofs[index];
      if (of === undefined || first === undefined) throw new ChainError("no plan read");
      if (proof === undefined) throw new Error("no proof");
      if (!Array.isArray(of)) throw of;
      if (isSponsorship(one)) {
        const value = sandwiched(
          chain.chainId,
          one,
          proof,
          answers.slice(first, first + 3),
          schedule,
          tokenOf(one.token, tokens),
          { payeeHolds: payeeHolds.has(one.token), payeeNew },
        );
        planned.set(index, { status: "fulfilled", value });
        return;
      }
      const reads = answers.slice(first, first + 2 + 3 * of.length);
      const value = draftFrom(one, proof, of, reads, tokens, schedule, payeeHolds.has(one.token));
      drafts.push({ status: "fulfilled", value });
    } catch (reason) {
      if (isSponsorship(one)) {
        planned.set(index, { status: "rejected", reason });
        return;
      }
      drafts.push({ status: "rejected", reason });
    }
    draftedAt.push(index);
  });
  const plans = await planDrafts(chain, drafts, schedule, sender);
  draftedAt.forEach((index, i) => {
    const plan = plans[i];
    if (plan !== undefined) planned.set(index, plan);
  });
  return taken.map((_, index) =>
    planned.get(index) ?? { status: "rejected", reason: new ChainError("no plan") }
  );
}

/** A sponsorship's plan from its account's `reads` (its balance, nonce and code). */
function sandwiched(
  chainId: number,
  sponsorship: Sponsorship,
  proof: Proof,
  reads: readonly Answer[],
  schedule: GasSchedule,
  erc20: Erc20 | null,
  { payeeHolds, payeeNew }: { payeeHolds: boolean; payeeNew: boolean },
): Plan {
  if (!(proof instanceof CheckedByForwarder)) {
    throw new ProofError("a sponsorship's payment is checked by the forwarder");
  }
  return sandwich(chainId, sponsorship, schedule, erc20, {
    accountEmpty: emptyAmong(reads) === 1,
    payeeHolds,
    payeeNew,
  });
}

/**
 * A submission's draft from its `reads` (its call's accounts, then its payer's code, if it's
 * paid by a transfer).
 */
function draftFrom(
  submission: Submission,
  proof: Proof,
  authorities: readonly Address[],
  reads: readonly Answer[],
  tokens: ReadonlyMap<Address, Erc20 | null>,
  schedule: GasSchedule,
  payeeHolds: boolean,
): Draft {
  if (!("plan" in proof)) {
    throw new ProofError("a submission's proof plans it, and this one plans none");
  }
  const accounts = accountsOf(
    submission.call.to,
    authorities,
    reads.slice(0, 1 + 3 * authorities.length),
  );
  let payerHasCode = false;
  if (submission.transfer !== undefined) {
    const payerCode = reads[1 + 3 * authorities.length];
    if (payerCode === undefined) throw new ChainError("no plan read");
    payerHasCode = hasCode(resultOf(payerCode));
  }
  return drafted(
    submission,
    proof as DraftedProof,
    authorities,
    accounts,
    tokenOf(submission.token, tokens),
    schedule,
    { payerHasCode, payeeHolds },
  );
}

/**
 * A signed submission's draft: its transfer, if any, its gas bounded for a key's signature (from
 * its token's measurement, knowing whether the payee holds some of it, `payeeHolds`), estimated
 * for a contract's (a payer with code, or a signature that isn't a key's), whose check is its
 * own; its deployment, if any, estimated. A wrapped signature from a payer with code is drafted
 * without its deployment, to be prepared only if it reverts without it (ERC-6492).
 */
export function drafted(
  submission: Submission,
  proof: DraftedProof,
  authorities: readonly Address[],
  accounts: CallAccounts,
  erc20: Erc20 | null,
  schedule: GasSchedule,
  { payerHasCode, payeeHolds }: { payerHasCode: boolean; payeeHolds: boolean },
): Draft {
  let deployment = toDeploy(submission);
  let transfer: Transfer | null = null;
  const signed = submission.transfer;
  if (signed !== undefined) {
    if (erc20 === null || erc20.transferGas === undefined) {
      throw new InvalidSubmissionError(
        "payment",
        refused("no-eip3009", { token: submission.token }),
      );
    }
    const wrapperDeployment = transferDeployment(signed);
    const contractPayer = !transferEcdsa(signed) || payerHasCode || wrapperDeployment !== null;
    const gas = contractPayer ? null : transferGas(erc20.transferGas, schedule, payeeHolds);
    transfer = { calldata: transferCalldata(signed), gas };
    if (wrapperDeployment !== null && payerHasCode) deployment = submission.deployment ?? null;
  }
  return {
    ...draftOf(submission, proof, authorities, accounts, erc20),
    transfer,
    deployment,
    payerHasCode,
  };
}

/**
 * Each draft planned by its proof, under `schedule`, sent by `sender`, a draft that's an error
 * left as it is: each its plan, or why not. A plan whose gas a step leaves to an estimate (code
 * the client chooses) has it from one eth_estimateGas of its transaction as it will be sent, the
 * estimates in one batch: an estimate that reverts is the submission's simulation failing.
 * Quotes, the bundler's estimates and submissions are all planned here.
 */
export async function planDrafts(
  chain: Chain,
  drafts: readonly PromiseSettledResult<Draft>[],
  schedule: GasSchedule,
  sender: Address,
): Promise<Planned[]> {
  const estimating: { index: number; draft: Draft; plan: Plan }[] = [];
  const plans = drafts.map((settled, index): Planned => {
    if (settled.status === "rejected") return settled;
    const draft = settled.value;
    try {
      const plan = draft.proof.plan(chain.chainId, draft, schedule);
      if (planEstimated(plan)) estimating.push({ index, draft, plan });
      return { status: "fulfilled", value: plan };
    } catch (reason) {
      return { status: "rejected", reason };
    }
  });
  await estimate(chain, estimating, plans, schedule, sender);
  return plans;
}

/** A payment's `token` as `tokens` measures it (null for ETH); refused if it's suspended. */
function tokenOf(token: Address, tokens: ReadonlyMap<Address, Erc20 | null>): Erc20 | null {
  if (!tokens.has(token)) {
    throw new InvalidSubmissionError("payment", refused("token-suspended", { token }));
  }
  return lookup(tokens, token, "a token's measurement");
}

/**
 * The most gas the forwarder's call takes, the transaction's own aside. For ETH, the forwarder's
 * own, to a payee whose code uses none (`payeeNew`: the ETH creates its account). For a token,
 * the forwarder's own and what it leaves the token: three balanceOf reads and its transfer,
 * measured at zero value and the balances' writes added (`payeeHolds`: the payee's is changed,
 * not set), with a 63rd more, since each is a call below the transaction, given all but a 64th
 * of what's left (EIP-150); a 63rd of them all leaves each call its own, whichever comes last.
 */
export function checkGas(
  schedule: GasSchedule,
  erc20: Erc20 | null,
  { payeeHolds, payeeNew }: { payeeHolds: boolean; payeeNew: boolean },
): number {
  if (erc20 === null) return ethGas(schedule, payeeNew);
  if (erc20.forwardGas === undefined) {
    throw new InvalidSubmissionError("payment", refused("not-forwarded", { token: erc20.address }));
  }
  const calls = 3 * erc20.balanceGas + transferGas(erc20.forwardGas, schedule, payeeHolds);
  return tokenGas(schedule) + callNeed(calls);
}

/** An account as read: whether it's empty (no balance, no nonce, no code), and its nonce. */
export interface AccountState {
  readonly empty: boolean;
  readonly nonce: number;
}

/** `account`'s state, read in one batch at the highest block seen. */
export async function accountState(chain: Chain, account: Address): Promise<AccountState> {
  const reads = await chain.batch(authorityReads(account, chain.seenTag));
  const nonce = reads[1];
  if (nonce === undefined) throw new ChainError("no account read");
  return { empty: emptyAmong(reads) === 1, nonce: Number(quantity(resultOf(nonce))) };
}

/** Calldata of `data`'s length at its dearest: no byte zero. */
function dearest(data: Uint8Array): Uint8Array {
  return new Uint8Array(data.length).fill(0xff);
}

/**
 * What a sponsorship's first transaction takes beside the gas its call is given: its intrinsic
 * gas, carrying `data` and `authorizations`, `sending` ETH or not, and what delegating the
 * account costs, or creating it by the ETH sent, if it's empty. The data is priced as if it had
 * no zero byte: an account's call may hold what it pays, so what it's charged mustn't change
 * with that.
 */
export function fundingOverhead(
  schedule: GasSchedule,
  data: Uint8Array,
  authorizations: number,
  sending: boolean,
  accountEmpty: boolean,
): number {
  const { standard } = intrinsic(schedule, dearest(data), { value: sending, authorizations });
  if (authorizations > 0) {
    return standard + authorizationsGas(schedule, accountEmpty ? 1 : 0, authorizations);
  }
  return standard + (sending && accountEmpty ? schedule.txNewAccount : 0);
}

/**
 * A sponsorship's transactions. The relay's first, to the account: the ETH its transactions need
 * (funding), its authorizations, and its call, given the sponsorship's gas with the
 * transaction's own, and what creating or delegating the account costs (`accountEmpty`: it has
 * no balance, nonce or code yet). The account's own, as signed. The relay's last: the
 * forwarder's call for the amount (checkGas), its calldata priced as if the amount's word had no
 * zero byte, so what a sponsorship is charged doesn't change with what it pays. With no
 * transaction of the account's the first sends no ETH: it's only the call.
 */
export function sandwich(
  chainId: number,
  sponsorship: Sponsorship,
  schedule: GasSchedule,
  erc20: Erc20 | null,
  { accountEmpty, payeeHolds, payeeNew }: {
    accountEmpty: boolean;
    payeeHolds: boolean;
    payeeNew: boolean;
  },
): Plan {
  const proof = new CheckedByForwarder();
  const authorizations = sponsorship.authorizations.length;
  const data = sponsorship.call === null ? new Uint8Array(0) : sponsorship.call.data;
  const value = funding(sponsorship);
  const funded: EncodedCall = { to: sponsorship.account, data, value };
  const own = fundingOverhead(schedule, data, authorizations, value > 0n, accountEmpty);
  const fundingGas = intrinsic(schedule, dearest(data), { value: value > 0n, authorizations });
  const first: Step = {
    transaction: funded,
    proof,
    gasLimit: Math.max(own + sponsorship.gas, fundingGas.floor),
    size: unsignedSize(chainId, funded.to, data, value, authorizations),
    authorizations: sponsorship.authorizations,
    estimated: false,
  };
  const check = forwarding(sponsorship.payee, sponsorship.token, sponsorship.amount);
  const widest = forwarding(sponsorship.payee, sponsorship.token, WIDEST_AMOUNT);
  const checking = intrinsic(schedule, widest.data);
  const executing = checkGas(schedule, erc20, { payeeHolds, payeeNew });
  const last: Step = {
    transaction: check,
    proof,
    gasLimit: Math.max(checking.standard + executing, checking.floor),
    size: unsignedSize(chainId, check.to, check.data, 0n, 0),
    authorizations: [],
    estimated: false,
  };
  return {
    steps: [first, ...sponsorship.transactions.map((transaction) => ({ transaction })), last],
  };
}

/** The draft's runner's transaction, and its gas limit from the gas schedule, its call given the
 * submission's gas: for one with a step whose gas isn't known (code the client chooses), only a
 * placeholder until it's estimated. */
function runnerPlanned(chainId: number, draft: Draft, schedule: GasSchedule, proof: Proof): Plan {
  const { submission } = draft;
  const payment: Payment = {
    token: draft.erc20,
    payee: submission.payee,
    amount: submission.amount,
    ...(draft.transfer === null ? {} : { transfer: draft.transfer }),
  };
  let runner: Runner;
  try {
    runner = inCallRunner(submission.call, payment, schedule, submission.gas, {
      delegated: draft.accounts.delegated,
      deployment: draft.deployment,
      prepare: draft.prepare,
    });
  } catch (e) {
    if (e instanceof RunnerError) {
      throw new InvalidSubmissionError("call", refused("runner-unbuilt", { why: e.message }));
    }
    throw e;
  }
  const authorizations = draft.authorities.length;
  const gasLimit = transactionGas(
    runner.initcode,
    frameGas(runner, submission.gas) + (draft.deployGas ?? 0),
    schedule,
    authorizations,
    draft.accounts.empty,
  );
  const size = unsignedSize(chainId, DEPLOYER, runner.initcode, 0n, authorizations);
  const transaction = { to: DEPLOYER, data: runner.initcode, value: 0n };
  const estimated = (draft.transfer !== null && draft.transfer.gas === null) ||
    (draft.deployment !== null && draft.deployGas === null);
  const step = {
    transaction,
    proof,
    gasLimit,
    size,
    authorizations: submission.authorizations,
    estimated,
  };
  return { steps: [step] };
}

/** The runner whose estimate gives the draft's call its gas: its deployment, if any, then the
 * call, without its payment's transfer and check (estimatingRunner). */
function estimatingRunnerOf(draft: Draft, schedule: GasSchedule): Runner {
  const { submission } = draft;
  const payment: Payment = {
    token: draft.erc20,
    payee: submission.payee,
    amount: submission.amount,
  };
  try {
    return estimatingRunner(submission.call, payment, schedule, {
      delegated: draft.accounts.delegated,
      deployment: draft.deployment,
    });
  } catch (e) {
    if (e instanceof RunnerError) {
      throw new InvalidSubmissionError("call", refused("runner-unbuilt", { why: e.message }));
    }
    throw e;
  }
}

/**
 * The draft's call, a handleOps, sent as is: its transaction's gas limit from the gas schedule,
 * the call given the submission's gas (handleOps' own gas, a node's estimate from its
 * beneficiary, which the EntryPoint pays last, less its intrinsic gas), with its authorizations,
 * those of empty accounts among them; and its most bytes unsigned. Sent from a concurrency
 * account, the beneficiary is cold when it's paid: its access as estimated was warm.
 */
function entryPointPlan(chainId: number, draft: Draft, schedule: GasSchedule, proof: Proof): Plan {
  const call = draft.submission.call;
  const authorizations = draft.authorities.length;
  const { standard, floor } = intrinsic(schedule, call.data, { authorizations });
  const beneficiary = schedule.coldAccountAccess - WARM_ACCOUNT_ACCESS;
  const execution = draft.submission.gas + beneficiary +
    authorizationsGas(schedule, draft.accounts.empty, authorizations);
  const gasLimit = Math.max(standard + execution, floor);
  const size = unsignedSize(chainId, call.to, call.data, 0n, authorizations);
  const step = {
    transaction: call,
    proof,
    gasLimit,
    size,
    authorizations: draft.submission.authorizations,
    estimated: false,
  };
  return { steps: [step] };
}

/**
 * The draft's call, then its transfer, each a transaction as it is. The call's gas limit is the
 * submission's gas with the transaction's own, and its authorizations', which it carries. The
 * transfer's is its bound with the transaction's own, and a 63rd of the bound more: the bound is
 * a zero-value transfer's gas, measured, with the balances' writes added, and what a token spends
 * it spends below its own frame (a proxy's implementation), given all but a 64th of what's left
 * (EIP-150). A runner's transfer needs none: the runner reads the payee's balance first, which
 * leaves the transfer that much warm. One whose gas isn't known (a contract payer's) is
 * estimated, alone, as the chain stands before the call.
 */
function transferPlanned(chainId: number, draft: Draft, schedule: GasSchedule, proof: Proof): Plan {
  const { submission } = draft;
  if (draft.transfer === null) throw new ProofError("paid by a transfer, and drafted without one");
  const call = submission.call;
  const authorizations = draft.authorities.length;
  const calling = intrinsic(schedule, call.data, { authorizations });
  const execution = submission.gas +
    authorizationsGas(schedule, draft.accounts.empty, authorizations);
  const made: Step = {
    transaction: call,
    proof,
    gasLimit: Math.max(calling.standard + execution, calling.floor),
    size: unsignedSize(chainId, call.to, call.data, 0n, authorizations),
    authorizations: submission.authorizations,
    estimated: false,
  };
  const transfer = { to: submission.token, data: draft.transfer.calldata, value: 0n };
  const transferring = intrinsic(schedule, transfer.data, {});
  const paid: Step = {
    transaction: transfer,
    proof,
    gasLimit: Math.max(
      transferring.standard + callNeed(draft.transfer.gas ?? 0),
      transferring.floor,
    ),
    size: unsignedSize(chainId, transfer.to, transfer.data, 0n, 0),
    authorizations: [],
    estimated: draft.transfer.gas === null,
  };
  return { steps: [made, paid] };
}

/**
 * Each draft's plan, put in `plans` at its index, each step to be estimated given its gas limit
 * by one eth_estimateGas of its transaction, in one batch: one that reverts is the submission's
 * simulation failing, unless a wrapped signature's payer may need preparing, which is then
 * estimated again, prepared.
 */
async function estimate(
  chain: Chain,
  drafts: readonly { index: number; draft: Draft; plan: Plan }[],
  plans: Planned[],
  schedule: GasSchedule,
  sender: Address,
): Promise<void> {
  if (drafts.length === 0) return;
  const at = chain.seenTag;
  const answers = await chain.batch(
    drafts.flatMap(({ plan }) =>
      planOwn(plan).filter((step) => step.estimated).map((step) => ({
        method: "eth_estimateGas",
        params: [transactionJson(step, sender), at],
      }))
    ),
  );
  const again: { index: number; draft: Draft; plan: Plan }[] = [];
  let first = 0;
  for (const { index, draft, plan } of drafts) {
    const count = planOwn(plan).filter((step) => step.estimated).length;
    const its = answers.slice(first, first + count);
    first += count;
    try {
      if (its.length !== count) throw new ChainError("no estimate");
      again.push(...estimated(chain.chainId, index, draft, plan, its, plans, schedule));
    } catch (reason) {
      // a node's malformed answer, or a bug of ours: this submission's alone
      plans[index] = { status: "rejected", reason };
    }
  }
  await estimate(chain, again, plans, schedule, sender);
}

/** `draft`'s estimates `answers`, one for each of its plan's steps to be estimated, read into its
 * plan, at `index` of `plans`: their gas limits, or why it has none; or, its payer's wrapped
 * signature to be prepared first, it drafted again (returned) (bruno/plan.py's _estimated). */
function estimated(
  chainId: number,
  index: number,
  draft: Draft,
  plan: Plan,
  answers: readonly Answer[],
  plans: Planned[],
  schedule: GasSchedule,
): { index: number; draft: Draft; plan: Plan }[] {
  const failed = answers.find((answer) => "error" in answer);
  if (failed === undefined || !("error" in failed)) {
    const estimates = answers.map((answer) =>
      "error" in answer ? 0 : Number(quantity(answer.result))
    );
    let next = 0;
    const steps = plan.steps.map((step) =>
      isOwn(step) && step.estimated
        ? { ...step, gasLimit: estimates[next++] ?? step.gasLimit }
        : step
    );
    plans[index] = { status: "fulfilled", value: { ...plan, steps } };
    return [];
  }
  if (failed.error.code !== REVERTED) {
    plans[index] = { status: "rejected", reason: failed.error };
    return [];
  }
  const signed = draft.submission.transfer;
  if (
    !draft.prepare && signed !== undefined && transferDeployment(signed) !== null &&
    draft.payerHasCode
  ) {
    const prepared = { ...draft, prepare: true, deployment: toDeploy(draft.submission) };
    return [{ index, draft: prepared, plan: prepared.proof.plan(chainId, prepared, schedule) }];
  }
  plans[index] = {
    status: "rejected",
    reason: new SimulationFailedError(revertData(failed.error)),
  };
  return [];
}

/** `step`'s transaction, from `sender`, with the authorizations it carries, as eth_call and
 * eth_estimateGas take it: no fees, so no balance is needed; at `gasLimit`, unless it's being
 * estimated (a limit would cap the estimate's search). */
export function transactionJson(
  step: Step,
  sender: Address,
  gasLimit?: number,
): Record<string, unknown> {
  return callJson(step.transaction, sender, step.authorizations, gasLimit);
}

/**
 * A transaction another account signed, as eth_simulateV1 takes a call: from its sender, at its
 * own nonce, gas and fees, with its access list and authorizations.
 */
export function signedJson(transaction: Decoded): Record<string, unknown> {
  const { signed } = transaction;
  const tx: Record<string, unknown> = {
    from: signed.sender,
    data: hex(transaction.data),
    value: hexQuantity(signed.value),
    gas: hexQuantity(signed.gas),
    nonce: hexQuantity(signed.nonce),
    maxFeePerGas: hexQuantity(signed.maxFeePerGas),
    maxPriorityFeePerGas: hexQuantity(transaction.maxPriorityFeePerGas),
  };
  if (transaction.to !== null) tx.to = transaction.to;
  if (transaction.accessList.length > 0) {
    tx.accessList = transaction.accessList.map((a) => ({
      address: a.address,
      storageKeys: a.storageKeys.map(hex),
    }));
  }
  if (transaction.authorizations.length > 0) {
    tx.authorizationList = transaction.authorizations.map(authorizationJson);
  }
  return tx;
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

/** How many of `authorities` are empty (no balance, no nonce, no code), read in one batch at the
 * highest block seen: nothing read if there are none. */
export async function countEmpty(
  chain: Chain,
  authorities: readonly Address[],
): Promise<number> {
  const at = chain.seenTag;
  const reads = authorities.flatMap((authority) => authorityReads(authority, at));
  return emptyAmong(await chain.batch(reads));
}

/** What's read of a call's accounts: those EIP-7702 delegated in its transaction (its
 * authorities, and its target if delegated already), how many of its authorities are empty, and
 * which of them are delegated already (`delegating`). */
export interface CallAccounts {
  readonly delegated: ReadonlySet<Address>;
  readonly empty: number;
  readonly delegating: ReadonlySet<Address>;
}

/** `target`'s code, and each authority's balance, nonce and code, read in one batch at the
 * highest block seen. */
export async function callAccounts(
  chain: Chain,
  target: Address,
  authorities: readonly Address[],
): Promise<CallAccounts> {
  const reads = accountReads(target, authorities, chain.seenTag);
  return accountsOf(target, authorities, await chain.batch(reads));
}

/** What's read of a call's accounts at `block` (CallAccounts): its target's code, then each
 * authority's balance, nonce and code. */
function accountReads(target: Address, authorities: readonly Address[], block: string): Call[] {
  return [
    { method: "eth_getCode", params: [target, block] },
    ...authorities.flatMap((authority) => authorityReads(authority, block)),
  ];
}

/** A call's accounts from their reads' `answers` (accountReads). */
function accountsOf(
  target: Address,
  authorities: readonly Address[],
  answers: readonly Answer[],
): CallAccounts {
  const delegatedCode = (i: number): boolean => {
    const answer = answers[i];
    if (answer === undefined) throw new ChainError("no code read");
    return hex(codeOf(resultOf(answer))).startsWith(DELEGATION_PREFIX);
  };
  const delegated = new Set(authorities);
  if (delegatedCode(0)) delegated.add(target);
  const delegating = new Set(authorities.filter((_, i) => delegatedCode(3 + 3 * i)));
  return { delegated, empty: emptyAmong(answers.slice(1)), delegating };
}

/**
 * The gas `draft`'s call needs as it will run, by its proof: one eth_estimateGas of the
 * transaction its proof estimates it by (a runner's, without its payment's transfer and check,
 * after its deployment), in `sender`'s transaction with the submission's
 * authorizations, the most gas its call can be given in a transaction of that estimate.
 * SimulationFailedError if the call reverts.
 */
export async function callGas(
  chain: Chain,
  draft: Draft<QuotedProof>,
  schedule: GasSchedule,
  sender: Address,
): Promise<number> {
  const transaction = draft.proof.estimating(draft, schedule);
  const tx = callJson(transaction, sender, draft.submission.authorizations);
  const [answer] = await chain.batch([{ method: "eth_estimateGas", params: [tx, "latest"] }]);
  if (answer === undefined) throw new ChainError("no estimate");
  if ("error" in answer) {
    if (answer.error.code === REVERTED) {
      throw new SimulationFailedError(revertData(answer.error));
    }
    throw answer.error;
  }
  return draft.proof.callGasOf(Number(quantity(answer.result)), draft, schedule);
}

/** An authority's reads at `block`, to tell whether it's empty: its balance, nonce and code. */
function authorityReads(account: Address, block: string): Call[] {
  return [
    { method: "eth_getBalance", params: [account, block] },
    { method: "eth_getTransactionCount", params: [account, block] },
    { method: "eth_getCode", params: [account, block] },
  ];
}

/** How many of the authorities whose reads `answers` are, three each, are empty. */
function emptyAmong(answers: readonly Answer[]): number {
  if (answers.length % 3 !== 0) throw new ChainError("no authority read");
  let empty = 0;
  for (let i = 0; i < answers.length; i += 3) {
    const [balance, nonce, code] = answers.slice(i, i + 3).map(resultOf);
    if (quantity(balance) === 0n && quantity(nonce) === 0n && !hasCode(code)) {
      empty++;
    }
  }
  return empty;
}
