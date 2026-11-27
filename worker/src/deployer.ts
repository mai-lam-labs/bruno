/**
 * The runner's deployer (docs/development/design/runner.md): Arachnid's deterministic
 * deployment proxy, at one address on every chain, with nothing of Bruno's to deploy. Kept
 * behind this module (its address, its calldata, its gas, what it answers) so that another
 * deployer can take its place. As bruno/deployer.py's.
 *
 * Calldata is a salt and the runner's initcode. It CREATE2s the initcode with the salt, passing
 * the transaction's value on, and answers the runner's address in 20 bytes; if the creation
 * fails it reverts with nothing, so a runner's own revert data doesn't come back.
 *
 * The salt is Bruno's to choose, and it's a keyed hash (runnerSalt): a submission's runner is at
 * a different address in every block it's tried for, and no one without the key can work that
 * address out before its transaction is seen. Once it is, anyone can create the same runner
 * there again, since the salt is only calldata: nothing may treat a runner's address as
 * authority.
 */

import { HDKey } from "@scure/bip32";
import { concatBytes } from "@noble/hashes/utils.js";
import { word } from "./abi.ts";
import { type Address, bytes, checksum, type EncodedCall, hex, keccak, memoryGas } from "./evm.ts";
import { authorizationsGas, type GasSchedule, intrinsic } from "./forks.ts";
import type { Signer } from "./transactions.ts";

/** Arachnid's deterministic deployment proxy
 * (github.com/Arachnid/deterministic-deployment-proxy). */
export const ARACHNID: Address = checksum("0x4e59b44847b379578588920cA78FbF26c0B4956C");
export const DEPLOYER: Address = ARACHNID;
export const RUNTIME = bytes(
  "0x7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffe03601600081602082378035828234f58015156039578182fd5b8082525050506014600cf3",
);
/** The salt Bruno's own deployments through it use (the payee's forwarder): the same address on
 * every chain. */
export const ARACHNID_SALT = new Uint8Array(32);

export const SALT_BYTES = 32;
/** What it answers: the address of what it created, alone. */
export const ANSWER_BYTES = 20;
/** A runner's salt where there's no submission to key it by (a quote, an estimate): every byte
 * nonzero, as a transaction's gas limit prices a salt's whatever they are. */
export const STAND_IN_SALT = new Uint8Array(SALT_BYTES).fill(0xff);
/** The path the salts' key is derived on from the float's key, apart from the concurrency
 * accounts' (m/44'/60'/0'/0/i). */
export const SALT_KEY_PATH = "m/44'/60'/1'/0/0";

/**
 * Where Arachnid's deployer puts `initcode`'s contract, at ARACHNID_SALT: the same on every
 * chain.
 */
export function arachnidAddress(initcode: Uint8Array): Address {
  return checksum(
    hex(
      keccak(concatBytes(Uint8Array.of(0xff), bytes(ARACHNID), ARACHNID_SALT, keccak(initcode)))
        .slice(12),
    ),
  );
}

/** The key runners' salts are hashed with: a private key derived from the float's on a path of
 * its own, used for nothing else. */
export function saltKey(floatAccount: Signer): Uint8Array {
  const key = HDKey.fromMasterSeed(floatAccount.privateKey).derive(SALT_KEY_PATH);
  if (key.privateKey === null) throw new Error("no private key for the salts");
  return key.privateKey;
}

/**
 * The salt of a submission's runner sent for block `block`: keccak256 of `key`, the submission's
 * id and the block. Different for every submission and every block, and not to be worked out
 * without the key.
 */
export function runnerSalt(key: Uint8Array, submissionId: Uint8Array, block: number): Uint8Array {
  return keccak(concatBytes(key, submissionId, word(BigInt(block))));
}

/** The call to the deployer that creates the runner `initcode` at `salt`, sending it `value`. */
export function runnerCall(salt: Uint8Array, initcode: Uint8Array, value = 0n): EncodedCall {
  return { to: DEPLOYER, data: concatBytes(salt, initcode), value };
}

/** A runner's calldata as its gas is priced: its salt's bytes nonzero, whatever they'll be, so
 * a transaction's gas limit doesn't move with the salt. */
export function pricedCalldata(initcode: Uint8Array): Uint8Array {
  return concatBytes(STAND_IN_SALT, initcode);
}

// the runtime's gas, from its opcodes: before CREATE2, PUSH32 CALLDATASIZE ADD PUSH1 DUP2 PUSH1
// DUP3 CALLDATACOPY DUP1 CALLDATALOAD DUP3 DUP3 CALLVALUE (copying and memory aside); after it,
// DUP1 ISZERO ISZERO PUSH1 JUMPI JUMPDEST DUP1 DUP3 MSTORE POP POP POP PUSH1 PUSH1 RETURN
const BEFORE_CREATE2_GAS = 3 + 2 + 3 + 3 + 3 + 3 + 3 + 3 + 3 + 3 + 3 + 3 + 2;
const AFTER_CREATE2_GAS = 3 + 3 + 3 + 3 + 10 + 1 + 3 + 3 + 3 + 2 + 2 + 2 + 3 + 3;
/** CALLDATACOPY's 3 a word; CREATE2's 6 a word of initcode (keccak) and 2 (EIP-3860). */
const COPY_WORD_GAS = 3;
const CREATE2_WORD_GAS = 6 + 2;

/** The deployer's own gas for `initcodeLength` bytes of initcode, the runner's aside. Its
 * answer is written to memory's first word, which the initcode has grown already unless it's
 * empty. */
export function overheadGas(initcodeLength: number, schedule: GasSchedule): number {
  const words = Math.ceil(initcodeLength / 32);
  return BEFORE_CREATE2_GAS +
    COPY_WORD_GAS * words +
    memoryGas(Math.max(words, 1)) +
    schedule.create +
    CREATE2_WORD_GAS * words +
    AFTER_CREATE2_GAS;
}

/**
 * The gas limit a transaction to the deployer needs for a runner that may use `runnerGas`:
 * intrinsic gas (the floor if higher, its salt priced as nonzero, a transaction `sending` ETH
 * charged for that where the fork does), the authorizations'
 * execution, the deployer's own gas,
 * and enough at CREATE2 that EIP-150's 63/64 rule still gives the runner `runnerGas`.
 */
export function transactionGas(
  initcode: Uint8Array,
  runnerGas: number,
  schedule: GasSchedule,
  authorizations = 0,
  emptyAuthorities = 0,
  sending = false,
): number {
  const overhead = overheadGas(initcode.length, schedule) - AFTER_CREATE2_GAS;
  const atCreate2 = Math.ceil(((runnerGas + AFTER_CREATE2_GAS) * 64) / 63);
  const execution = overhead + atCreate2 +
    authorizationsGas(schedule, emptyAuthorities, authorizations);
  const { standard, floor } = intrinsic(schedule, pricedCalldata(initcode), {
    authorizations,
    value: sending,
  });
  return Math.max(standard + execution, floor);
}

/**
 * The most gas a runner is given in a transaction to the deployer of `gasLimit`:
 * transactionGas's inverse.
 */
export function runnerGasWithin(
  initcode: Uint8Array,
  gasLimit: number,
  schedule: GasSchedule,
  authorizations = 0,
  emptyAuthorities = 0,
  sending = false,
): number {
  const overhead = overheadGas(initcode.length, schedule) - AFTER_CREATE2_GAS;
  const { standard } = intrinsic(schedule, pricedCalldata(initcode), {
    authorizations,
    value: sending,
  });
  const atCreate2 = gasLimit - standard - overhead -
    authorizationsGas(schedule, emptyAuthorities, authorizations);
  return Math.max(0, Math.floor((atCreate2 * 63) / 64) - AFTER_CREATE2_GAS);
}
