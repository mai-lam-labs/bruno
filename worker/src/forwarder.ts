/**
 * The forwarder: a contract with the payee in its code, which a payer pays where no runner
 * measures the payment, and which sends all it holds on to the payee, reverting unless the payee
 * gained at least the amount asked. As bruno/forwarder.py's: the same code, address and gas.
 */

import { concatBytes } from "@noble/hashes/utils.js";
import { addressWord, BALANCE_OF, selector, word } from "./abi.ts";
import { Assembly, Op } from "./assembler.ts";
import { arachnidAddress } from "./deployer.ts";
import { type Address, type EncodedCall, NATIVE_ASSET, WARM_ACCOUNT_ACCESS } from "./evm.ts";
import type { GasSchedule } from "./forks.ts";
import { BALANCE_UNREADABLE, callFailed, leading, revertWith, UNDERPAID } from "./runner.ts";

export const TRANSFER = selector("transfer(address,uint256)");
/** A word, and the calldata of each of the forwarder's calls to a token. */
const WORD = 32;
const BALANCE_OF_BYTES = 4 + WORD;
const TRANSFER_BYTES = 4 + 2 * WORD;
/**
 * The initcode before the runtime: PUSH2 its length, DUP1, PUSH1 where it starts, PUSH0,
 * CODECOPY, PUSH0, RETURN.
 */
const INITCODE_PREFIX_BYTES = 10;

/** A payee's forwarder: its code, the initcode that deploys it, and where. */
export interface Forwarder {
  readonly payee: Address;
  readonly runtime: Uint8Array;
  readonly initcode: Uint8Array;
  readonly address: Address;
}

/** `payee`'s forwarder. */
export function forwarderOf(payee: Address): Forwarder {
  const runtime = program(payee, 0, 0, false).code;
  const initcode = concatBytes(
    Uint8Array.of(Op.PUSH2, runtime.length >> 8, runtime.length & 0xff),
    Uint8Array.of(Op.DUP1, Op.PUSH1, INITCODE_PREFIX_BYTES, Op.PUSH0, Op.CODECOPY),
    Uint8Array.of(Op.PUSH0, Op.RETURN),
    runtime,
  );
  return { payee, runtime, initcode, address: arachnidAddress(initcode) };
}

/**
 * The call that forwards all `forwarder` holds of `asset` (a token, or ERC-7528's address for
 * ETH) to the payee, reverting unless the payee gains `least` (for a token, less the one unit
 * kept).
 */
export function forwarding(forwarder: Forwarder, asset: Address, least: bigint): EncodedCall {
  const data = asset === NATIVE_ASSET ? word(least) : concatBytes(addressWord(asset), word(least));
  return { to: forwarder.address, data, value: 0n };
}

/**
 * The most gas forwarding a token takes of the forwarder's own: all but what the token's
 * balanceOf and transfer use.
 */
export function tokenGas(forwarder: Forwarder, schedule: GasSchedule): number {
  return program(forwarder.payee, schedule.coldAccountAccess, 0, false).gas;
}

/**
 * The most gas forwarding ETH takes, to a payee whose code uses none: a `newPayee` is an account
 * the ETH creates.
 */
export function ethGas(forwarder: Forwarder, schedule: GasSchedule, newPayee: boolean): number {
  const sending = schedule.callValue + (newPayee ? schedule.newAccount : 0);
  return program(forwarder.payee, schedule.coldAccountAccess, sending, true).gas;
}

/**
 * The forwarder's code for `payee`, and the gas of the path laid straight after its calldata's
 * length is read: a token's, as it's deployed, or ETH's (`ethFirst`), laid out so only for the
 * assembler to sum it. `cold` and `sending` only count: the code is the same whatever they are.
 */
function program(
  payee: Address,
  cold: number,
  sending: number,
  ethFirst: boolean,
): { code: Uint8Array; gas: number } {
  const asm = new Assembly();
  asm.op(Op.CALLDATASIZE, Op.ISZERO).jumpi("paid");
  asm.push(2 * WORD).op(Op.CALLDATASIZE, Op.LT).jumpi("eth");
  if (ethFirst) {
    asm.label("eth");
    forwardEth(asm, payee, cold, sending);
    asm.end();
    forwardToken(asm, payee, cold);
  } else {
    forwardToken(asm, payee, cold);
    asm.end();
    asm.label("eth");
    forwardEth(asm, payee, cold, sending);
  }
  asm.label("paid");
  asm.op(Op.STOP);
  callFailed(asm);
  revertWith(asm, "underpaid", UNDERPAID);
  revertWith(asm, "balance_unreadable", BALANCE_UNREADABLE);
  return asm.assemble([]);
}

/** Send the payee all the ETH held, and require its balance to rise by calldata's one word. */
function forwardEth(asm: Assembly, payee: Address, cold: number, sending: number): void {
  asm.pushAddress(payee).balance(cold); // stack: before
  // CALL's arguments, last pushed first: gas, to, value, no args, no return
  asm.push(0).push(0).push(0).push(0).op(Op.SELFBALANCE).pushAddress(payee).op(Op.GAS);
  asm.valueCall(WARM_ACCOUNT_ACCESS + sending);
  asm.op(Op.ISZERO).jumpi("call_failed");
  asm.pushAddress(payee).balance(WARM_ACCOUNT_ACCESS); // stack: before after
  requireGain(asm, 0, 0);
  asm.op(Op.STOP);
}

/**
 * Send the payee all but one unit held of the token (calldata's first word), and require its
 * balance to rise by calldata's second word, less the unit.
 */
function forwardToken(asm: Assembly, payee: Address, cold: number): void {
  asm.op(Op.ADDRESS);
  balanceOf(asm, cold); // stack: held
  asm.op(Op.DUP1, Op.ISZERO).jumpi("underpaid");
  asm.pushAddress(payee);
  balanceOf(asm, WARM_ACCOUNT_ACCESS); // stack: held before
  // memory[0:68] = transfer(payee, held - 1); whatever it answers is left unread
  asm.push(leading(TRANSFER)).mstoreAt(0);
  asm.pushAddress(payee).mstoreAt(4);
  asm.push(1).op(Op.DUP3, Op.SUB).mstoreAt(BALANCE_OF_BYTES);
  asm.push(0).push(0).push(TRANSFER_BYTES).push(0).push(0);
  asm.push(0).op(Op.CALLDATALOAD, Op.GAS).clientCall(WARM_ACCOUNT_ACCESS);
  asm.op(Op.POP);
  asm.pushAddress(payee);
  balanceOf(asm, WARM_ACCOUNT_ACCESS); // stack: held before after
  requireGain(asm, WORD, 1);
  asm.op(Op.STOP);
}

/**
 * Replace the account on the stack with its balance of the token (calldata's first word), the
 * token costing `access`: memory[0:36] = balanceOf(account), its answer to memory[0:32].
 */
function balanceOf(asm: Assembly, access: number): void {
  asm.push(leading(BALANCE_OF)).mstoreAt(0);
  asm.mstoreAt(4);
  asm.push(WORD).push(0).push(BALANCE_OF_BYTES).push(0);
  asm.push(0).op(Op.CALLDATALOAD, Op.GAS).clientStaticcall(access);
  asm.op(Op.ISZERO).jumpi("balance_unreadable");
  // a token with no code answers nothing: success, but no 32 bytes
  asm.op(Op.RETURNDATASIZE).push(WORD).op(Op.EQ, Op.ISZERO).jumpi("balance_unreadable");
  asm.mloadAt(0);
}

/**
 * With the payee's balance before and after on the stack: Underpaid() unless it rose by the word
 * of calldata at `leastAt`, allowing for `kept` units left behind.
 */
function requireGain(asm: Assembly, leastAt: number, kept: number): void {
  // after < before: the payee lost funds
  asm.op(Op.DUP2, Op.DUP2, Op.LT).jumpi("underpaid");
  asm.op(Op.SUB); // stack: after - before
  if (kept) asm.push(kept).op(Op.ADD);
  asm.push(leastAt).op(Op.CALLDATALOAD, Op.DUP2, Op.LT).jumpi("underpaid");
}
