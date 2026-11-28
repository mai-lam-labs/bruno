/**
 * A submission's transactions, by its proof of payment (the runner measuring it, the EntryPoint's
 * own rules, or its transfer sent with its calls), and their gas limits under a block's schedule,
 * from what the chain says of its calls' targets and its authorities. The one path: a quote, the bundler's estimates
 * and a submission are each drafted (Draft), their proof chosen in one place (proofOf), and
 * planned alike (planDrafts). Many submissions' reads are one batch; their simulation is
 * simulation.ts's.
 */

import {
  accountReads,
  accountsOf,
  authorityReads,
  type CallAccounts,
  DELEGATION_PREFIX,
  emptyAmong,
} from "./accountreads.ts";
import { type Chain, ChainError, dataOf, hasCode, quantity, revertData, wordOf } from "./chain.ts";
import { type Answer, type Call, resultOf, REVERTED } from "./jsonrpc.ts";
import { lookup } from "./measurements.ts";
import { aggregate3, GroupError, ownGas } from "./multicall.ts";
import {
  ANSWER_BYTES,
  runnerCall,
  runnerGasWithin,
  STAND_IN_SALT,
  transactionGas,
} from "./deployer.ts";
import type { Deployment } from "./erc6492.ts";
import {
  type Address,
  type EncodedCall,
  type Hex,
  hex,
  hexQuantity,
  WARM_ACCOUNT_ACCESS,
} from "./evm.ts";
import { authorizationsGas, type GasSchedule, intrinsic, transfer as transferOf } from "./forks.ts";
import { ethGas, forwarding, tokenGas } from "./forwarder.ts";
import { funding, isSponsorship, type Sponsorship } from "./sponsorship.ts";
import { boundable, direct } from "./targets.ts";
import { balanceOfCall } from "./tokens.ts";
import {
  transferCalldata,
  transferDeployment,
  transferEcdsa,
  transferGas,
  transferParties,
} from "./eip3009.ts";
import {
  callNeed,
  callsNeed,
  clientGasWithin,
  type Erc20,
  estimatingRunner,
  frameGas,
  inCallRunner,
  type Payment,
  type PlannedCall,
  type Runner,
  RunnerError,
  type Transfer,
} from "./runner.ts";
import {
  authorities as authoritiesOf,
  InvalidSubmissionError,
  type Submission,
  type SubmittedCall,
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
  type FeePayment,
  handleOpsBeneficiary,
  paidAt,
} from "./userops.ts";
import { refused } from "./refusals.ts";
import { SimulationFailedError } from "./reverts.ts";

/** An amount whose word has no zero byte: the dearest calldata an amount can be. */
const WIDEST_AMOUNT = (1n << 256n) - 1n;
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
  /** The transaction whose estimate gives `draft`'s `index`th call its gas (callGas). */
  estimating(draft: Draft, index: number, schedule: GasSchedule): EncodedCall;
  /** The most gas `draft`'s `index`th call can be given in a transaction of `estimate`. */
  callGasOf(estimate: number, draft: Draft, index: number, schedule: GasSchedule): number;
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

  /** The deployer answers the runner's address, 20 bytes; an address without code answers
   * nothing, and ran no runner, so checked no payment: a revert. */
  revertOf(result: unknown): Uint8Array | null {
    if (result === "0x") return new Uint8Array();
    if (dataOf(result).length !== ANSWER_BYTES) {
      throw new ChainError(`the deployer answered ${String(result)}`);
    }
    return null;
  }

  /** The draft's runner, every call in it: the one estimated states no gas, so the runner gives
   * it what's left. */
  estimating(draft: Draft, _index: number, schedule: GasSchedule): EncodedCall {
    const initcode = estimatingRunnerOf(draft, schedule).initcode;
    return runnerCall(STAND_IN_SALT, initcode, sent(draft));
  }

  /** The runner's own steps each at their bound and its other calls each at their gas, as the
   * runner sent has them, so what they don't use is the call's in both. */
  callGasOf(estimate: number, draft: Draft, _index: number, schedule: GasSchedule): number {
    const runner = estimatingRunnerOf(draft, schedule);
    const signed = draft.submission.authorizations.length;
    const runnerGas = runnerGasWithin(
      runner.initcode,
      estimate,
      schedule,
      signed,
      draft.accounts.empty,
      sent(draft) > 0n,
    );
    const others = callsNeed(draft.calls);
    return clientGasWithin(runnerGas - runner.overheadGas - (draft.deployGas ?? 0) - others);
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
    const call = handleOpsOf(draft).call;
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
 * Paid by its signed transfer, its calls and the transfer each sent as it is, by the concurrency
 * account, to targets it may call directly (targets.direct): the calls first, in order, then the
 * transfer, so a call may earn what pays for them. Consecutive ones the relay bounds are one
 * transaction, through Multicall3, and any other a transaction of its own; more than one
 * transaction is a bundle of their own: none lands without the others. The relay's overhead is
 * the transactions' own, and Multicall3's. The transfer moves exactly the amount to the payee,
 * as its payer signed.
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

  /** The call alone, as the chain stands: the calls before it aren't made first. */
  estimating(draft: Draft, index: number, _schedule: GasSchedule): EncodedCall {
    return plannedAt(draft, index).call;
  }

  /** What's left of `estimate` after the transaction's own gas and its authorizations', each at
   * its bound, as the transaction sent has them. */
  callGasOf(estimate: number, draft: Draft, index: number, schedule: GasSchedule): number {
    const signed = draft.submission.authorizations.length;
    const call = plannedAt(draft, index).call;
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
 * it lands only if the payee gained the amount, and its bundle with it. Its calls are planned as
 * a submission's are, a runner of them checking nothing itself.
 */
export class CheckedByForwarder implements DraftedProof {
  paying(amount: bigint, _baseFee: bigint): bigint {
    return amount;
  }

  plan(chainId: number, draft: Draft, schedule: GasSchedule): Plan {
    return forwarderChecked(chainId, draft, schedule, this);
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
 * How `calls` are sent on the chain `chainId`, and their payment known, decided here alone, for
 * a quote and a submission alike, by what they call and how they pay. An op paying by its fees
 * (`feePayment`) goes to its EntryPoint as it is, its handleOps the one call, which pays or
 * reverts. Calls each to a target the concurrency account may call directly (targets.direct),
 * paid by a signed transfer (`transfer`, its token called with its calldata) the account may
 * send directly too, are sent as they are with their transfer, if they deploy no account
 * (`deployment`). Anything else needs a runner: for isolation, a target not being listed, or to
 * check what the payee gains.
 */
export function proofOf(
  chainId: number,
  calls: readonly EncodedCall[],
  sending?: Sending & { readonly feePayment?: null },
): QuotedProof;
export function proofOf(
  chainId: number,
  calls: readonly EncodedCall[],
  sending: Sending,
): DraftedProof;
export function proofOf(
  chainId: number,
  calls: readonly EncodedCall[],
  { transfer = null, deployment = null, feePayment = null }: Sending = {},
): DraftedProof {
  if (feePayment !== null) {
    const [handleOps, ...others] = calls;
    if (handleOps === undefined || others.length > 0) {
      throw new ProofError(`an op's handleOps is one call, and there are ${calls.length}`);
    }
    return new ByEntryPoint(entryPointAt(handleOps.to), feePayment);
  }
  if (
    transfer !== null && deployment === null && calls.every((call) => direct(chainId, call)) &&
    direct(chainId, transfer)
  ) {
    return new PaidByTransfer();
  }
  return new MeasuredByRunner();
}

/** What's read at `block` to bound `call`'s gas, if it's to a function the relay can bound
 * (targets.ts's boundable, a token's transfer a key signs): its signer's code, and what its
 * recipient holds of the token. Nothing for any other call. */
export function boundReads(chainId: number, call: EncodedCall, block: string): Call[] {
  const parties = boundable(chainId, call) ? transferParties(call.data) : null;
  if (parties === null) return [];
  const [signer, recipient] = parties;
  return [
    { method: "eth_getCode", params: [signer, block] },
    balanceOfCall(call.to, recipient, block),
  ];
}

/**
 * The most gas `call` can take, from its `reads` (boundReads), if the relay bounds it: null
 * otherwise. Bounded is a fact of this call, not of its function: a transfer the token measured,
 * signed by a key, so from an account with no code that none of the submission's `authorities`
 * delegates (the token checks any other signature by the account's own code). Its bound is the
 * measured transfer's with its balances' writes, its recipient's set or only changed by what it
 * holds; one whose holding wasn't read is taken to hold nothing, the dearer.
 */
export function callBound(
  chainId: number,
  call: EncodedCall,
  reads: readonly Answer[],
  tokens: ReadonlyMap<Address, Erc20 | null>,
  schedule: GasSchedule,
  authorities: readonly Address[],
): number | null {
  const [code, held] = reads;
  if (code === undefined || held === undefined) return null;
  const parties = boundable(chainId, call) ? transferParties(call.data) : null;
  const erc20 = tokens.get(call.to);
  if (parties === null || erc20 === undefined || erc20 === null) return null;
  if (erc20.transferGas === undefined) return null;
  const [signer] = parties;
  if (authorities.includes(signer) || hasCode(resultOf(code))) return null;
  const holds = !("error" in held) && wordOf(held.result) > 0n;
  return transferGas(erc20.transferGas, schedule, holds);
}

/** Each of `calls`' bound (callBound), their reads made here in one batch, at the highest block
 * seen: for a quote, which has no batch of its own to put them in. Nothing is read where none
 * may be bounded. */
export async function boundsOf(
  chain: Chain,
  calls: readonly EncodedCall[],
  tokens: ReadonlyMap<Address, Erc20 | null>,
  schedule: GasSchedule,
  authorities: readonly Address[],
): Promise<(number | null)[]> {
  const reads = calls.map((call) => boundReads(chain.chainId, call, chain.seenTag));
  const asked = reads.flat();
  const answers = asked.length > 0 ? await chain.batch(asked) : [];
  let at = 0;
  return calls.map((call, index) => {
    const its = answers.slice(at, at + reads[index]!.length);
    at += its.length;
    return callBound(chain.chainId, call, its, tokens, schedule, authorities);
  });
}

/** A submission's call, the `index`th, as it's planned: given its `bound`, if the relay bounds
 * it (whatever gas it states), else the gas it states. InvalidSubmissionError for one the relay
 * doesn't bound that states none. */
export function plannedCall(
  index: number,
  given: SubmittedCall,
  bound: number | null,
): PlannedCall {
  if (bound !== null) return { call: given.call, gas: callNeed(bound), bounded: true };
  if (given.gas === null) {
    throw new InvalidSubmissionError(`calls[${index}].gas`, refused("call-no-gas"));
  }
  return { call: given.call, gas: given.gas, bounded: false };
}

/**
 * The ETH a sponsored `account` is sent before its own transactions, `amount` of it. A plain send
 * where the account has no code and nothing in the request delegates it (`plain`), creating it
 * if it's `new` (no balance, nonce or code yet). Otherwise a call a runner makes, sending it, so
 * the account's code runs in isolation: its gas isn't known, so its transaction is estimated.
 */
export interface Funding {
  readonly account: Address;
  readonly amount: bigint;
  readonly new: boolean;
  readonly plain: boolean;
}

/** Whether `funding` is a runner's call: there's ETH to send, and code it would run. */
export function throughRunner(funding: Funding): boolean {
  return funding.amount > 0n && !funding.plain;
}

/**
 * What a plan is made from: its `submission` (a quote's stand-in, paying the most it can, its
 * transfer only as `transfer` here; or a sponsorship), its `proof`, and what's read of its
 * call's accounts (`accounts`, its `authorities` among them) and its token (`erc20`, null for
 * ETH); its `calls` as planned, each with the gas it's given; its payment's transfer, if it's
 * paid by one, its gas bounded or null (estimated); the account deployed first, if any, its
 * factory call's gas known (`deployGas`, a quote's, estimated apart) or null (estimated with the
 * rest); `prepare` to call a wrapped signature's factory however its payer's code reads
 * (`payerHasCode`). A sponsorship's also has the ETH its account is sent (`funding`), the
 * account's own `transactions`, sent after the relay's calls, and the gas the forwarder's check
 * takes (`checkingGas`).
 */
export interface Draft<P extends DraftedProof = DraftedProof> {
  readonly submission: Submission | Sponsorship;
  readonly proof: P;
  readonly authorities: readonly Address[];
  readonly accounts: CallAccounts;
  readonly erc20: Erc20 | null;
  readonly calls: readonly PlannedCall[];
  readonly transfer: Transfer | null;
  readonly deployment: Deployment | null;
  readonly deployGas: number | null;
  readonly payerHasCode: boolean;
  readonly prepare: boolean;
  /** Its runner's salt, if it's sent through one: a stand-in's where it's only priced. */
  readonly salt: Uint8Array;
  readonly funding: Funding | null;
  readonly transactions: readonly Decoded[];
  readonly checkingGas: number;
}

/** An op's draft's one call, its handleOps: ProofError if it's drafted with any other number. */
function handleOpsOf(draft: Draft): PlannedCall {
  const [handleOps, ...others] = draft.calls;
  if (handleOps === undefined || others.length > 0) {
    throw new ProofError(`an op's handleOps is one call, and there are ${draft.calls.length}`);
  }
  return handleOps;
}

/** A draft's `index`th call: a bug of ours if it has none there. */
function plannedAt(draft: Draft, index: number): PlannedCall {
  const planned = draft.calls[index];
  if (planned === undefined) throw new ProofError(`a draft with no call at ${index}`);
  return planned;
}

/** A draft with only what's read of its calls: no transfer, deployment or preparing. */
export function draftOf<P extends DraftedProof>(
  submission: Submission | Sponsorship,
  proof: P,
  authorities: readonly Address[],
  accounts: CallAccounts,
  erc20: Erc20 | null,
  calls: readonly PlannedCall[],
): Draft<P> {
  return {
    submission,
    proof,
    authorities,
    accounts,
    erc20,
    calls,
    transfer: null,
    deployment: null,
    deployGas: null,
    payerHasCode: false,
    prepare: false,
    salt: STAND_IN_SALT,
    funding: null,
    transactions: [],
    checkingGas: 0,
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
  salt: Uint8Array = STAND_IN_SALT,
): Promise<Plan> {
  const plans = await makePlans(
    chain,
    [taken],
    [proof],
    schedule,
    tokens,
    sender,
    payeeHolds,
    payeeNew,
    [salt],
  );
  return planOf(plans[0]);
}

/** The plan that was made: why not, raised. */
export function planOf(planned: Planned | undefined): Plan {
  if (planned === undefined) throw new ChainError("no result");
  if (planned.status === "rejected") throw planned.reason;
  return planned.value;
}

/**
 * Each one taken, its transactions by its proof (`proofs`, one each), and their gas limits under
 * `schedule`, sent by `sender`, their reads in one batch: a submission's calls' accounts and its
 * payer's code, a sponsorship's account and its calls' accounts, then each drafted (drafted,
 * sponsored) and planned (planDrafts). Each its plan, or why not.
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
  salts: readonly Uint8Array[] | null = null,
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
    const targets = one.calls.map((each) => each.call.to);
    if (isSponsorship(one)) {
      // its account's own state, then its calls' accounts, the account among them
      calls.push(...authorityReads(one.account, at));
      targets.push(one.account);
    }
    calls.push(...accountReads(targets, of, at));
    if (one.transfer !== undefined) {
      calls.push({ method: "eth_getCode", params: [one.transfer.payer, at] });
    }
    for (const each of one.calls) calls.push(...boundReads(chain.chainId, each.call, at));
    return first;
  });
  const answers = await chain.batch(calls);
  const drafts = taken.map((one, index): PromiseSettledResult<Draft> => {
    try {
      const of = authorities[index];
      const first = firsts[index];
      const proof = proofs[index];
      if (of === undefined || first === undefined) throw new ChainError("no plan read");
      if (proof === undefined) throw new Error("no proof");
      if (!Array.isArray(of)) throw of;
      const holds = payeeHolds.has(one.token);
      const reads = answers.slice(first);
      const made = isSponsorship(one)
        ? sponsoredDraft(chain.chainId, one, proof, of, reads, tokens, schedule, holds, payeeNew)
        : draftFrom(chain.chainId, one, proof, of, reads, tokens, schedule, holds);
      const value = { ...made, salt: salts?.[index] ?? STAND_IN_SALT };
      return { status: "fulfilled", value };
    } catch (reason) {
      return { status: "rejected", reason };
    }
  });
  return await planDrafts(chain, drafts, schedule, sender);
}

/** Calls as they're planned, from each one's own `reads`, in order (boundReads): a call the
 * relay bounds given its bound, any other the gas it states. */
function plannedCalls(
  chainId: number,
  given: readonly SubmittedCall[],
  reads: readonly Answer[],
  tokens: ReadonlyMap<Address, Erc20 | null>,
  schedule: GasSchedule,
  authorities: readonly Address[],
): PlannedCall[] {
  let at = 0;
  return given.map((each, index) => {
    const its = boundReads(chainId, each.call, "").length;
    const bound = callBound(
      chainId,
      each.call,
      reads.slice(at, at + its),
      tokens,
      schedule,
      authorities,
    );
    at += its;
    return plannedCall(index, each, bound);
  });
}

/**
 * A sponsorship's draft from its `reads`, which start a batch's rest: its account's balance,
 * nonce and code, then its calls' accounts, the account among them, then each call's own.
 */
function sponsoredDraft(
  chainId: number,
  sponsorship: Sponsorship,
  proof: Proof,
  authorities: readonly Address[],
  reads: readonly Answer[],
  tokens: ReadonlyMap<Address, Erc20 | null>,
  schedule: GasSchedule,
  payeeHolds: boolean,
  payeeNew: boolean,
): Draft {
  if (!(proof instanceof CheckedByForwarder)) {
    throw new ProofError("a sponsorship's payment is checked by the forwarder");
  }
  const account = sponsorship.account;
  const own = reads.slice(0, 3);
  const code = own[2];
  if (code === undefined) throw new ChainError("an account's reads: no code");
  const targets = [...sponsorship.calls.map((given) => given.call.to), account];
  const at = 3 + targets.length + 3 * authorities.length;
  return sponsored(
    sponsorship,
    proof,
    authorities,
    accountsOf(targets, authorities, reads.slice(3, at)),
    tokenOf(sponsorship.token, tokens),
    schedule,
    plannedCalls(chainId, sponsorship.calls, reads.slice(at), tokens, schedule, authorities),
    {
      accountNew: emptyAmong(own) === 1,
      accountHasCode: hasCode(resultOf(code)),
      payeeHolds,
      payeeNew,
    },
  );
}

/**
 * A sponsorship's draft, of its `calls` as planned: the ETH its account is sent, a plain send
 * unless the account has code or the request delegates it (`accountHasCode`, its `authorities`);
 * its own transactions; and the gas the forwarder's check takes (checkGas).
 */
export function sponsored(
  sponsorship: Sponsorship,
  proof: CheckedByForwarder,
  authorities: readonly Address[],
  accounts: CallAccounts,
  erc20: Erc20 | null,
  schedule: GasSchedule,
  calls: readonly PlannedCall[],
  { accountNew, accountHasCode, payeeHolds, payeeNew }: {
    accountNew: boolean;
    accountHasCode: boolean;
    payeeHolds: boolean;
    payeeNew: boolean;
  },
): Draft<CheckedByForwarder> {
  const account = sponsorship.account;
  const plain = !accountHasCode && !authorities.includes(account);
  return {
    ...draftOf(sponsorship, proof, authorities, accounts, erc20, calls),
    funding: { account, amount: funding(sponsorship), new: accountNew, plain },
    transactions: sponsorship.transactions,
    checkingGas: checkGas(schedule, erc20, { payeeHolds, payeeNew }),
  };
}

/**
 * A submission's draft from its `reads`, which start a batch's rest: its call's accounts, then
 * its payer's code, if it's paid by a transfer, then each call's own, if the relay may bound it
 * (boundReads).
 */
function draftFrom(
  chainId: number,
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
  const targets = submission.calls.map((given) => given.call.to);
  let at = targets.length + 3 * authorities.length;
  const accounts = accountsOf(targets, authorities, reads.slice(0, at));
  let payerHasCode = false;
  if (submission.transfer !== undefined) {
    const payerCode = reads[at];
    if (payerCode === undefined) throw new ChainError("no plan read");
    payerHasCode = hasCode(resultOf(payerCode));
    at += 1;
  }
  const calls = plannedCalls(
    chainId,
    submission.calls,
    reads.slice(at),
    tokens,
    schedule,
    authorities,
  );
  return drafted(
    submission,
    proof as DraftedProof,
    authorities,
    accounts,
    tokenOf(submission.token, tokens),
    schedule,
    calls,
    { payerHasCode, payeeHolds },
  );
}

/**
 * A signed submission's draft, of its `calls` as planned: its transfer, if any, its gas bounded
 * for a key's signature (from its token's measurement, knowing whether the payee holds some of
 * it, `payeeHolds`), estimated for a contract's (a payer with code or one of the submission's
 * authorities, or a signature that isn't a key's), whose check is its
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
  calls: readonly PlannedCall[],
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
    const contractPayer = !transferEcdsa(signed) || payerHasCode || wrapperDeployment !== null ||
      authorities.includes(signed.payer);
    const gas = contractPayer ? null : transferGas(erc20.transferGas, schedule, payeeHolds);
    transfer = { calldata: transferCalldata(signed), gas };
    if (wrapperDeployment !== null && payerHasCode) deployment = submission.deployment ?? null;
  }
  return {
    ...draftOf(submission, proof, authorities, accounts, erc20, calls),
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

/** Calldata of `data`'s length at its dearest: no byte zero. */
function dearest(data: Uint8Array): Uint8Array {
  return new Uint8Array(data.length).fill(0xff);
}

/**
 * A sponsorship's transactions. The relay's own first: the ETH its account's transactions need, a
 * plain send where the account has no code (Funding); its calls, each sent directly where every
 * one may be, else all of them in a runner that checks no payment, the ETH sent from it too
 * where the account has code. Then the account's own, as signed. Then the forwarder's call for
 * the amount (checkGas). Every calldata of the relay's is priced as if it had no zero byte: a
 * call may hold what it pays, and the check its amount, so what a sponsorship is charged mustn't
 * change with what it pays.
 */
function forwarderChecked(
  chainId: number,
  draft: Draft,
  schedule: GasSchedule,
  proof: Proof,
): Plan {
  const taken = draft.submission;
  const funding = draft.funding;
  const steps: Step[] = [];
  let calls = draft.calls;
  if (funding !== null && funding.amount > 0n && funding.plain) {
    const send: EncodedCall = {
      to: funding.account,
      data: new Uint8Array(0),
      value: funding.amount,
    };
    steps.push({
      transaction: send,
      proof,
      gasLimit: transferOf(schedule, funding.new),
      size: unsignedSize(chainId, send.to, send.data, send.value, 0),
      authorizations: [],
      estimated: false,
    });
  }
  const through = funding !== null && throughRunner(funding);
  if (funding !== null && through) {
    // given the gas left over: the account's code is its own, of no bound
    const sending: EncodedCall = {
      to: funding.account,
      data: new Uint8Array(0),
      value: funding.amount,
    };
    calls = [{ call: sending, gas: 0, bounded: false }, ...calls];
  }
  if (through || calls.some((planned) => !direct(chainId, planned.call))) {
    steps.push(runnerStep(chainId, draft, calls, schedule, proof, through));
  } else {
    steps.push(...directSteps(chainId, draft, calls, schedule, proof, true));
  }
  const check = forwarding(taken.payee, taken.token, taken.amount);
  const widest = forwarding(taken.payee, taken.token, WIDEST_AMOUNT);
  const checking = intrinsic(schedule, widest.data);
  const last: Step = {
    transaction: check,
    proof,
    gasLimit: Math.max(checking.standard + draft.checkingGas, checking.floor),
    size: unsignedSize(chainId, check.to, check.data, 0n, 0),
    authorizations: [],
    estimated: false,
  };
  return {
    steps: [...steps, ...draft.transactions.map((transaction) => ({ transaction })), last],
  };
}

/**
 * The transaction of a runner that makes `calls` and checks no payment, as a sponsorship's is:
 * its gas limit from the gas schedule, its calldata priced at its dearest, only a placeholder if
 * it's `estimated` (a call of it given the gas left over).
 */
function runnerStep(
  chainId: number,
  draft: Draft,
  calls: readonly PlannedCall[],
  schedule: GasSchedule,
  proof: Proof,
  estimated: boolean,
): Step {
  const taken = draft.submission;
  const payment: Payment = { token: draft.erc20, payee: taken.payee, amount: taken.amount };
  let runner: Runner;
  try {
    runner = estimatingRunner(calls, payment, schedule, { delegated: draft.accounts.delegated });
  } catch (e) {
    if (e instanceof RunnerError) {
      throw new InvalidSubmissionError("calls", refused("runner-unbuilt", { why: e.message }));
    }
    throw e;
  }
  const value = calls.reduce((total, planned) => total + planned.call.value, 0n);
  const authorizations = draft.authorities.length;
  const gasLimit = transactionGas(
    dearest(runner.initcode),
    frameGas(runner, calls),
    schedule,
    authorizations,
    draft.accounts.empty,
    value > 0n,
  );
  const transaction = runnerCall(draft.salt, runner.initcode, value);
  const size = unsignedSize(
    chainId,
    transaction.to,
    transaction.data,
    transaction.value,
    authorizations,
  );
  return {
    transaction,
    proof,
    gasLimit,
    size,
    authorizations: taken.authorizations,
    estimated,
  };
}

/**
 * `calls`, in order, as the concurrency account's own transactions. Each run of consecutive
 * calls the relay bounds is one transaction, Multicall3's aggregate3 of them, every one required
 * to succeed: its gas limit Multicall3's own gas (multicall.ts's ownGas), the calls' together,
 * and a 63rd of the largest, since Multicall3 gives a call all but a 64th of what's left
 * (EIP-150). Any other call, and a bounded one alone, is a transaction as it is, its gas limit
 * its gas with the transaction's own. A transaction's calldata is priced as if no byte were
 * zero, if `dearestData`; the first carries the authorizations, and their gas.
 */
function directSteps(
  chainId: number,
  draft: Draft,
  calls: readonly PlannedCall[],
  schedule: GasSchedule,
  proof: Proof,
  dearestData = false,
): Step[] {
  // each run of consecutive bounded calls together, any other call alone
  const together: PlannedCall[][] = [];
  for (const planned of calls) {
    const last = together.at(-1);
    if (planned.bounded && last !== undefined && last.every((each) => each.bounded)) {
      last.push(planned);
    } else {
      together.push([planned]);
    }
  }
  return together.map((group, index): Step => {
    const [first] = group;
    if (first === undefined) throw new ProofError("a group of no calls");
    let call = first.call;
    let execution = first.gas;
    if (group.length > 1) {
      const grouped = group.map((planned) => planned.call);
      let own: number;
      try {
        call = aggregate3(grouped);
        own = ownGas(grouped, schedule);
      } catch (e) {
        if (e instanceof GroupError) throw new ProofError(e.message);
        throw e;
      }
      const gas = group.map((planned) => planned.gas);
      execution = own + gas.reduce((total, each) => total + each, 0) +
        Math.ceil(Math.max(...gas) / 63);
    }
    // the authorizations go with the first transaction: every later one finds them applied
    const authorizations = index === 0 ? draft.authorities.length : 0;
    const empty = index === 0 ? draft.accounts.empty : 0;
    const priced = dearestData ? dearest(call.data) : call.data;
    const calling = intrinsic(schedule, priced, { authorizations });
    execution += authorizationsGas(schedule, empty, authorizations);
    return {
      transaction: call,
      proof,
      gasLimit: Math.max(calling.standard + execution, calling.floor),
      size: unsignedSize(chainId, call.to, call.data, 0n, authorizations),
      authorizations: index === 0 ? draft.submission.authorizations : [],
      estimated: false,
    };
  });
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
    runner = inCallRunner(draft.calls, payment, schedule, {
      delegated: draft.accounts.delegated,
      deployment: draft.deployment,
      prepare: draft.prepare,
    });
  } catch (e) {
    if (e instanceof RunnerError) {
      throw new InvalidSubmissionError("calls", refused("runner-unbuilt", { why: e.message }));
    }
    throw e;
  }
  const authorizations = draft.authorities.length;
  const gasLimit = transactionGas(
    runner.initcode,
    frameGas(runner, draft.calls) + (draft.deployGas ?? 0),
    schedule,
    authorizations,
    draft.accounts.empty,
    sent(draft) > 0n,
  );
  const transaction = runnerCall(draft.salt, runner.initcode, sent(draft));
  const size = unsignedSize(
    chainId,
    transaction.to,
    transaction.data,
    transaction.value,
    authorizations,
  );
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

/** The ETH the draft's calls send, together: what its runner's transaction carries. */
function sent(draft: Draft): bigint {
  return draft.calls.reduce((total, planned) => total + planned.call.value, 0n);
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
    return estimatingRunner(draft.calls, payment, schedule, {
      delegated: draft.accounts.delegated,
      deployment: draft.deployment,
    });
  } catch (e) {
    if (e instanceof RunnerError) {
      throw new InvalidSubmissionError("calls", refused("runner-unbuilt", { why: e.message }));
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
  const planned = handleOpsOf(draft);
  const call = planned.call;
  const authorizations = draft.authorities.length;
  const { standard, floor } = intrinsic(schedule, call.data, { authorizations });
  const beneficiary = schedule.coldAccountAccess - WARM_ACCOUNT_ACCESS;
  const execution = planned.gas + beneficiary +
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
 * The draft's calls, in order, then its transfer, as the concurrency account's own transactions
 * (directSteps). A transfer whose gas is bounded (a key's) is the last of the calls the relay
 * bounds, so it's grouped with those before it: given its bound and a 63rd of it more, since the
 * bound is a zero-value transfer's gas, measured, with the balances' writes added, and what a
 * token spends it spends below its own frame (a proxy's implementation), given all but a 64th of
 * what's left (EIP-150). A runner's transfer needs none: the runner reads the payee's balance
 * first, which leaves the transfer that much warm. One whose gas isn't known (a contract
 * payer's) is a transaction of its own, estimated, alone, as the chain stands before the calls.
 */
function transferPlanned(chainId: number, draft: Draft, schedule: GasSchedule, proof: Proof): Plan {
  const { submission } = draft;
  if (draft.transfer === null) throw new ProofError("paid by a transfer, and drafted without one");
  const transfer = { to: submission.token, data: draft.transfer.calldata, value: 0n };
  if (draft.transfer.gas !== null) {
    const paying = { call: transfer, gas: callNeed(draft.transfer.gas), bounded: true };
    return { steps: directSteps(chainId, draft, [...draft.calls, paying], schedule, proof) };
  }
  const made = directSteps(chainId, draft, draft.calls, schedule, proof);
  const transferring = intrinsic(schedule, transfer.data, {});
  const paid: Step = {
    transaction: transfer,
    proof,
    gasLimit: Math.max(transferring.standard, transferring.floor),
    size: unsignedSize(chainId, transfer.to, transfer.data, 0n, 0),
    authorizations: [],
    estimated: true,
  };
  return { steps: [...made, paid] };
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
    drafts.flatMap(({ draft, plan }) => estimateCalls(draft, plan, sender, at)),
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

/**
 * One eth_estimateGas for each of `plan`'s steps to be estimated, as it will run. A step after
 * the one that carries the draft's authorizations finds them applied, so it's estimated with
 * each authority's code as its delegation leaves it: a transfer a delegated key signed is
 * checked by its account's new code, which costs more than recovering the key.
 */
function estimateCalls(draft: Draft, plan: Plan, sender: Address, block: string): Call[] {
  const calls: Call[] = [];
  let applied = false;
  for (const step of planOwn(plan)) {
    if (step.estimated) {
      const code = applied ? delegated(draft) : new Map<Address, Hex>();
      const transaction = transactionJson(step, sender);
      calls.push(estimateCall(transaction, sender, step.transaction.value, block, code));
    }
    applied = applied || step.authorizations.length > 0;
  }
  return calls;
}

/**
 * The code each of the draft's authorities has once its authorizations are applied: its
 * delegation's designator (EIP-7702), or none where the delegate is the zero address, which
 * clears it. A later authorization of the same authority's is the one that stands.
 */
function delegated(draft: Draft): Map<Address, Hex> {
  const code = new Map<Address, Hex>();
  draft.submission.authorizations.forEach((signed, index) => {
    const authority = draft.authorities[index];
    if (authority === undefined) throw new ProofError("an authorization with no authority");
    const delegate = signed.address.slice(2).toLowerCase();
    code.set(authority, /^0*$/.test(delegate) ? "0x" : `${DELEGATION_PREFIX}${delegate}` as Hex);
  });
  return code;
}

/** eth_estimateGas of `transaction` at `block`. One sending ETH is estimated with its `sender`
 * holding that much (a state override), as its simulation is: the account it's planned as sent
 * from holds nothing of its own to send. Each account in `code` is given that code, likewise. */
function estimateCall(
  transaction: Record<string, unknown>,
  sender: Address,
  value: bigint,
  block: string,
  code: ReadonlyMap<Address, Hex> = new Map(),
): Call {
  const overrides: Record<string, Record<string, string>> = {};
  for (const [account, its] of code) overrides[account] = { code: its };
  if (value !== 0n) overrides[sender] = { ...overrides[sender], balance: hexQuantity(value) };
  if (Object.keys(overrides).length === 0) {
    return { method: "eth_estimateGas", params: [transaction, block] };
  }
  return { method: "eth_estimateGas", params: [transaction, block, overrides] };
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
  index: number,
  schedule: GasSchedule,
  sender: Address,
): Promise<number> {
  const transaction = draft.proof.estimating(draft, index, schedule);
  const tx = callJson(transaction, sender, draft.submission.authorizations);
  const [answer] = await chain.batch([estimateCall(tx, sender, transaction.value, "latest")]);
  if (answer === undefined) throw new ChainError("no estimate");
  if ("error" in answer) {
    if (answer.error.code === REVERTED) {
      throw new SimulationFailedError(revertData(answer.error));
    }
    throw answer.error;
  }
  return draft.proof.callGasOf(Number(quantity(answer.result)), draft, index, schedule);
}
