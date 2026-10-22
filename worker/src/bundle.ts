/**
 * Bundles (DESIGN.md, Bundling), and the float's funding transaction: a contract creation whose
 * initcode sends each concurrency account its top-up and self-destructs to the float. A port of the Python
 * reference (bruno/bundle.py): the same bytes, the same parameters.
 */

import { Assembly, Op } from "./assembler.ts";
import { type Address, hex } from "./evm.ts";
import { type GasSchedule, intrinsic } from "./forks.ts";
import { type Fees, maxCost, sign, type SignedTransaction, type Signer } from "./transactions.ts";

export class BundleError extends Error {}

/** How a builder is sent bundles. */
export type Mode = "dropping" | "standalone";

/** ETH for a concurrency account; `newAccount` if the transfer creates it. */
export interface TopUp {
  readonly account: Address;
  readonly amount: bigint;
  readonly newAccount: boolean;
}

/** eth_sendBundle's parameters. */
export interface BundleParams {
  readonly txs: string[];
  readonly blockNumber: string;
  readonly droppingTxHashes?: string[];
}

/** What `tx`'s sender lacks, holding `balance`, for `tx` to be valid at its max fee. */
export function shortfall(tx: SignedTransaction, balance: bigint): bigint {
  const lacking = maxCost(tx) - balance;
  return lacking > 0n ? lacking : 0n;
}

/**
 * Initcode that sends each top-up (value CALLs given only the stipend: the accounts are EOAs)
 * and returns no code, reverting if any transfer fails; and its most gas.
 */
export function fundingInitcode(
  topUps: readonly TopUp[],
  schedule: GasSchedule,
): { code: Uint8Array; gas: number } {
  if (topUps.length === 0) {
    throw new BundleError("a funding transaction tops up at least one account");
  }
  const asm = new Assembly();
  for (const topUp of topUps) {
    // CALL's arguments, last pushed first: gas 0, to, value, no args, no return
    asm.push(0).push(0).push(0).push(0).push(topUp.amount).pushAddress(topUp.account);
    const created = topUp.newAccount ? schedule.newAccount : 0;
    asm.push(0).valueCall(schedule.coldAccountAccess + schedule.callValue + created);
    asm.op(Op.ISZERO).jumpi("failed");
  }
  asm.op(Op.ORIGIN, Op.SELFDESTRUCT);
  asm.end();
  asm.label("failed");
  asm.push(0).push(0).op(Op.REVERT);
  return asm.assemble([]);
}

/** The float's funding transaction: a creation carrying the top-ups' value. */
export function signFunding(
  float: Signer,
  chainId: number,
  nonce: number,
  fees: Fees,
  topUps: readonly TopUp[],
  schedule: GasSchedule,
): SignedTransaction {
  const { code, gas: execution } = fundingInitcode(topUps, schedule);
  const { standard, floor } = intrinsic(schedule, code, { creation: true });
  const value = topUps.reduce((sum, t) => sum + t.amount, 0n);
  return sign(
    float,
    chainId,
    nonce,
    fees,
    Math.max(standard + execution, floor),
    null,
    code,
    value,
  );
}

/**
 * One block's bundle: the funding transaction (none if no account needs ETH), then the
 * submissions', every submission's in droppingTxHashes. No `version`: rbuilder takes
 * droppingTxHashes only from a bundle with none, or v2 and later.
 */
export function bundleParams(
  blockNumber: number,
  funding: SignedTransaction | null,
  submissions: readonly SignedTransaction[],
): BundleParams {
  const txs = funding === null ? submissions : [funding, ...submissions];
  return {
    txs: txs.map((tx) => hex(tx.raw)),
    blockNumber: `0x${blockNumber.toString(16)}`,
    droppingTxHashes: submissions.map((tx) => hex(tx.hash)),
  };
}

/** A submission's transaction alone, nothing listed: a revert fails the bundle. */
export function standaloneParams(blockNumber: number, submission: SignedTransaction): BundleParams {
  return { txs: [hex(submission.raw)], blockNumber: `0x${blockNumber.toString(16)}` };
}

/** `txs`, all of them or none: nothing listed, so any one reverting fails the bundle. Every
 * builder takes it in either mode. */
export function atomicParams(blockNumber: number, txs: readonly SignedTransaction[]): BundleParams {
  return { txs: txs.map((tx) => hex(tx.raw)), blockNumber: `0x${blockNumber.toString(16)}` };
}

/** The eth_sendBundle parameters for one block in `mode`. */
export function bundles(
  mode: Mode,
  blockNumber: number,
  funding: SignedTransaction | null,
  submissions: readonly SignedTransaction[],
): BundleParams[] {
  if (mode === "dropping") {
    return [bundleParams(blockNumber, funding, submissions)];
  }
  if (funding !== null) {
    throw new BundleError("standalone bundles carry no funding: accounts are funded beforehand");
  }
  return submissions.map((tx) => standaloneParams(blockNumber, tx));
}
