---
description: "Where Bruno stands, the strategy, and what's next: 0.5.0, then Base and MCP side by side"
---

# Priorities

Where Bruno stands, why, and what's next. Last updated 2026-10-30.

## Status

- Live on Ethereum and Sepolia at bruno.mailam.app, v0.4.1. x402 there is atomic: the payment
  settles in the call's own transaction.
- The TypeScript client (`@mailam/bruno`) and the AgentKit provider (`@mailam/bruno-agentkit`)
  are on JSR (0.4.x prereleases) and, from 0.5.2, on npm too (dist-tag `dev`, by trusted
  publishing from CI); tested on Deno, Node.js and Bun, and as npm, pnpm and Bun users install
  them. The documentation is published, with `llms.txt`.
- 0.5.0 (PR #13) and 0.5.1 (PR #14) are merged, never tagged: signed EIP-3009 transfers as
  payment, smart wallets deployed or not (ERC-6492), ZeroDev's Kernel, and the relay as an
  ERC-4337 bundler ([plan](erc4337.md)).
- 0.5.2 (PR #15), the release to tag and deploy: the price before signing for ops paying by
  their calls, EIP-7702 accounts on EntryPoint v0.7 (MetaMask's, Alchemy's), the packages on
  npm, a hardened release pipeline, and the relay relicensed AGPL-3.0-or-later (the clients
  Apache-2.0, the docs CC0). npm's trusted publishing must first publish by 2026-11-01.

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

## Now: 0.5.x

Point releases for smart-wallet integration and its tests, the smaller known gaps, hygiene and
docs. 0.6 is MCP.

- [x] ZeroDev's Kernel v3.2 as AgentKit's ZeroDev provider makes it: measured into the wallet
  gas schedule, and paying through Bruno over x402 and from an AgentKit agent
  ([plan](smart-wallets.md)).
- [x] ERC-4337, the relay as a permissionless bundler ([plan](erc4337.md)), 0.5.1: ERC-7769's
  methods and Pimlico's and ZeroDev's gas price methods; ops paying by their fees (viem with the
  relay's URL alone) or by a call in a token; EntryPoint v0.7 and v0.8, EIP-7702 accounts
  (viem's Simple7702, ZeroDev's Kernel v3.3); another's paymaster (Circle's, Pimlico's); a
  user docs page a situation, for LLMs.
- [ ] ERC-4337 next, 0.5.2, released and deployed: the price before signing, for an op paying by
  its calls (`postpaid_quoteUserOperation`, built); EIP-7702 accounts on v0.7 end to end,
  MetaMask's Stateless7702 and Alchemy's Modular Account v2 (built, each with its SDK, the user
  docs' scripts now tested on current viem, in `npm-userops/`). Trust Wallet's delegate waits
  for an SDK to document. Then Safe and Light Account
  ([what's in use](erc4337.md#accounts-and-entrypoints-in-use-2026-10-30)); EntryPoint v0.9
  after; Coinbase Smart Wallet is v0.6 only, not planned.
- [ ] Release, when the series has gone far enough: tag, deploy the Worker. The JSR packages stay
  prereleases until their API settles.
- [ ] Listings: the x402.org ecosystem page and Coinbase's agentic.market.
- [ ] Undo the workarounds for upstream issues as they're fixed
  ([upstream issues](../upstream.md)): the `@x402/svm` overrides, `tslib`, `skipLibCheck`.

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
- [ ] Unichain: `mainnet.unichain.org` refuses `eth_sendBundle` since the 2026-10-29 move to
  OP Enterprise (probed 2026-10-30), as Sepolia has since its move. Asked in Uniswap/docs#1183;
  pushing OP Labs for revert protection in Subblocks is sidelined (2026-10-30): OP Enterprise
  chains are `upfront`-only ([plan](morechains.md)).

## Later

- **More chains** ([plan](morechains.md)): atomic flows on Unichain if its bundles survive
  the move to OP Enterprise, tested locally with builder-playground first.
- **Facilitator** ([plan](facilitator.md)): revisit once MCP and `upfront` exist.
- **Articles** on docs.mailam.app: the x402 critique, and the runner's SELFDESTRUCT under
  EIP-8037.
- **Skills:** a `SKILL.md` for using Bruno with each wallet, written as each MCP slice lands.
- **Delegated permissions** ([plan](delegations.md)): ERC-7710 delegations and ERC-7715
  Advanced Permissions, an agent spending a user's USDC within limits, its gas through Bruno.
  Researched, not started.
- **Bundler methods beyond ERC-7769** ([candidates](erc4337.md#bundler-methods-beyond-erc-7769-candidates-2026-10-30)):
  a synchronous send, an op's status; built when a client using them comes along.

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
  (Kernel v3.2's, with a key owner): a Safe with several owners, or a passkey owner, pays too
  little unless the client passes `verifyGas` ([plan](smart-wallets.md)).
- [ ] x402's refusal code can name the signature when the cause is elsewhere: for a deployed
  wallet still to be prepared, or an undeployed one whose inner signature isn't 65 bytes.
- [ ] A REST transfer's validity window is checked only by simulating it: one valid too late
  fails as a simulation, and one expiring before `maxBlockNumber` is taken, then rejected.
- [ ] Uniswap v4 pools and TWAP prices.
- [ ] A treasury's v2 hops go through the router's own Uniswap V2 factory: a pair from another
  factory prices a token, but can't be swapped through.
- [ ] bloXroute's `blxr_submit_bundle`.
- [ ] Testing: an rbuilder devnet running the same scenarios as the mock builder; a log of
  every bundle sent.
- [ ] `postpaid_quoteUserOperation` prices an op with its stub signature's length: a real one
  longer than its stub (viem's aren't) could leave the quote a few gas short, for the underpaid
  answer to settle.

## Watch

- **BuilderNet:** a May 2026 report of a bundled transaction included even though it reverted
  (cause unknown). Bruno's safety on mainnet relies on builders dropping reverting
  transactions.
- **EIP-8037:** All Core Devs (2026-10-22) postponed Hoodi's move to Amsterdam for further
  consideration. [Our reply](https://ethereum-magicians.org/t/eip-8037-state-creation-gas-cost-increase/25694/51?u=harryr)
  explains the SELFDESTRUCT refund problem.
- **Unichain bundles:** Flashbots referred
  [our questions](https://collective.flashbots.net/t/bundles-are-coming-to-unichain/5072/2?u=mailam)
  to the OP team (2026-10-27). Since Unichain's mainnet moved to OP Enterprise, run by
  Optimism, on 2026-10-29, its public endpoint refuses `eth_sendBundle`, as its testnet's has
  since its own move. Whether bundles come back is unknown; no OP Enterprise contact found.
