---
description: "Where Bruno stands and what's next: integrations, then x402 settled first, then MCP"
---

# Priorities

Where Bruno stands and what's next. Last updated 2026-10-27.

## Status

- Live on Ethereum and Sepolia at bruno.mailam.app, v0.4.0. v0.4.1 (the documentation site,
  and the same failure handling in both relays) is in review.
- x402 on Ethereum is atomic: the payment settles in the call's own transaction.
- The TypeScript client (`@mailam/bruno`) and the AgentKit provider (`@mailam/bruno-agentkit`)
  are on JSR.

## Now: integrations

Each integration ships with a tutorial.

- [x] TypeScript client on JSR, tested on Deno, Node.js and Bun.
- [x] AgentKit action provider on JSR, tested as npm, pnpm and Bun users install it.
- [x] Smart wallets paying over x402 (ERC-1271), tested with Coinbase Smart Wallet.
- [ ] AgentKit's `send_usdc_gasless` from a smart wallet: sign the transfer to the recipient
  with USDC's `bytes` overload when the signature isn't 65 bytes.
- [ ] ERC-6492: a smart wallet that isn't deployed yet pays, and its first payment deploys it.
- [ ] A local MCP server (npm, wrapping the live API), listed in the MCP Registry, Glama and
  Anthropic's directory.
- [ ] Listings: the x402.org ecosystem page and Coinbase's agentic.market.
- [ ] Tools for GOAT, ElizaOS and the Vercel AI SDK.
- [ ] Base, through `upfront` x402 (below). Most AgentKit agents are on Base.
- [ ] Drop `skipLibCheck` from `make npm-test` once AgentKit's dependencies type-check
  (zerodevapp/sdk#279, zerodevapp/intent-sdk#33, coinbase/agentkit#1532).

## Small items

- [x] Publish the documentation, with `llms.txt`.
- [x] Ask in public about Unichain's bundles ([our reply](https://collective.flashbots.net/t/bundles-are-coming-to-unichain/5072/2?u=mailam)).
- [ ] Accept a signed EIP-3009 transfer as a submission's payment over REST and JSON-RPC. The
  relay already handles it internally; this needs a specification change, and MCP builds on
  it.
- [ ] Open an issue on x402 about request-bound nonces (from the x402 article).

## Next: x402 settled first

See [the plan](x402-async.md).

1. `upfront` with a choice of settler: Bruno itself on any chain, or a facilitator where one
   serves the chain. First on Base Sepolia, and self-settled on Sepolia.
2. Sending on L2s: OP-stack L1-fee pricing, and the deployer, USDC, pools and router on each
   chain.
3. x402 over MCP, as a paid tool.

## Then: MCP

See [the plan](MCP.md).

1. A stateless `/mcp` with the `bruno` and `3009` tool groups, listed in the MCP Registry.
2. ERC-20 and Uniswap helpers.
3. Calibur.
4. Tasks and prompts.

## Later

- **More chains** ([plan](morechains.md)): atomic flows on Unichain, tested locally with
  builder-playground first.
- **Facilitator** ([plan](facilitator.md)): revisit once MCP and `upfront` exist.
- **Articles** on docs.mailam.app: the x402 critique, and the runner's SELFDESTRUCT under
  EIP-8037.
- **Skills:** a `SKILL.md` for using Bruno with each wallet, written as each MCP slice lands.

## Before 1.0

- [ ] Test the client in browsers and Cloudflare Workers, then mark it so on JSR.

## Watch

- **BuilderNet:** a May 2026 report of a bundled transaction included even though it reverted
  (cause unknown). Bruno's safety on mainnet relies on builders dropping reverting
  transactions.
- **EIP-8037:** All Core Devs (2026-10-22) postponed Hoodi's move to Amsterdam for further
  consideration. [Our reply](https://ethereum-magicians.org/t/eip-8037-state-creation-gas-cost-increase/25694/51?u=harryr)
  explains the SELFDESTRUCT refund problem.
- **Unichain bundles:** waiting for an answer to
  [our reply](https://collective.flashbots.net/t/bundles-are-coming-to-unichain/5072/2?u=mailam)
  on the Flashbots Collective.
