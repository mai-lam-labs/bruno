/**
 * @mailam/bruno-agentkit: Bruno for Coinbase's AgentKit. An agent holding only USDC acts on
 * Ethereum with no ETH, paying Bruno over x402 only if its transaction succeeds and lands.
 *
 * Wherever AgentKit runs: Node 20 or later, Bun, Deno. From npm: `npx jsr add
 * @mailam/bruno-agentkit` (or `pnpm i jsr:@mailam/bruno-agentkit`, `bunx jsr add …`).
 *
 * ```ts
 * import { AgentKit } from "@coinbase/agentkit";
 * import { brunoActionProvider } from "@mailam/bruno-agentkit";
 *
 * const agentkit = await AgentKit.from({ walletProvider, actionProviders: [brunoActionProvider()] });
 * ```
 */

export * from "./src/brunoActionProvider.ts";
