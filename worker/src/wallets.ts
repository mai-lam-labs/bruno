/**
 * Smart wallets' gas schedule: for each wallet the relay knows, the deployment that makes it (its
 * factory called, or a meta-factory calling it), and what that wallet's signature check adds to
 * an EIP-3009 transfer it pays by, over a key's, per fork.
 * A quote prices a contract payer's transfer by it before the payment's signed; the payment
 * itself is priced by its transaction's estimate. A port of the Python reference
 * (bruno/wallets.py), where each figure is measured and its measurement described.
 */

import type { Deployment } from "./erc6492.ts";
import { type Address, checksum, hex, keccak, utf8 } from "./evm.ts";
import type { GasSchedule } from "./forks.ts";

/** A meta-factory's call (ZeroDev's FactoryStaker): deployWithFactory(factory, initData, salt),
 * the factory it calls first. */
const DEPLOY_WITH_FACTORY = hex(
  keccak(utf8("deployWithFactory(address,bytes,bytes32)")).subarray(0, 4),
);

/** What a contract payer's signature check adds to its transfer: `gas`, and its signature's
 * length. */
export interface Verification {
  readonly gas: number;
  readonly signatureBytes: number;
}

/** A wallet `factory` deploys, called directly or, if `through` names one, by that
 * meta-factory; its signature's length, and its `verifyGas` by fork. */
export interface WalletGas {
  readonly name: string;
  readonly factory: Address;
  readonly signatureBytes: number;
  readonly verifyGas: Readonly<Record<string, number>>;
  readonly through?: Address;
}

/** Whether `deployment` makes `wallet`: its factory called, or its meta-factory's
 * deployWithFactory naming it. */
export function deploys(wallet: WalletGas, deployment: Deployment): boolean {
  if (wallet.through === undefined) return deployment.factory === wallet.factory;
  const data = deployment.factoryData;
  return deployment.factory === wallet.through &&
    data.length >= 36 &&
    hex(data.subarray(0, 4)) === DEPLOY_WITH_FACTORY &&
    data.subarray(4, 16).every((b) => b === 0) &&
    checksum(hex(data.subarray(16, 36))) === wallet.factory;
}

/** Coinbase Smart Wallet v1 with a key as its owner (viem's toCoinbaseSmartAccount's default). */
export const COINBASE_SMART_WALLET: WalletGas = {
  name: "Coinbase Smart Wallet v1, a key owner",
  factory: checksum("0x0BA5ED0c6AA8c49038F819E587E2633c4A9F428a"),
  signatureBytes: 224,
  // measured 2026-10-27 (bruno/tests/test_wallets.py)
  verifyGas: { prague: 14_693, amsterdam: 15_093 },
};
/** v1.1, what CDP's smart accounts (AgentKit's among them) and the Base Account SDK's new
 * accounts are: signing as v1 does, built anew, so its check costs differently. */
export const COINBASE_SMART_WALLET_1_1: WalletGas = {
  name: "Coinbase Smart Wallet v1.1, a key owner",
  factory: checksum("0xBA5ED110eFDBa3D005bfC882d75358ACBbB85842"),
  signatureBytes: 224,
  // measured 2026-10-27 (bruno/tests/test_wallets.py)
  verifyGas: { prague: 14_368, amsterdam: 14_768 },
};
/** ZeroDev's Kernel v3.2 with a key as its owner, as AgentKit's ZeroDevWalletProvider makes it:
 * created through ZeroDev's meta-factory, which calls v3.2's factory; ZeroDev's ECDSA validator
 * its root, the intent executor installed. */
export const KERNEL_3_2: WalletGas = {
  name: "Kernel v3.2, a key owner",
  factory: checksum("0x7a1dBAB750f12a90EB1B60D2Ae3aD17D4D81EfFe"),
  through: checksum("0xd703aaE79538628d27099B8c4f621bE4CCd142d5"),
  signatureBytes: 86,
  // measured 2026-10-28 (bruno/tests/test_wallets.py)
  verifyGas: { prague: 16_636, amsterdam: 17_436 },
};
export const WALLETS: readonly WalletGas[] = [
  COINBASE_SMART_WALLET,
  COINBASE_SMART_WALLET_1_1,
  KERNEL_3_2,
];

/**
 * The verification of the wallet `deployment` makes, under `schedule`: its own if it's known,
 * else the largest gas and signature length of those that are.
 */
export function verification(deployment: Deployment | null, schedule: GasSchedule): Verification {
  const known = deployment === null ? undefined : WALLETS.find((w) => deploys(w, deployment));
  if (known !== undefined) {
    return { gas: gasOf(known, schedule), signatureBytes: known.signatureBytes };
  }
  return {
    gas: Math.max(...WALLETS.map((w) => gasOf(w, schedule))),
    signatureBytes: Math.max(...WALLETS.map((w) => w.signatureBytes)),
  };
}

function gasOf(wallet: WalletGas, schedule: GasSchedule): number {
  const gas = wallet.verifyGas[schedule.name];
  if (gas === undefined) throw new Error(`${wallet.name}: no figure for ${schedule.name}`);
  return gas;
}
