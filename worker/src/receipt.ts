/**
 * A payout address's receipt wallet, which a merchant's buyers pay (the hosted x402 facilitator,
 * facilitator.ts): the key Bruno holds for it. Derived, never stored: from a key of its own on
 * the float's (BIP-32, as the concurrency accounts are, on a path apart from theirs and the
 * salts'), hashed with the payout address. So a payout address has one receipt address for a
 * float, the same in both relays, and a receipt key says nothing of another's or of the
 * float's. As bruno/receipt.py's.
 */

import { concatBytes } from "@noble/hashes/utils.js";
import { HDKey } from "@scure/bip32";
import { type Address, bytes, keccak } from "./evm.ts";
import { type Signer, signer } from "./transactions.ts";

/** The path the receipt keys' root is derived on from the float's key, apart from the
 * concurrency accounts' (m/44'/60'/0'/0/i) and the salts' (m/44'/60'/1'/0/0). */
export const RECEIPT_ROOT_PATH = "m/44'/60'/2'/0/0";

/** The key every receipt key is hashed from: a private key derived from the float's on a path
 * of its own, used for nothing else. */
export function receiptRoot(floatAccount: Signer): Uint8Array {
  const key = HDKey.fromMasterSeed(floatAccount.privateKey).derive(RECEIPT_ROOT_PATH);
  if (key.privateKey === null) throw new Error("no private key for the receipt wallets");
  return key.privateKey;
}

/** The receipt wallet's private key for `payout`: keccak256 of `root` and the address's twenty
 * bytes. A hash that isn't a private key (zero, or the curve's order or more, about one in
 * 2**128) leaves that payout address with no receipt wallet. */
export function receiptKey(root: Uint8Array, payout: Address): Uint8Array {
  return keccak(concatBytes(root, bytes(payout)));
}

/** The receipt wallet for `payout`, as it signs. */
export function receiptAccount(root: Uint8Array, payout: Address): Signer {
  return signer(receiptKey(root, payout));
}
