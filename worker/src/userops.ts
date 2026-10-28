/**
 * UserOperations (ERC-4337, EntryPoint v0.7) a relay takes as calls that pay for themselves, an
 * op paying by its own calls (its fees zero) or by its fees, and their gas estimated as
 * ERC-7769's eth_estimateUserOperationGas answers it: run through EntryPointSimulations at the
 * EntryPoint's address by eth_call's state override, with the account's stub signature, a fee of
 * a wei and, paying by its calls, a deposit for the account in the EntryPoint. A port of the
 * Python reference (bruno/userops.py), where the two ways of paying, the estimate and the
 * simulation's code are described; the code comes from it (userops.json, make vectors).
 */

import { concatBytes } from "@noble/hashes/utils.js";
import { addressWord, encodeBytes, word } from "./abi.ts";
import type { Call } from "./chain.ts";
import { type Address, bytes, checksum, type Hex, hex, keccak, NATIVE_ASSET, utf8 } from "./evm.ts";
import { bigIntOf } from "./transactions.ts";
import useropsDocument from "./userops.json" with { type: "json" };

export const ENTRY_POINT: Address = checksum("0x0000000071727De22E5E9d8BAf0edAc6f37da032");
const SIMULATIONS_CODE = useropsDocument.entryPointSimulations;

const PACKED = "(address,uint256,bytes,bytes,bytes32,uint256,bytes32,bytes,bytes)";
const SIMULATE_HANDLE_OP = keccak(utf8(`simulateHandleOp(${PACKED},address,bytes)`)).subarray(0, 4);
/** The fee an op is simulated at: a wei, so what it pays is the gas it uses. */
const SIMULATED_FEE = 1n;
/** The deposit the account is given in the EntryPoint to fund its simulation, in its deposits
 * mapping at slot 0: the account itself left as it is. */
const SIMULATED_DEPOSIT = 10n ** 18n;
const DEPOSITS_SLOT = 0n;
/** EntryPoint v0.7's penalty on an op's unused call gas, in percent. */
const UNUSED_GAS_PENALTY = 10n;
/** Each limit is what's used and this much more, in percent. */
const MARGIN = 10n;

/** A field of the user operation is the client's mistake: `field`, its JSON name. */
export class InvalidUserOperationError extends Error {
  constructor(readonly field: string, message: string) {
    super(`${field}: ${message}`);
  }
}

/** An op for EntryPoint v0.7, as ERC-7769 has its fields. */
export interface UserOperation {
  readonly sender: Address;
  readonly nonce: bigint;
  readonly factory: Address | null;
  readonly factoryData: Uint8Array;
  readonly callData: Uint8Array;
  readonly callGasLimit: bigint;
  readonly verificationGasLimit: bigint;
  readonly preVerificationGas: bigint;
  readonly maxFeePerGas: bigint;
  readonly maxPriorityFeePerGas: bigint;
  readonly signature: Uint8Array;
}

/** An op's gas limits, as eth_estimateUserOperationGas answers. */
export interface GasEstimate {
  readonly preVerificationGas: bigint;
  readonly verificationGasLimit: bigint;
  readonly callGasLimit: bigint;
}

/** The factory and its data, packed: empty for a deployed account. */
export function initCode(op: UserOperation): Uint8Array {
  return op.factory === null ? new Uint8Array() : concatBytes(bytes(op.factory), op.factoryData);
}

/** Its verification and call gas limits, packed as one word. */
function accountGasLimits(op: UserOperation): Uint8Array {
  return concatBytes(
    word(op.verificationGasLimit).subarray(16),
    word(op.callGasLimit).subarray(16),
  );
}

/** Its priority fee and its most fee, packed as one word. */
function gasFees(op: UserOperation): Uint8Array {
  return concatBytes(
    word(op.maxPriorityFeePerGas).subarray(16),
    word(op.maxFeePerGas).subarray(16),
  );
}

/** The op as the EntryPoint takes it: ABI-encoded, a dynamic tuple. */
export function packed(op: UserOperation): Uint8Array {
  const tails = [initCode(op), op.callData, new Uint8Array(), op.signature].map(encodeBytes);
  const offsets: bigint[] = [];
  let at = 9 * 32;
  for (const tail of tails) {
    offsets.push(BigInt(at));
    at += tail.length;
  }
  const [initAt, callAt, paymasterAt, signatureAt] = offsets as [bigint, bigint, bigint, bigint];
  return concatBytes(
    addressWord(op.sender),
    word(op.nonce),
    word(initAt),
    word(callAt),
    accountGasLimits(op),
    word(op.preVerificationGas),
    gasFees(op),
    word(paymasterAt),
    word(signatureAt),
    ...tails,
  );
}

/** Whether the op pays the relay by its fees, in ETH through the EntryPoint (a max fee above
 * zero), rather than by its calls. */
export function paysByFees(op: UserOperation): boolean {
  return op.maxFeePerGas > 0n;
}

/** What the op pays a gas in a block of `baseFee`, as EntryPoint v0.7's getUserOpGasPrice has
 * it: its max fee if that's its priority fee too; else the base fee and its priority fee, at
 * most its max fee. */
export function gasPrice(op: UserOperation, baseFee: bigint): bigint {
  if (op.maxFeePerGas === op.maxPriorityFeePerGas) return op.maxFeePerGas;
  const price = op.maxPriorityFeePerGas + baseFee;
  return price < op.maxFeePerGas ? price : op.maxFeePerGas;
}

/** The op as it's simulated to estimate it: its limits each `limit`, its fees a wei. */
function simulated(op: UserOperation, limit: bigint): UserOperation {
  return {
    ...op,
    verificationGasLimit: limit,
    callGasLimit: limit,
    maxFeePerGas: SIMULATED_FEE,
    maxPriorityFeePerGas: SIMULATED_FEE,
  };
}

/** simulateHandleOp's calldata for `op`, its limits each `limit`, its fees a wei. */
export function simulationCalldata(op: UserOperation, limit: bigint): Uint8Array {
  return simulateHandleOp(simulated(op, limit), checksum(`0x${"00".repeat(20)}`));
}

/** simulateHandleOp's calldata for `op` as it is, `target` called after it, with no data. */
function simulateHandleOp(op: UserOperation, target: Address): Uint8Array {
  const tuple = packed(op);
  return concatBytes(
    SIMULATE_HANDLE_OP,
    word(3n * 32n),
    addressWord(target),
    word(BigInt(3 * 32 + tuple.length)),
    tuple,
    encodeBytes(new Uint8Array()),
  );
}

/** The eth_call that runs `op` through EntryPointSimulations, at the latest block: for an op
 * paying by its calls, a deposit for the account in its storage too. */
export function simulation(op: UserOperation, limit: bigint): Call {
  const override: Record<string, unknown> = { code: SIMULATIONS_CODE };
  if (!paysByFees(op)) {
    override.stateDiff = {
      [hex(keccak(concatBytes(addressWord(op.sender), word(DEPOSITS_SLOT))))]: hex(
        word(SIMULATED_DEPOSIT),
      ),
    };
  }
  return {
    method: "eth_call",
    params: [
      { to: ENTRY_POINT, data: hex(simulationCalldata(op, limit)) },
      "latest",
      { [ENTRY_POINT]: override },
    ],
  };
}

/** simulateHandleOp's answer, its op's limits each `limit`: the op's pre-execution gas, and its
 * execution. */
function used(answer: Uint8Array, limit: bigint): [bigint, bigint] {
  // the answer is one dynamic tuple: its offset, then preOpGas and paid, its first two words
  const at = Number(bigIntOf(answer.subarray(0, 32)));
  const preOpGas = bigIntOf(answer.subarray(at, at + 32));
  const spent = bigIntOf(answer.subarray(at + 32, at + 64)) / SIMULATED_FEE;
  // paid = preOpGas + execution + (limit - execution) * penalty / 100, solved for execution
  let execution = spent - preOpGas - limit * UNUSED_GAS_PENALTY / 100n;
  if (execution < 0n) execution = 0n;
  return [preOpGas, execution * 100n / (100n - UNUSED_GAS_PENALTY)];
}

/** simulateHandleOp's answer, its op's limits each `limit`, read as the op's estimate. */
export function estimated(answer: Uint8Array, limit: bigint): GasEstimate {
  const [preOpGas, execution] = used(answer, limit);
  return {
    preVerificationGas: 0n,
    verificationGasLimit: preOpGas * (100n + MARGIN) / 100n,
    callGasLimit: execution * (100n + MARGIN) / 100n,
  };
}

/** `estimate`, of an op paying by its fees, its preVerificationGas the rest of the
 * transaction's `gasLimit`, beyond what the op used. */
export function withOverhead(
  estimate: GasEstimate,
  answer: Uint8Array,
  limit: bigint,
  gasLimit: bigint,
): GasEstimate {
  const [preOpGas, execution] = used(answer, limit);
  const rest = gasLimit - preOpGas - execution;
  return { ...estimate, preVerificationGas: rest > 0n ? rest : 0n };
}

/** Where the payee's balances helper is put, by override: an address no contract has. */
export const BALANCES: Address = checksum(
  hex(keccak(utf8("bruno: the payee's balances")).subarray(12)),
);
const HANDLE_OPS = keccak(utf8(`handleOps(${PACKED}[],address)`)).subarray(0, 4);
const BALANCE_OF = keccak(utf8("balanceOf(address)")).subarray(0, 4);
/** The EntryPoint's error for an op it refuses, and its reason when the account refuses the
 * op's signature. */
const FAILED_OP = hex(keccak(utf8("FailedOp(uint256,string)")).subarray(0, 4));
const SIGNATURE_ERROR = "AA24 signature error";
/** Where the helper's balances begin, after its call to the EntryPoint: a JUMPDEST. */
const BALANCES_START = 58;
const USER_OPERATION_EVENT = hex(
  keccak(utf8("UserOperationEvent(bytes32,address,address,uint256,bool,uint256,uint256)")),
);
const REVERT_REASON = hex(keccak(utf8("UserOperationRevertReason(bytes32,address,uint256,bytes)")));
const BEFORE_EXECUTION = hex(keccak(utf8("BeforeExecution()")));

/** The op's hash, as EntryPoint v0.7's getUserOpHash computes it. */
export function userOperationHash(op: UserOperation, chainId: number): Uint8Array {
  const fields = concatBytes(
    addressWord(op.sender),
    word(op.nonce),
    keccak(initCode(op)),
    keccak(op.callData),
    accountGasLimits(op),
    word(op.preVerificationGas),
    gasFees(op),
    keccak(new Uint8Array()),
  );
  return keccak(concatBytes(keccak(fields), addressWord(ENTRY_POINT), word(BigInt(chainId))));
}

/** The EntryPoint's handleOps calldata for `op` alone, `beneficiary` paid its fees (none). */
export function handleOps(op: UserOperation, beneficiary: Address): Uint8Array {
  return concatBytes(
    HANDLE_OPS,
    word(2n * 32n),
    addressWord(beneficiary),
    word(1n),
    word(32n),
    packed(op),
  );
}

/** A helper's runtime code returning `payee`'s balance of each of `tokens`, a word each, in
 * order (ETH as ERC-7528's address), then the block's base fee; called with data, it first calls
 * the EntryPoint with it, reverting as it does: as bruno/userops.py's balances_code. */
export function balancesCode(payee: Address, tokens: readonly Address[]): Uint8Array {
  const out = (tokens.length + 1) * 32;
  const u16 = (n: number): Uint8Array => Uint8Array.of(n >> 8, n & 0xff);
  const start = u16(BALANCES_START);
  const parts: Uint8Array[] = [
    Uint8Array.of(0x36, 0x15, 0x61),
    start,
    Uint8Array.of(0x57, 0x36, 0x60, 0x00, 0x60, 0x00, 0x37),
    Uint8Array.of(0x60, 0x00, 0x60, 0x00, 0x36, 0x60, 0x00, 0x60, 0x00, 0x73),
    bytes(ENTRY_POINT),
    Uint8Array.of(0x5a, 0xf1, 0x61),
    start,
    Uint8Array.of(0x57, 0x3d, 0x60, 0x00, 0x60, 0x00, 0x3e, 0x3d, 0x60, 0x00, 0xfd, 0x5b),
  ];
  for (const [i, token] of tokens.entries()) {
    if (token === NATIVE_ASSET) {
      parts.push(Uint8Array.of(0x73), bytes(payee), Uint8Array.of(0x31));
      parts.push(Uint8Array.of(0x61), u16(i * 32), Uint8Array.of(0x52));
      continue;
    }
    parts.push(Uint8Array.of(0x63), BALANCE_OF, Uint8Array.of(0x60, 0xe0, 0x1b));
    parts.push(Uint8Array.of(0x61), u16(out), Uint8Array.of(0x52));
    parts.push(Uint8Array.of(0x73), bytes(payee), Uint8Array.of(0x61), u16(out + 4));
    parts.push(Uint8Array.of(0x52, 0x60, 0x20, 0x61), u16(i * 32));
    parts.push(Uint8Array.of(0x60, 0x24, 0x61), u16(out));
    parts.push(Uint8Array.of(0x73), bytes(token), Uint8Array.of(0x5a, 0xfa, 0x50));
  }
  parts.push(Uint8Array.of(0x48, 0x61), u16(tokens.length * 32), Uint8Array.of(0x52));
  parts.push(Uint8Array.of(0x61), u16(out), Uint8Array.of(0x60, 0x00, 0xf3));
  return concatBytes(...parts);
}

/** What a send reads, in one batch: `payee`'s balances in `tokens`; the op run by the
 * EntryPoint's handleOps as it will be sent, the payee its beneficiary, the balances read after
 * it; and handleOps' gas. */
export function sending(op: UserOperation, payee: Address, tokens: readonly Address[]): Call[] {
  const helper = { [BALANCES]: { code: hex(balancesCode(payee, tokens)) } };
  const calls = hex(handleOps(op, payee));
  return [
    { method: "eth_call", params: [{ to: BALANCES, data: "0x" }, "latest", helper] },
    { method: "eth_call", params: [{ to: BALANCES, data: calls }, "latest", helper] },
    { method: "eth_estimateGas", params: [{ from: payee, to: ENTRY_POINT, data: calls }] },
  ];
}

/** The client's mistake an EntryPoint's revert names, if it's one: the account refusing the op's
 * signature. */
export function refusal(revert: Uint8Array): InvalidUserOperationError | null {
  if (revert.length < 4 + 96 || hex(revert.subarray(0, 4)) !== FAILED_OP) return null;
  const body = revert.subarray(4);
  const at = Number(bigIntOf(body.subarray(32, 64)));
  const length = Number(bigIntOf(body.subarray(at, at + 32)));
  const reason = new TextDecoder().decode(body.subarray(at + 32, at + 32 + length));
  if (reason !== SIGNATURE_ERROR) return null;
  return new InvalidUserOperationError("signature", "the account doesn't take it");
}

/** The `i`th word of `data`. */
function wordAt(data: Uint8Array, i: number): bigint {
  return bigIntOf(data.subarray(i * 32, i * 32 + 32));
}

/** What the op paid the payee in each of `tokens`, from its balances `before` the op and `after`
 * it. */
export function paid(
  before: Uint8Array,
  after: Uint8Array,
  tokens: readonly Address[],
): Map<Address, bigint> {
  return new Map(tokens.map((token, i) => {
    const gained = wordAt(after, i) - wordAt(before, i);
    return [token, gained > 0n ? gained : 0n];
  }));
}

/** What `op`, paying by its fees, pays the payee in a block of `baseFee`: the gas it was charged
 * when run (`eth` paid, at its price under that run's base fee, the word read `after` it past its
 * `tokens` balances), at its price then. */
export function feePayment(
  op: UserOperation,
  eth: bigint,
  after: Uint8Array,
  baseFee: bigint,
  tokens: number,
): bigint {
  return eth / gasPrice(op, wordAt(after, tokens)) * gasPrice(op, baseFee);
}

/** An op's receipt, as ERC-7769 has it (bruno/userops.py's UserOperationReceipt). */
export interface UserOperationReceipt {
  readonly userOpHash: Hex;
  readonly sender: Address;
  readonly nonce: bigint;
  readonly paymaster: Address;
  readonly actualGasCost: bigint;
  readonly actualGasUsed: bigint;
  readonly success: boolean;
  readonly reason: Uint8Array;
  readonly logs: readonly unknown[];
  readonly receipt: Readonly<Record<string, unknown>>;
}

interface Log {
  readonly topics: readonly Hex[];
  readonly data: Hex;
}

/** ERC-7769's receipt of the op `opHash`, from its transaction's `receipt`: null if the
 * transaction holds no UserOperationEvent of it. */
export function receiptOf(
  opHash: Uint8Array,
  receipt: Readonly<Record<string, unknown>>,
): UserOperationReceipt | null {
  const logs = receipt.logs as readonly Log[];
  const topic = hex(opHash);
  let event: { index: number; log: Log } | null = null;
  let reason: Log | null = null;
  let start = 0;
  for (const [i, log] of logs.entries()) {
    const first = log.topics[0];
    if (first === BEFORE_EXECUTION) start = i + 1;
    else if (first === USER_OPERATION_EVENT && log.topics[1] === topic) event = { index: i, log };
    else if (first === REVERT_REASON && log.topics[1] === topic) reason = log;
  }
  if (event === null) return null;
  const { index, log } = event;
  const data = bytes(log.data);
  const word = (i: number): bigint => bigIntOf(data.subarray(i * 32, i * 32 + 32));
  let revert = new Uint8Array();
  if (reason !== null) {
    const r = bytes(reason.data);
    const at = Number(bigIntOf(r.subarray(32, 64)));
    const length = Number(bigIntOf(r.subarray(at, at + 32)));
    revert = r.slice(at + 32, at + 32 + length);
  }
  return {
    userOpHash: topic,
    sender: checksum(`0x${(log.topics[2] as string).slice(-40)}`),
    nonce: word(0),
    paymaster: checksum(`0x${(log.topics[3] as string).slice(-40)}`),
    actualGasCost: word(2),
    actualGasUsed: word(3),
    success: word(1) === 1n,
    reason: revert,
    logs: logs.slice(start, index),
    receipt,
  };
}
