/**
 * @mailam/bruno: a client of Bruno, the permissionless post-paid gas relay. Send a call on
 * Ethereum holding no ETH, paid in ETH or a token as the call runs, only if it succeeds and
 * lands: by the call itself, or over x402 with any signer of typed data.
 *
 * No dependencies and web-standard APIs only (`fetch`, Web Crypto): Deno, Node 20 or later,
 * Bun, Cloudflare Workers and browsers alike.
 *
 * ```ts
 * import { Bruno, viemSigner } from "@mailam/bruno";
 *
 * const bruno = new Bruno({ chain: "ethereum" });
 * const paid = await bruno.x402.payAndSend({ call: { to, data }, gas: 100_000n }, viemSigner(account), {
 *   pay: [{ token: "USDC", max: { decimal: "2.5" } }],
 * });
 * console.log(paid.transaction, paid.paid.decimal);
 * ```
 */

export * from "./src/types.ts";
export * from "./src/tokens.ts";
export * from "./src/client.ts";
export * from "./src/viem.ts";
export {
  chainIdOf,
  freshNonce,
  type PaymentRequired,
  type Requirement,
  type Settlement,
  type Transfer,
  transferFor,
  typedData,
  usableRequirements,
  X402Error,
} from "./src/x402.ts";
