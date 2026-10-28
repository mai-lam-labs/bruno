/**
 * The runner's CREATE2 deployer (docs/development/design/runner.md): its code, its address, and the gas a
 * transaction through it needs. A port of the Python reference (bruno/deployer.py).
 */

import { concatBytes } from "@noble/hashes/utils.js";
import { type Address, bytes, checksum, hex, keccak, memoryGas } from "./evm.ts";
import { authorizationsGas, type GasSchedule, intrinsic } from "./forks.ts";

/** Arachnid's deterministic deployment proxy: calldata `salt ‖ initcode`, at this address. */
export const ARACHNID: Address = checksum("0x4e59b44847b379578588920cA78FbF26c0B4956C");

export const RUNTIME = bytes(
  "0x365f5f3733365243602036015260403620365f5ff5806020573d5f5f3e3d5ffd5b5f5260205ff3",
);
/** PUSH1 39 DUP1 PUSH1 9 PUSH0 CODECOPY PUSH0 RETURN: the runtime, which follows. */
export const INITCODE = concatBytes(bytes("0x60278060095f395ff3"), RUNTIME);
export const ARACHNID_SALT = new Uint8Array(32);
export const DEPLOYER: Address = checksum(
  hex(
    keccak(concatBytes(Uint8Array.of(0xff), bytes(ARACHNID), ARACHNID_SALT, keccak(INITCODE)))
      .slice(12),
  ),
);

// the runtime's gas, from its opcodes, before CREATE2 and after (copying, memory and hashing
// aside)
const BEFORE_CREATE2_GAS = 2 + 2 + 2 + 3 + 2 + 2 + 3 + 2 + 3 + 2 + 3 + 3 + 3 + 2 + 30 + 2 + 2 +
  2;
const AFTER_CREATE2_GAS = 3 + 3 + 10 + 1 + 2 + 3 + 3 + 2;
/** The salt's preimage, caller and block number: two words, hashed at 6 a word. */
const SALT_WORDS = 2;
const KECCAK_WORD_GAS = 6;
/** CALLDATACOPY's 3 a word; CREATE2's 6 a word of initcode (keccak) and 2 (EIP-3860). */
const COPY_WORD_GAS = 3;
const CREATE2_WORD_GAS = 6 + 2;

/** The calldata that deploys the deployer through Arachnid's. */
export function arachnidCalldata(): Uint8Array {
  return concatBytes(ARACHNID_SALT, INITCODE);
}

/** The deployer's own gas for `initcodeLength` bytes of initcode, the runner's aside. */
export function overheadGas(initcodeLength: number, schedule: GasSchedule): number {
  const words = Math.ceil(initcodeLength / 32);
  return BEFORE_CREATE2_GAS +
    COPY_WORD_GAS * words +
    KECCAK_WORD_GAS * SALT_WORDS +
    memoryGas(Math.ceil((initcodeLength + 32 * SALT_WORDS) / 32)) +
    schedule.create +
    CREATE2_WORD_GAS * words +
    AFTER_CREATE2_GAS;
}

/**
 * The gas limit a transaction to the deployer needs for a runner that may use `runnerGas`:
 * intrinsic gas (the floor if higher), the authorizations' execution, the deployer's own gas,
 * and enough at CREATE2 that EIP-150's 63/64 rule still gives the runner `runnerGas`.
 */
export function transactionGas(
  initcode: Uint8Array,
  runnerGas: number,
  schedule: GasSchedule,
  authorizations = 0,
  emptyAuthorities = 0,
): number {
  const overhead = overheadGas(initcode.length, schedule) - AFTER_CREATE2_GAS;
  const atCreate2 = Math.ceil(((runnerGas + AFTER_CREATE2_GAS) * 64) / 63);
  const execution = overhead + atCreate2 +
    authorizationsGas(schedule, emptyAuthorities, authorizations);
  const { standard, floor } = intrinsic(schedule, initcode, { authorizations });
  return Math.max(standard + execution, floor);
}
