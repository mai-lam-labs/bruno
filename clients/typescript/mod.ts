/**
 * @mailam/bruno: a client of Bruno, the permissionless post-paid gas relay. Send a call on
 * Ethereum holding no ETH: pay in USDC over x402 with any signer of typed data, only if the
 * call succeeds and lands.
 *
 * ```ts
 * import { Bruno, viemSigner } from "@mailam/bruno";
 *
 * const bruno = new Bruno({ chain: "ethereum" });
 * const paid = await bruno.payAndSend({ call: { to, data }, gas: 100_000n }, viemSigner(account));
 * console.log(paid.transaction);
 * ```
 */

export * from "./src/types.ts";
export * from "./src/client.ts";
export * from "./src/viem.ts";
export {
  chainIdOf,
  chooseRequirement,
  freshNonce,
  type PaymentRequired,
  type Requirement,
  type Settlement,
  type Transfer,
  transferFor,
  typedData,
  X402Error,
} from "./src/x402.ts";
