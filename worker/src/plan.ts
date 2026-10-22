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
import { calldata, transferGas } from "./eip3009.ts";
import {
  type Erc20,
  frameGas,
  inCallRunner,
  type Payment,
  type Runner,
  RunnerError,
} from "./runner.ts";
import { InvalidSubmissionError, type Submission } from "./submission.ts";
import { authority, authorizationJson } from "./transactions.ts";

const DELEGATION_PREFIX = "0xef0100";

/** The submission's transaction reverts: with `revertData`. */
export class SimulationFailedError extends Error {
  constructor(readonly revertData: Uint8Array) {
    super("simulation failed");
  }
}

/** A submission's runner and its transaction's gas limit, under a block's schedule. */
export interface Plan {
  readonly runner: Runner;
  readonly gasLimit: number;
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
 * call's target's code; each authority's balance, nonce and code): each its plan, or why not.
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
    }
    return first;
  });
  const answers = await chain.batch(calls);
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
          submission,
          schedule,
          erc20,
          payeeHolds.has(submission.token),
          of,
          delegated,
          empty,
        ),
      };
    } catch (reason) {
      return { status: "rejected", reason };
    }
  });
}

function planned(
  submission: Submission,
  schedule: GasSchedule,
  erc20: Erc20 | null,
  payeeHolds: boolean,
  authorities: readonly Address[],
  delegated: ReadonlySet<Address>,
  empty: number,
): Plan {
  let payment: Payment = { token: erc20, payee: submission.payee, amount: submission.amount };
  if (submission.transfer !== undefined) {
    if (erc20 === null || erc20.transferGas === undefined) {
      throw new InvalidSubmissionError(
        "payment",
        `${submission.token} takes no EIP-3009 transfer`,
      );
    }
    const gas = transferGas(erc20.transferGas, schedule, payeeHolds);
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
  return { runner, gasLimit };
}

/** How many of `authorities` are empty (no balance, no nonce, no code), read in one batch. */
export async function empty(chain: Chain, authorities: readonly Address[]): Promise<number> {
  const answers = await chain.batch(authorities.flatMap(authorityReads));
  return countEmpty(answers, 0, authorities.length);
}

/** The submission's transaction as eth_call from `sender`: throw its revert data. */
export async function simulate(
  chain: Chain,
  submission: Submission,
  plan: Plan,
  sender: Address,
): Promise<void> {
  const [revert] = await simulations(chain, [{ submission, plan, sender }]);
  if (revert !== null && revert !== undefined) {
    throw new SimulationFailedError(revert);
  }
}

/** Each submission's transaction as eth_call from its sender, in one batch: each its revert
 * data, or null if it doesn't revert. */
export async function simulations(
  chain: Chain,
  sims: readonly Simulation[],
): Promise<(Uint8Array | null)[]> {
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
    return { method: "eth_call", params: [tx, "latest"] };
  }));
  return answers.map((answer) => {
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
