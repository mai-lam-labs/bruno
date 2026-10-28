/**
 * UserOperations (ERC-4337, EntryPoint v0.7) a relay takes as calls that pay for themselves, and
 * their gas estimated as ERC-7769's eth_estimateUserOperationGas answers it: run through
 * EntryPointSimulations at the EntryPoint's address by eth_call's state override, with the
 * account's stub signature, a fee of a wei and a deposit for the account in the EntryPoint. A port of the Python
 * reference (bruno/userops.py), where the estimate and the simulation's code are described;
 * the code comes from it (userops.json, make vectors).
 */

import { concatBytes } from "@noble/hashes/utils.js";
import { addressWord, encodeBytes, word } from "./abi.ts";
import type { Call } from "./chain.ts";
import { type Address, bytes, checksum, hex, keccak, utf8 } from "./evm.ts";
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

/** The op as the EntryPoint takes it (ABI-encoded, a dynamic tuple), given these limits and
 * both fees `fee`. */
export function packed(
  op: UserOperation,
  verificationGas: bigint,
  callGas: bigint,
  fee: bigint,
): Uint8Array {
  const tails = [initCode(op), op.callData, new Uint8Array(), op.signature].map(encodeBytes);
  const head = 9 * 32;
  const offsets: bigint[] = [];
  let at = head;
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
    concatBytes(word(verificationGas).subarray(16), word(callGas).subarray(16)),
    word(op.preVerificationGas),
    concatBytes(word(fee).subarray(16), word(fee).subarray(16)),
    word(paymasterAt),
    word(signatureAt),
    ...tails,
  );
}

/** simulateHandleOp's calldata for `op`, its limits each `limit`, its fees a wei. */
export function simulationCalldata(op: UserOperation, limit: bigint): Uint8Array {
  const tuple = packed(op, limit, limit, SIMULATED_FEE);
  return concatBytes(
    SIMULATE_HANDLE_OP,
    word(3n * 32n),
    addressWord(checksum(`0x${"00".repeat(20)}`)),
    word(BigInt(3 * 32 + tuple.length)),
    tuple,
    encodeBytes(new Uint8Array()),
  );
}

/** The eth_call that runs `op` through EntryPointSimulations, at the latest block. */
export function simulation(op: UserOperation, limit: bigint): Call {
  return {
    method: "eth_call",
    params: [
      { to: ENTRY_POINT, data: hex(simulationCalldata(op, limit)) },
      "latest",
      {
        [ENTRY_POINT]: {
          code: SIMULATIONS_CODE,
          stateDiff: {
            [hex(keccak(concatBytes(addressWord(op.sender), word(DEPOSITS_SLOT))))]: hex(
              word(SIMULATED_DEPOSIT),
            ),
          },
        },
      },
    ],
  };
}

/** simulateHandleOp's answer, its op's limits each `limit`, read as the op's estimate. */
export function estimated(answer: Uint8Array, limit: bigint): GasEstimate {
  // the answer is one dynamic tuple: its offset, then preOpGas and paid, its first two words
  const at = Number(bigIntOf(answer.subarray(0, 32)));
  const preOpGas = bigIntOf(answer.subarray(at, at + 32));
  const used = bigIntOf(answer.subarray(at + 32, at + 64)) / SIMULATED_FEE;
  // paid = preOpGas + execution + (limit - execution) * penalty / 100, solved for execution
  let execution = used - preOpGas - limit * UNUSED_GAS_PENALTY / 100n;
  if (execution < 0n) execution = 0n;
  execution = execution * 100n / (100n - UNUSED_GAS_PENALTY);
  return {
    preVerificationGas: 0n,
    verificationGasLimit: preOpGas * (100n + MARGIN) / 100n,
    callGasLimit: execution * (100n + MARGIN) / 100n,
  };
}
