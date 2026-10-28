/**
 * Smart wallets' gas schedule: for each factory the relay knows, the wallet it deploys, and what
 * that wallet's signature check adds to an EIP-3009 transfer it pays by, over a key's, per fork.
 * A quote prices a contract payer's transfer by it before the payment's signed; the payment
 * itself is priced by its transaction's estimate. A port of the Python reference
 * (bruno/wallets.py), where each figure is measured and its measurement described.
 */

import { type Address, checksum } from "./evm.ts";
import type { GasSchedule } from "./forks.ts";

/** What a contract payer's signature check adds to its transfer: `gas`, and its signature's
 * length. */
export interface Verification {
  readonly gas: number;
  readonly signatureBytes: number;
}

/** A wallet `factory` deploys, its signature's length, and its `verifyGas` by fork. */
export interface WalletGas {
  readonly name: string;
  readonly factory: Address;
  readonly signatureBytes: number;
  readonly verifyGas: Readonly<Record<string, number>>;
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
export const WALLETS: readonly WalletGas[] = [COINBASE_SMART_WALLET, COINBASE_SMART_WALLET_1_1];

/**
 * The verification of the wallet `factory` deploys, under `schedule`: its own if it's known,
 * else the largest gas and signature length of those that are.
 */
export function verification(factory: Address | null, schedule: GasSchedule): Verification {
  const known = WALLETS.find((w) => w.factory === factory);
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
