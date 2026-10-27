/**
 * The runner: initcode generated per submission and deployed through the deployer. It makes the
 * client's one call and asserts the payment (docs/development/design/runner.md). A port of the Python reference
 * (bruno/runner.py): the same bytes, the same bounds.
 */

import { Assembly, AssemblyError, Op } from "./assembler.ts";
import type { Deployment } from "./erc6492.ts";
import { type Address, type EncodedCall, keccak, utf8, WARM_ACCOUNT_ACCESS } from "./evm.ts";
import type { GasSchedule } from "./forks.ts";

/** EIP-3860's limit on initcode: 2 x the 24,576-byte code limit. */
export const MAX_INITCODE_BYTES = 49_152;

/** The standard's error namespace: its ERC number, once assigned. */
export const ERC = "ERCNNNN";

function selector(signature: string): Uint8Array {
  return keccak(utf8(signature)).slice(0, 4);
}

export const UNDERPAID = selector(`${ERC}_Underpaid()`);
export const BALANCE_UNREADABLE = selector(`${ERC}_BalanceUnreadable()`);
export const GAS_EXHAUSTED = selector(`${ERC}_GasExhausted()`);
const BALANCE_OF = selector("balanceOf(address)");
const MAX_UINT256 = (1n << 256n) - 1n;

export class RunnerError extends Error {}

/**
 * A payment token, and the most gas its balanceOf may take: the runner gives it exactly that. An
 * EIP-3009 token's `transferGas` is a zero-value transferWithAuthorization's, measured.
 */
export interface Erc20 {
  readonly address: Address;
  readonly balanceGas: number;
  readonly transferGas?: number;
}

/**
 * A call to the payment's token that the runner makes before the client's: its calldata, given
 * exactly `gas` (a key's EIP-3009 transfer, its gas bounded), or null: the gas left over (a
 * contract's, whose signature check is its own code).
 */
export interface Transfer {
  readonly calldata: Uint8Array;
  readonly gas: number | null;
}

/**
 * What the payee must receive during the client's call: `amount` of `token`, or of ETH; and a
 * transfer to make first, if the payment comes with one.
 */
export interface Payment {
  readonly token: Erc20 | null;
  readonly payee: Address;
  readonly amount: bigint;
  readonly transfer?: Transfer;
}

/** A runner's initcode, and the most gas the runner itself takes, the client's call aside. */
export interface Runner {
  readonly initcode: Uint8Array;
  readonly overheadGas: number;
}

/**
 * The gas the runner's frame needs for the client's call to be given `clientGas`: its own, and
 * what the call needs (callNeed). A runner with a step given the gas left over needs that step's
 * too, which only an estimate knows.
 */
export function frameGas(runner: Runner, clientGas: number): number {
  return runner.overheadGas + callNeed(clientGas);
}

/**
 * What the call needs left above its R to be given `clientGas`: the gas, and what EIP-150's
 * 63/64 rule keeps back from a call given all that's left.
 */
export function callNeed(clientGas: number): number {
  return clientGas + Math.ceil(clientGas / 63);
}

/** Each CALL's index among the runner's GAS readings: its R measured from its own. */
const CALL = 0;
const DEPLOYMENT = 1;
const TRANSFER = 2;

/** What a runner's made with besides its call and payment: each optional. */
export interface RunnerOptions {
  /** The targets that are, or by the transaction's authorization list will be, delegated. */
  readonly delegated?: ReadonlySet<Address>;
  /** An account to deploy first, if it has no code (always, to `prepare` a deployed one). */
  readonly deployment?: Deployment | null;
  readonly prepare?: boolean;
}

/**
 * The runner for `call`, given at least `clientGas`, and `payment`, its gas bounded by
 * `schedule`; the deployment made first, if its account has no code (always, to prepare a
 * deployed one). `delegated` are the targets that are, or by the transaction's authorization
 * list will be, EIP-7702 delegated.
 */
export function inCallRunner(
  call: EncodedCall,
  payment: Payment,
  schedule: GasSchedule,
  clientGas: number,
  options: RunnerOptions = {},
): Runner {
  if (call.value !== 0n) {
    throw new RunnerError("the client's call carries no value: the runner holds no ETH");
  }
  if (!(payment.amount > 0n && payment.amount <= MAX_UINT256)) {
    throw new RunnerError(`payment amount ${payment.amount} isn't a positive uint256`);
  }
  const delegated = options.delegated ?? new Set();
  const need = callNeed(clientGas);
  const asm = new Assembly();
  const blobs = [call.data];
  if (payment.transfer !== undefined) blobs.push(payment.transfer.calldata);
  const deployment = options.deployment ?? null;
  if (deployment !== null) {
    blobs.push(deployment.factoryData);
    deploy(
      asm,
      deployment,
      blobs.length - 1,
      schedule.coldAccountAccess,
      need,
      options.prepare ?? false,
    );
  }
  balance(asm, payment, schedule.coldAccountAccess); // stack: before
  if (payment.transfer !== undefined) {
    if (payment.token === null) {
      throw new RunnerError("a transfer pays in a token, not ETH");
    }
    transfer(asm, payment.token.address, payment.transfer, need);
  }
  clientCall(asm, call, schedule.coldAccountAccess * (delegated.has(call.to) ? 2 : 1), need);
  balance(asm, payment, WARM_ACCOUNT_ACCESS); // stack: before after
  // after < before: the payee lost funds in the call
  asm.op(Op.DUP2, Op.DUP2, Op.LT).jumpi("underpaid");
  asm.op(Op.SUB); // stack: after - before
  asm.push(payment.amount).op(Op.DUP2, Op.LT).jumpi("underpaid");
  asm.op(Op.ORIGIN, Op.SELFDESTRUCT);
  asm.end();
  callFailed(asm);
  revertWith(asm, "underpaid", UNDERPAID);
  revertWith(asm, "balance_unreadable", BALANCE_UNREADABLE);
  revertWith(asm, "gas_exhausted", GAS_EXHAUSTED);
  let assembled: { code: Uint8Array; gas: number };
  try {
    assembled = asm.assemble(blobs);
  } catch (e) {
    if (e instanceof AssemblyError) throw new RunnerError(e.message);
    throw e;
  }
  const { code, gas } = assembled;
  if (code.length > MAX_INITCODE_BYTES) {
    throw new RunnerError(
      `initcode is ${code.length} bytes, over EIP-3860's ${MAX_INITCODE_BYTES}`,
    );
  }
  return { initcode: code, overheadGas: gas };
}

function word(selector: Uint8Array): bigint {
  let v = 0n;
  for (const b of selector) {
    v = (v << 8n) | BigInt(b);
  }
  return v << 224n;
}

/** Push the payee's balance, the payee and token costing `access`. */
function balance(asm: Assembly, payment: Payment, access: number): void {
  if (payment.token === null) {
    asm.pushAddress(payment.payee).balance(access);
    return;
  }
  // memory[0:36] = balanceOf(payee); its answer to memory[0:32]
  asm.push(word(BALANCE_OF)).mstoreAt(0);
  asm.pushAddress(payment.payee).mstoreAt(4);
  asm.push(32).push(0).push(36).push(0).pushAddress(payment.token.address);
  asm.push(payment.token.balanceGas).staticcall(access, payment.token.balanceGas);
  asm.op(Op.ISZERO).jumpi("balance_unreadable");
  // a token with no code answers nothing: success, but no 32 bytes
  asm.op(Op.RETURNDATASIZE).push(32).op(Op.EQ, Op.ISZERO).jumpi("balance_unreadable");
  asm.mloadAt(0);
}

/**
 * Call the deployment's factory with its data (blob `blob`), given the gas left over, unless its
 * account has code already (unchecked to `prepare` it): both cold, the first touched.
 */
function deploy(
  asm: Assembly,
  deployment: Deployment,
  blob: number,
  access: number,
  need: number,
  prepare: boolean,
): void {
  if (!prepare) asm.pushAddress(deployment.account).extcodesize(access).jumpi("deployed");
  const length = deployment.factoryData.length;
  asm.push(length).pushData(blob).push(0).codecopy(length);
  asm.push(0).push(0).push(length).push(0).push(0).pushAddress(deployment.factory);
  leftOver(asm, DEPLOYMENT, need);
  asm.clientCall(access);
  asm.op(Op.ISZERO).jumpi("call_failed");
  if (!prepare) asm.label("deployed");
}

/**
 * Call `token` with the transfer's calldata (the second blob), given exactly its gas, or the
 * gas left over if it has no bound.
 */
function transfer(asm: Assembly, token: Address, transfer: Transfer, need: number): void {
  const length = transfer.calldata.length;
  asm.push(length).pushData(1).push(0).codecopy(length);
  asm.push(0).push(0).push(length).push(0).push(0).pushAddress(token);
  if (transfer.gas === null) {
    leftOver(asm, TRANSFER, need);
    asm.clientCall(WARM_ACCOUNT_ACCESS);
  } else {
    asm.push(transfer.gas).cappedCall(WARM_ACCOUNT_ACCESS, transfer.gas);
  }
  asm.op(Op.ISZERO).jumpi("call_failed");
}

/**
 * Push a CALL's gas, GAS - R - `need`, its R measured from this GAS reading: GasExhausted() if
 * there isn't R + `need`. Checked against the same GAS reading.
 */
function leftOver(asm: Assembly, index: number, need: number): void {
  asm.reserve(index, need).gasRead(index).op(Op.DUP2, Op.DUP2, Op.LT).jumpi("gas_exhausted");
  asm.op(Op.SUB);
}

/**
 * Make the client's call with all but R of the gas left: GasExhausted() if there isn't
 * R + `need`, the call then given less than its declared gas.
 */
function clientCall(asm: Assembly, call: EncodedCall, access: number, need: number): void {
  const length = call.data.length;
  asm.push(length).pushData(0).push(0).codecopy(length);
  // CALL's arguments, last pushed first: gas, to, value, args, argsSize, ret, retSize
  asm.push(0).push(0).push(length).push(0).push(0).pushAddress(call.to);
  // its gas, GAS - R, checked against the same GAS reading. Stack: R, GAS; then R + need,
  // compared with GAS
  asm.reserve(CALL).gasRead(CALL).push(need).op(Op.DUP3, Op.ADD, Op.DUP2, Op.LT);
  asm.jumpi("gas_exhausted");
  asm.op(Op.SUB).clientCall(access);
  asm.op(Op.ISZERO).jumpi("call_failed");
}

/** Revert with the failed call's revert data, as it is. */
function callFailed(asm: Assembly): void {
  asm.label("call_failed");
  asm.op(Op.RETURNDATASIZE).push(0).push(0).op(Op.RETURNDATACOPY);
  asm.op(Op.RETURNDATASIZE).push(0).op(Op.REVERT);
}

function revertWith(asm: Assembly, label: string, selector: Uint8Array): void {
  asm.label(label);
  asm.push(word(selector)).push(0).op(Op.MSTORE);
  asm.push(4).push(0).op(Op.REVERT);
}
