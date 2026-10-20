/**
 * The relay's accounts: the float, the instance's own key, and the concurrency accounts derived
 * from it, BIP-32 with the float's private key as the seed on m/44'/60'/0'/0/i (DESIGN.md,
 * Accounts). The same derivation as the Python reference (bruno/accounts.py).
 */

import { HDKey } from "@scure/bip32";
import { type Signer, signer } from "./transactions.ts";

/** Concurrency account `index`, derived from the float's key. */
export function derive(float: Signer, index: number): Signer {
  const key = HDKey.fromMasterSeed(float.privateKey).derive(`m/44'/60'/0'/0/${index}`);
  if (key.privateKey === null) {
    throw new Error(`no private key at index ${index}`);
  }
  return signer(key.privateKey);
}
