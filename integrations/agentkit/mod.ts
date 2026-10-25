/**
 * @mailam/bruno-agentkit: Bruno for Coinbase's AgentKit. An agent holding only USDC acts on
 * Ethereum with no ETH, paying Bruno over x402 only if its transaction succeeds and lands.
 *
 * ```ts
 * import { AgentKit } from "@coinbase/agentkit";
 * import { brunoActionProvider } from "@mailam/bruno-agentkit";
 *
 * const agentkit = await AgentKit.from({ walletProvider, actionProviders: [brunoActionProvider()] });
 * ```
 */

export * from "./src/brunoActionProvider.ts";
