/**
 * Sending bundles to builders: eth_sendBundle, signed with an X-Flashbots-Signature for a
 * builder that wants one. The signature is the identity's EIP-191 signature of the hex keccak256
 * of the exact request body, sent as `address:signature`. The identity holds no funds.
 */

import { secp256k1 } from "@noble/curves/secp256k1.js";
import { concatBytes } from "@noble/hashes/utils.js";
import type { BundleParams, Mode } from "./bundle.ts";
import { hex, keccak, utf8 } from "./evm.ts";
import type { Signer } from "./transactions.ts";

export const SIGNATURE_HEADER = "X-Flashbots-Signature";
/** How long a builder is given to take a bundle: within the slot it's for. */
export const BUILDER_TIMEOUT_MS = 4_000;

/** A builder's bundle endpoint, the mode it's sent bundles in, and the identity that signs. */
export interface Builder {
  readonly url: string;
  readonly mode: Mode;
  readonly identity: Signer | null;
}

export class BuilderError extends Error {}

/** The X-Flashbots-Signature header's value for `body`. */
export function flashbotsSignature(body: Uint8Array, identity: Signer): string {
  const digest = hex(keccak(body));
  const message = utf8(`\x19Ethereum Signed Message:\n${digest.length}${digest}`);
  const signature = secp256k1.sign(keccak(message), identity.privateKey, {
    prehash: false,
    format: "recovered",
  });
  // r ‖ s ‖ v, v being 27 or 28
  const rsv = concatBytes(signature.slice(1), Uint8Array.of((signature[0] as number) + 27));
  return `${identity.address}:${hex(rsv)}`;
}

/** eth_sendBundle with `params` to `builder`: its result, typically the bundle's hash. */
export async function sendBundle(builder: Builder, params: BundleParams): Promise<unknown> {
  // signed as its UTF-8 bytes, which are what fetch sends
  const body = JSON.stringify({
    jsonrpc: "2.0",
    id: 1,
    method: "eth_sendBundle",
    params: [params],
  });
  const headers: Record<string, string> = { "content-type": "application/json" };
  if (builder.identity !== null) {
    headers[SIGNATURE_HEADER] = flashbotsSignature(utf8(body), builder.identity);
  }
  const response = await fetch(builder.url, {
    method: "POST",
    body,
    headers,
    signal: AbortSignal.timeout(BUILDER_TIMEOUT_MS),
  });
  if (!response.ok) {
    throw new BuilderError(`${builder.url}: HTTP ${response.status}`);
  }
  const answer: { result?: unknown; error?: { code: number; message: string } } = await response
    .json();
  if (answer.error !== undefined) {
    throw new BuilderError(`${builder.url}: ${answer.error.code} ${answer.error.message}`);
  }
  return answer.result;
}
