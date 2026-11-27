/**
 * The runner: initcode generated per submission and deployed through the deployer. It makes the
 * client's calls, in order, and asserts the payment (docs/development/design/runner.md). As bruno/runner.py's:
 * the same bytes, the same bounds.
 */

import { type Assembled, Assembly, AssemblyError, Op } from "./assembler.ts";
import type { Deployment } from "./erc6492.ts";
import { BALANCE_OF, selector } from "./abi.ts";
import { type Address, type EncodedCall, hex, WARM_ACCOUNT_ACCESS } from "./evm.ts";
import type { GasSchedule } from "./forks.ts";

/** EIP-3860's limit on initcode: 2 x the 24,576-byte code limit. */
export const MAX_INITCODE_BYTES = 49_152;

/** The standard's error namespace: its ERC number, once assigned. */
export const ERC = "ERCNNNN";

export const UNDERPAID = selector(`${ERC}_Underpaid()`);
export const BALANCE_UNREADABLE = selector(`${ERC}_BalanceUnreadable()`);
export const GAS_EXHAUSTED = selector(`${ERC}_GasExhausted()`);
const MAX_UINT256 = (1n << 256n) - 1n;

export class RunnerError extends Error {}

/**
 * A payment token, and the most gas its balanceOf may take: the runner gives it exactly that. An
 * EIP-3009 token's `transferGas` is a zero-value transferWithAuthorization's, measured;
 * `forwardGas` a zero-value transfer's by the payee's forwarder, measured, absent if it didn't
 * measure.
 */
export interface Erc20 {
  readonly address: Address;
  readonly balanceGas: number;
  readonly transferGas?: number;
  readonly forwardGas?: number;
}

/**
 * A call to the payment's token that the runner makes with the client's: its calldata, given
 * exactly `gas` (a key's EIP-3009 transfer, its gas bounded), made after the client's call; or
 * null: the gas left over (a contract's, whose signature check is its own code), made before it.
 */
export interface Transfer {
  readonly calldata: Uint8Array;
  readonly gas: number | null;
}

/**
 * What the payee must receive during the client's call: `amount` of `token`, or of ETH; and a
 * transfer to make with it, if the payment comes with one.
 */
export interface Payment {
  readonly token: Erc20 | null;
  readonly payee: Address;
  readonly amount: bigint;
  readonly transfer?: Transfer;
}

/**
 * One of a submission's calls as it's planned: the `call`, and the `gas` it's given. One the
 * relay `bounded` is given exactly that, its own bound of the call with what EIP-150 keeps back
 * (plan's plannedCall decides, nothing else does); any other, at least what its submission
 * states.
 */
export interface PlannedCall {
  readonly call: EncodedCall;
  readonly gas: number;
  readonly bounded: boolean;
}

/**
 * A runner's initcode, and the most gas the runner itself takes, its bounded calls' among it:
 * everything but what its other calls are given, which is the rest of its frame's gas.
 */
export interface Runner {
  readonly initcode: Uint8Array;
  readonly overheadGas: number;
}

/**
 * The gas the runner's frame needs for each of `calls` to be given its gas: its own, and what
 * the calls not bounded need (callsNeed). A runner with a step given the gas left over needs
 * that step's too, which only an estimate knows.
 */
export function frameGas(runner: Runner, calls: readonly PlannedCall[]): number {
  return runner.overheadGas + callsNeed(calls);
}

/**
 * What the call needs left above its R to be given `clientGas`: the gas, and what EIP-150's
 * 63/64 rule keeps back from a call given all that's left.
 */
export function callNeed(clientGas: number): number {
  return clientGas + Math.ceil(clientGas / 63);
}

/**
 * What `calls` need left above the runner's own gas, together: each call's not bounded
 * (callNeed). A bounded call's gas is the runner's own, a cap among its overhead.
 */
export function callsNeed(calls: readonly PlannedCall[]): number {
  let need = 0;
  for (const planned of calls) if (!planned.bounded) need += callNeed(planned.gas);
  return need;
}

/** The most gas a call can be given with `need` left above its R: callNeed's inverse. */
export function clientGasWithin(need: number): number {
  return Math.floor(Math.max(0, need) * 63 / 64);
}

/** What a runner's made with besides its call and payment: each optional. */
export interface RunnerOptions {
  /** The targets that are, or by the transaction's authorization list will be, delegated. */
  readonly delegated?: ReadonlySet<Address>;
  /** An account to deploy first, if it has no code (always, to `prepare` a deployed one). */
  readonly deployment?: Deployment | null;
  readonly prepare?: boolean;
}

/**
 * The runner for `calls`, made in order, each given its gas, and `payment`, its gas bounded by
 * `schedule`; the deployment made first, if its account has no code (always, to prepare a
 * deployed one). `delegated` are the targets that are, or by the transaction's authorization
 * list will be, EIP-7702 delegated.
 */
export function inCallRunner(
  calls: readonly PlannedCall[],
  payment: Payment,
  schedule: GasSchedule,
  options: RunnerOptions = {},
): Runner {
  return runner(calls, payment, schedule, options, true);
}

/**
 * inCallRunner's runner without its payment's transfer and check, only to estimate what one of
 * `calls` needs (plan's callGas), the one stating no gas: the deployment, the payee's balance
 * read, then the calls, that one given all the gas left but what the later ones need, as the
 * runner sent makes them, so each finds the state it will. The transfer, unsigned yet, is
 * priced from its bound (eip3009's transferGas). Never sent: it doesn't revert unpaid.
 */
export function estimatingRunner(
  calls: readonly PlannedCall[],
  payment: Payment,
  schedule: GasSchedule,
  options: RunnerOptions = {},
): Runner {
  return runner(calls, payment, schedule, { ...options, prepare: false }, false);
}

/**
 * A runner's steps: its payment's transfer and check only if `checked`. Each call's data is the
 * blob of its place and its GAS reading the reading of its place; the transfer's and the
 * deployment's follow the calls'.
 */
function runner(
  calls: readonly PlannedCall[],
  payment: Payment,
  schedule: GasSchedule,
  options: RunnerOptions,
  checked: boolean,
): Runner {
  if (calls.length === 0) throw new RunnerError("a runner makes at least one call");
  if (calls.some((planned) => planned.call.value !== 0n)) {
    throw new RunnerError("the client's call carries no value: the runner holds no ETH");
  }
  if (checked && !(payment.amount > 0n && payment.amount <= MAX_UINT256)) {
    throw new RunnerError(`payment amount ${payment.amount} isn't a positive uint256`);
  }
  const delegated = options.delegated ?? new Set();
  const need = callsNeed(calls);
  const asm = new Assembly();
  const blobs = calls.map((planned) => planned.call.data);
  const paid = checked ? payment.transfer : undefined;
  const transferBlob = blobs.length;
  const transferReading = calls.length;
  const deploymentReading = calls.length + 1;
  if (paid !== undefined) blobs.push(paid.calldata);
  const deployment = options.deployment ?? null;
  if (deployment !== null) {
    blobs.push(deployment.factoryData);
    deploy(
      asm,
      deployment,
      blobs.length - 1,
      deploymentReading,
      schedule.coldAccountAccess,
      need,
      options.prepare ?? false,
    );
  }
  balance(asm, payment, schedule.coldAccountAccess); // stack: before
  if (paid !== undefined && payment.token === null) {
    throw new RunnerError("a transfer pays in a token, not ETH");
  }
  // a transfer with no bound, before the calls: after them, nothing would keep its gas back
  if (paid !== undefined && payment.token !== null && paid.gas === null) {
    transfer(asm, payment.token.address, paid, transferBlob, transferReading, need);
  }
  calls.forEach((planned, index) => {
    const access = callAccess(planned.call.to, delegated, schedule);
    clientCall(asm, index, planned, access, callsNeed(calls.slice(index + 1)));
  });
  if (paid !== undefined && payment.token !== null && paid.gas !== null) {
    transfer(asm, payment.token.address, paid, transferBlob, transferReading, need);
  }
  if (checked) {
    balance(asm, payment, WARM_ACCOUNT_ACCESS); // stack: before after
    // after < before: the payee lost funds in the calls
    asm.op(Op.DUP2, Op.DUP2, Op.LT).jumpi("underpaid");
    asm.op(Op.SUB); // stack: after - before
    asm.push(payment.amount).op(Op.DUP2, Op.LT).jumpi("underpaid");
  }
  asm.op(Op.ORIGIN, Op.SELFDESTRUCT);
  asm.end();
  callFailed(asm);
  if (checked) revertWith(asm, "underpaid", UNDERPAID);
  revertWith(asm, "balance_unreadable", BALANCE_UNREADABLE);
  revertWith(asm, "gas_exhausted", GAS_EXHAUSTED);
  let assembled: Assembled;
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

/**
 * The most a CALL to `target` is charged before it forwards gas: the target's access, cold at
 * worst, and for a delegated EOA its delegate's too (EIP-7702).
 */
function callAccess(
  target: Address,
  delegated: ReadonlySet<Address>,
  schedule: GasSchedule,
): number {
  return schedule.coldAccountAccess * (delegated.has(target) ? 2 : 1);
}

/** `selector` as a word's leading bytes. */
export function leading(selector: Uint8Array): bigint {
  return BigInt(hex(selector)) << 224n;
}

/** Push the payee's balance, the payee and token costing `access`. */
function balance(asm: Assembly, payment: Payment, access: number): void {
  if (payment.token === null) {
    asm.pushAddress(payment.payee).balance(access);
    return;
  }
  // memory[0:36] = balanceOf(payee); its answer to memory[0:32]
  asm.push(leading(BALANCE_OF)).mstoreAt(0);
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
  reading: number,
  access: number,
  need: number,
  prepare: boolean,
): void {
  if (!prepare) asm.pushAddress(deployment.account).extcodesize(access).jumpi("deployed");
  const length = deployment.factoryData.length;
  asm.push(length).pushData(blob).push(0).codecopy(length);
  asm.push(0).push(0).push(length).push(0).push(0).pushAddress(deployment.factory);
  leftOver(asm, reading, need);
  asm.clientCall(access);
  asm.op(Op.ISZERO).jumpi("call_failed");
  if (!prepare) asm.label("deployed");
}

/**
 * Call `token` with the transfer's calldata (blob `blob`), given exactly its gas, or the gas
 * left over if it has no bound (GAS reading `reading`).
 */
function transfer(
  asm: Assembly,
  token: Address,
  transfer: Transfer,
  blob: number,
  reading: number,
  need: number,
): void {
  const length = transfer.calldata.length;
  asm.push(length).pushData(blob).push(0).codecopy(length);
  asm.push(0).push(0).push(length).push(0).push(0).pushAddress(token);
  if (transfer.gas === null) {
    leftOver(asm, reading, need);
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
 * Make the client's call, the `index`th (its data that blob, its GAS reading that reading).
 * Bounded, it's given exactly its gas. Otherwise it's given all the gas left but R and `later`,
 * what the calls after it need: GasExhausted() if there isn't R + `later` and what its own gas
 * needs, the call then given less than its declared gas.
 */
function clientCall(
  asm: Assembly,
  index: number,
  planned: PlannedCall,
  access: number,
  later: number,
): void {
  const call = planned.call;
  const length = call.data.length;
  asm.push(length).pushData(index).push(0).codecopy(length);
  // CALL's arguments, last pushed first: gas, to, value, args, argsSize, ret, retSize
  asm.push(0).push(0).push(length).push(0).push(0).pushAddress(call.to);
  if (planned.bounded) {
    asm.push(planned.gas).cappedCall(access, planned.gas);
  } else {
    // its gas, GAS - R - later, checked against the same GAS reading. Stack: R + later, GAS;
    // then R + later + need, compared with GAS
    asm.reserve(index, later).gasRead(index).push(callNeed(planned.gas));
    asm.op(Op.DUP3, Op.ADD, Op.DUP2, Op.LT).jumpi("gas_exhausted");
    asm.op(Op.SUB).clientCall(access);
  }
  asm.op(Op.ISZERO).jumpi("call_failed");
}

/** Revert with the failed call's revert data, as it is. */
export function callFailed(asm: Assembly): void {
  asm.label("call_failed");
  asm.op(Op.RETURNDATASIZE).push(0).push(0).op(Op.RETURNDATACOPY);
  asm.op(Op.RETURNDATASIZE).push(0).op(Op.REVERT);
}

export function revertWith(asm: Assembly, label: string, selector: Uint8Array): void {
  asm.label(label);
  asm.push(leading(selector)).push(0).op(Op.MSTORE);
  asm.push(4).push(0).op(Op.REVERT);
}
