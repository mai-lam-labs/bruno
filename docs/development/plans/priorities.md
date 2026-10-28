---
description: "Where Bruno stands, the strategy, and what's next: 0.5.0, then Base and MCP side by side"
---

# Priorities

Where Bruno stands, why, and what's next. Last updated 2026-10-28.

## Status

- Live on Ethereum and Sepolia at bruno.mailam.app, v0.4.1. x402 there is atomic: the payment
  settles in the call's own transaction.
- The TypeScript client (`@mailam/bruno`) and the AgentKit provider (`@mailam/bruno-agentkit`)
  are on JSR, tested on Deno, Node.js and Bun, and as npm, pnpm and Bun users install them. The
  documentation is published, with `llms.txt`.
- v0.5.0 is in review (PR #13): a signed EIP-3009 transfer as a submission's payment over REST
  and JSON-RPC, and smart wallets, deployed or not (ERC-6492), paying over x402 and through
  AgentKit's `send_usdc_gasless`; tested with Coinbase Smart Wallet ([plan](erc6492.md)).

## Strategy

- **Who it's for:** anyone holding a token but no ETH who needs a transaction sent: agents
  first, then wallets and the apps that embed them.
- **What sets it apart:** no API key, account or deposit; any token with EIP-3009; a call that
  would revert is never included, so it costs nothing; prices from on-chain pools.
- **The bet:** agents, reached through AgentKit, MCP and the agent frameworks, paying as they
  already do with x402.
- **The constraint:** agents and smart wallets are mostly on Base, and Bruno is on Ethereum and
  Sepolia. Reaching Base takes `upfront` x402, since Base has no bundles that drop reverts.
- Detail: [smart wallets, adoption](smart-wallets.md#adoption),
  [ERC-4337 and paymasters](erc4337.md), [more chains](morechains.md).

## Now: 0.5.0

- [ ] Release 0.5.0: merge PR #13, tag `v0.5.0`, deploy the Worker. The JSR packages stay
  prereleases (`0.5.0-dev.1`) until their API settles.
- [ ] ZeroDev's Kernel as AgentKit's ZeroDev provider makes it: measured into the wallet gas
  schedule, and tested paying through Bruno ([plan](smart-wallets.md)).
- [ ] Listings: the x402.org ecosystem page and Coinbase's agentic.market.
- [ ] Drop `skipLibCheck` from `make npm-test` once AgentKit's dependencies type-check
  (zerodevapp/sdk#279, zerodevapp/intent-sdk#33, coinbase/agentkit#1532).

## Next: two tracks, side by side

### Base, through `upfront` x402

See [the plan](x402-async.md).

1. `upfront` with a choice of settler: Bruno itself on any chain, or a facilitator where one
   serves the chain. First on Base Sepolia, and self-settled on Sepolia.
2. Sending on L2s: OP-stack L1-fee pricing, and the deployer, USDC, pools and router on each
   chain.

### MCP

See [the plan](MCP.md). Over HTTPS, served by the relay, with nothing installed on the user's
machine.

1. A stateless `/mcp` with the `bruno` and `3009` tool groups, listed in the MCP Registry,
   Glama and Anthropic's directory.
2. ERC-20 and Uniswap helpers.
3. Calibur.
4. Tasks and prompts.

### Where they meet

- x402 over MCP, as a paid tool, once `upfront` and the first MCP slice exist.

## Alongside

Smaller pieces, each when there's room:

- [ ] Smart wallets in the plan's order ([plan](smart-wallets.md)): Light Account and Safe with
  one owner, passkey owners, keys upgraded with EIP-7702, then the rest.
- [ ] A recipe per smart-account SDK in the docs (viem, permissionless.js, ZeroDev, Alchemy,
  MetaMask's kit), each with a tutorial.
- [ ] Tools for GOAT, ElizaOS and the Vercel AI SDK, each with a tutorial.
- [ ] Open an issue on x402 about request-bound nonces (from the x402 article).
- [ ] Unichain: probe `mainnet.unichain.org` for `eth_sendBundle` over the days after the
  2026-10-29 switch-over to OP Enterprise; then find who to ask on the OP side
  ([plan](morechains.md)).

## Later

- **More chains** ([plan](morechains.md)): atomic flows on Unichain if its bundles survive
  the move to OP Enterprise, tested locally with builder-playground first.
- **Facilitator** ([plan](facilitator.md)): revisit once MCP and `upfront` exist.
- **Articles** on docs.mailam.app: the x402 critique, and the runner's SELFDESTRUCT under
  EIP-8037.
- **Skills:** a `SKILL.md` for using Bruno with each wallet, written as each MCP slice lands.

## Research

- [ ] ERC-4337 ([plan](erc4337.md)): how bundlers and paymasters work, how Bruno differs, and
  whether Bruno should be a paymaster service or a bundler. Circle's and Pimlico's paymasters
  are recorded; the sponsoring paymasters are next.

## Before 1.0

- [ ] Test the client in browsers and Cloudflare Workers, then mark it so on JSR.
- [ ] Settle the client's and the AgentKit provider's API, then publish them without `-dev`.

## Known gaps

- [ ] The Python relay keeps its submissions and the treasury's snapshot in memory: a restart
  loses the queue, and the treasury measures spend afresh. The Worker keeps both in its
  Durable Object's storage.
- [ ] A funding transaction given up on may still be pending: the next reuses its nonce and
  can be refused as an underpriced replacement.
- [ ] More than one deployment in a submission.
- [ ] A smart wallet the gas schedule doesn't know is priced at the largest figure it has
  (Coinbase's, with a key owner): a Safe with several owners, or a passkey owner, pays too
  little unless the client passes `verifyGas` ([plan](smart-wallets.md)).
- [ ] x402's refusal code can name the signature when the cause is elsewhere: for a deployed
  wallet still to be prepared, or an undeployed one whose inner signature isn't 65 bytes.
- [ ] A node error other than a revert while planning (an estimate, the payer's code) is an
  internal error, not "unavailable".
- [ ] `verifyGas`'s bound applies to the quote; the gas estimated at submission has only the
  node's own cap.
- [ ] A REST transfer's validity window is checked only by simulating it: one valid too late
  fails as a simulation, and one expiring before `maxBlockNumber` is taken, then rejected.
- [ ] Wording the two relays still differ on, with the same codes: "over N" and "out of range"
  for quantities past a bound; JSON-RPC `params` that aren't a list.
- [ ] The client's `payAndSubmit` doesn't sign again when underpaid, as its x402 payments do.
- [ ] Uniswap v4 pools and TWAP prices.
- [ ] A treasury's v2 hops go through the router's own Uniswap V2 factory: a pair from another
  factory prices a token, but can't be swapped through.
- [ ] bloXroute's `blxr_submit_bundle`.
- [ ] Testing: an rbuilder devnet running the same scenarios as the mock builder; a log of
  every bundle sent.

## Watch

- **BuilderNet:** a May 2026 report of a bundled transaction included even though it reverted
  (cause unknown). Bruno's safety on mainnet relies on builders dropping reverting
  transactions.
- **EIP-8037:** All Core Devs (2026-10-22) postponed Hoodi's move to Amsterdam for further
  consideration. [Our reply](https://ethereum-magicians.org/t/eip-8037-state-creation-gas-cost-increase/25694/51?u=harryr)
  explains the SELFDESTRUCT refund problem.
- **Unichain bundles:** Flashbots referred
  [our questions](https://collective.flashbots.net/t/bundles-are-coming-to-unichain/5072/2?u=mailam)
  to the OP team (2026-10-27): Unichain's mainnet moves to OP Enterprise, run by Optimism, on
  2026-10-29, and its testnet already has. Whether bundles and revert protection survive is
  unknown.
