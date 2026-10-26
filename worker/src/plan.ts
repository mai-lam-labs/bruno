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
import { calldata, ecdsa, measured, transferGas } from "./eip3009.ts";
import {
  type Erc20,
  frameGas,
  inCallRunner,
  type Payment,
  type Runner,
  RunnerError,
} from "./runner.ts";
import { InvalidSubmissionError, type Submission } from "./submission.ts";
import { authority, authorizationJson, unsignedSize } from "./transactions.ts";

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
 * bytes its transaction takes unsigned (what's paid beyond gas is by size). */
export interface Plan {
  readonly runner: Runner;
  readonly gasLimit: number;
  readonly size: number;
}

/** A submission's transaction, as planned, from `sender`. */
export interface Simulation {
  readonly submission: Submission;
  readonly plan: Plan;
  readonly sender: Address;
}

/** The submission's runner and gas limit under `schedule`, paying in its token as `tokens`
 * reads it; a transfer's gas bounded knowing which tokens the payee holds some of. */
export async function makePlan(
  chain: Chain,
  submission: Submission,
  schedule: GasSchedule,
  tokens: ReadonlyMap<Address, Erc20 | null>,
  payeeHolds: ReadonlySet<Address> = new Set(),
): Promise<Plan> {
  return settled((await makePlans(chain, [submission], schedule, tokens, payeeHolds))[0]);
}

/**
 * Each submission's runner and gas limit, under `schedule`, their reads in one batch (its
 * call's target's code; each authority's balance, nonce and code; a payer's code): each its
 * plan, or why not. A key's transfer's gas is bounded from its token's measurement; a contract
 * payer's (code, or a signature that isn't a key's) is its own transfer's, estimated in a
 * second batch. An estimate that fails leaves the key's bound, and the simulation the refusal.
 */
export async function makePlans(
  chain: Chain,
  submissions: readonly Submission[],
  schedule: GasSchedule,
  tokens: ReadonlyMap<Address, Erc20 | null>,
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
  const contractGas = await contractPayers(
    chain,
    submissions,
    authorities,
    firsts,
    answers,
    schedule,
  );
  return submissions.map((submission, i): PromiseSettledResult<Plan> => {
    try {
      const of = authorities[i];
      const first = firsts[i];
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
      const erc20 = tokens.get(submission.token) ?? null;
      return {
        status: "fulfilled",
        value: planned(
          chain.chainId,
          submission,
          schedule,
          erc20,
          payeeHolds.has(submission.token),
          of,
          delegated,
          empty,
          contractGas.get(i),
        ),
      };
    } catch (reason) {
      return { status: "rejected", reason };
    }
  });
}

function planned(
  chainId: number,
  submission: Submission,
  schedule: GasSchedule,
  erc20: Erc20 | null,
  payeeHolds: boolean,
  authorities: readonly Address[],
  delegated: ReadonlySet<Address>,
  empty: number,
  contractTransferGas?: number,
): Plan {
  let payment: Payment = { token: erc20, payee: submission.payee, amount: submission.amount };
  if (submission.transfer !== undefined) {
    if (erc20 === null || erc20.transferGas === undefined) {
      throw new InvalidSubmissionError(
        "payment",
        `${submission.token} takes no EIP-3009 transfer`,
      );
    }
    const gas = contractTransferGas ?? transferGas(erc20.transferGas, schedule, payeeHolds);
    payment = { ...payment, transfer: { calldata: calldata(submission.transfer), gas } };
  }
  let runner: Runner;
  try {
    runner = inCallRunner(submission.call, payment, schedule, delegated);
  } catch (e) {
    if (e instanceof RunnerError) throw new InvalidSubmissionError("call", e.message);
    throw e;
  }
  const gasLimit = transactionGas(
    runner.initcode,
    frameGas(runner, submission.gas),
    schedule,
    authorities.length,
    empty,
  );
  const size = unsignedSize(chainId, DEPLOYER, runner.initcode, 0n, authorities.length);
  return { runner, gasLimit, size };
}

/** Each contract payer's transfer's gas, by its submission's index: estimated as it will run
 * (its value, the state now), less its intrinsic gas (eip3009.measured). A payer is a contract if
 * it has code, or signed with something other than a key's 65 bytes. */
async function contractPayers(
  chain: Chain,
  submissions: readonly Submission[],
  authorities: readonly (Address[] | InvalidSubmissionError)[],
  firsts: readonly number[],
  answers: readonly Answer[],
  schedule: GasSchedule,
): Promise<Map<number, number>> {
  const estimating: { index: number; token: Address; data: Uint8Array }[] = [];
  for (const [index, submission] of submissions.entries()) {
    const transfer = submission.transfer;
    const of = authorities[index];
    const first = firsts[index];
    if (transfer === undefined || !Array.isArray(of) || first === undefined) continue;
    const code = answers[first + 1 + 3 * of.length];
    if (code === undefined || "error" in code) continue; // unread: the key's bound
    const hasCode = code.result !== "0x" && code.result !== "0x0";
    if (!ecdsa(transfer) || hasCode) {
      estimating.push({ index, token: submission.token, data: calldata(transfer) });
    }
  }
  const gas = new Map<number, number>();
  if (estimating.length === 0) return gas;
  const estimates = await chain.batch(
    estimating.map(({ token, data }) => ({
      method: "eth_estimateGas",
      params: [{ to: token, data: hex(data) }],
    })),
  );
  for (const [i, { index, data }] of estimating.entries()) {
    const estimate = estimates[i];
    if (estimate !== undefined && "result" in estimate) {
      gas.set(index, measured(Number(quantity(estimate.result)), data, schedule));
    }
  }
  return gas;
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
  const answers = await chain.batch(sims.map(({ submission, plan, sender }) => {
    const tx: Record<string, unknown> = {
      from: sender,
      to: DEPLOYER,
      data: hex(plan.runner.initcode),
      gas: `0x${plan.gasLimit.toString(16)}`,
    };
    if (submission.authorizations.length > 0) {
      tx.authorizationList = submission.authorizations.map(authorizationJson);
    }
    return { method: "eth_call", params: [tx, at] };
  }));
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
  return answer.error.code === RESOURCE_NOT_FOUND ||
    answer.error.message.endsWith(` ${HEADER_NOT_FOUND}`);
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
