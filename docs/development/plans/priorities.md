---
description: "Where Bruno stands, the strategy, and what's next: the 0.5.x series, then 0.6, Bruno's public interface and a Safe by one signature; eth_sendBundle the only way Bruno sends; MCP as a project of its own"
---

# Priorities

Where Bruno stands, why, and what's next. Last updated 2026-11-03.

## Status

- Live on Ethereum and Sepolia at bruno.mailam.app, v0.5.4. x402 there is atomic: the payment
  settles in the call's own transaction.
- The TypeScript client (`@mailam/bruno`) and the AgentKit provider (`@mailam/bruno-agentkit`)
  are on JSR and npm (dist-tag `latest`, by trusted publishing from CI), as -dev prereleases;
  tested on Deno, Node.js and Bun, and as npm, pnpm and Bun users install them. The
  documentation is published, with `llms.txt`.
- 0.5.2 (PR #15), released and deployed 2026-10-30: signed EIP-3009 transfers, smart wallets
  (ERC-6492), the relay as an ERC-4337 bundler ([plan](erc4337.md)), the packages on npm, the
  relay AGPL-3.0-or-later.
- 0.5.3 (PR #16), released, not deployed: the two relays aligned as equals, their drifts fixed on both and tested
  failing first, duplication folded; an unserved chain told apart; `make <net>-verify` funded
  by the network's rule (capped on Ethereum, bought through the pool on a testnet), rehearsed
  on anvil by `make anvil-verify`.
- 0.5.4 (PR #17), released and deployed 2026-11-03: the two relays aligned further, preparing
  0.6 (MCP): `relay` split by the state each part owns, the batches and the operations layer
  alike, units of time typed, and tests for a submission's expiry and the funding's failures.
  What it left is in [parity](parity.md).

## Strategy

- **Who it's for:** anyone holding a token but no ETH who needs a transaction sent: agents
  first, then wallets and the apps that embed them.
- **What sets it apart:** no API key, account or deposit; any token with EIP-3009; a call that
  would revert is never included, so it costs nothing; prices from on-chain pools.
- **The bet:** agents, reached through code against Bruno's client and its declarations, and
  through x402, which their wallets already pay. MCP becomes a project of its own built on
  Bruno ([MCP](MCP.md)).
- **The constraint, chosen:** Bruno sends only through `eth_sendBundle` (decided 2026-11-04):
  a bundle lands in the block it's signed for or not at all, so nothing of Bruno's waits in a
  pool. That keeps Bruno to chains with builders taking bundles (Ethereum, Sepolia), though
  agents and smart wallets are mostly on Base, where reaching them would take `upfront` x402
  and the public mempool. A narrower scope, kept on purpose.
- **Reaching users:** developers paying as they go, and agents through the client and x402
  ([adoption](adoption.md)).
- Detail: [smart wallets, adoption](smart-wallets.md#adoption),
  [ERC-4337 and paymasters](erc4337.md), [more chains](morechains.md).

## Now: 0.5.x

Point releases for smart-wallet integration and its tests, the smaller known gaps, hygiene and
docs. 0.6 is Bruno's public interface and a Safe by one signature (below).

- [x] ZeroDev's Kernel v3.2 as AgentKit's ZeroDev provider makes it: measured into the wallet
  gas schedule, and paying through Bruno over x402 and from an AgentKit agent
  ([plan](smart-wallets.md)).
- [x] ERC-4337, the relay as a permissionless bundler ([plan](erc4337.md)), 0.5.1: ERC-7769's
  methods and Pimlico's and ZeroDev's gas price methods; ops paying by their fees (viem with the
  relay's URL alone) or by a call in a token; EntryPoint v0.7 and v0.8, EIP-7702 accounts
  (viem's Simple7702, ZeroDev's Kernel v3.3); another's paymaster (Circle's, Pimlico's); a
  user docs page a situation, for LLMs.
- [x] ERC-4337 next, 0.5.2, released and deployed (2026-10-30): the price before signing, for an
  op paying by its calls (`postpaid_quoteUserOperation`); EIP-7702 accounts on v0.7 end to end,
  MetaMask's Stateless7702 and Alchemy's Modular Account v2, each with its SDK, the user docs'
  scripts tested on current viem, in `npm-userops/`.
- Accounts, done for 0.5.x (2026-10-31): those tested carry most of the ERC-4337 traffic outside
  Coinbase's ([what's in use](erc4337.md#accounts-and-entrypoints-in-use-2026-10-30)). Safe,
  Light Account and EntryPoint v0.9 are standard and untested, tested when someone asks; Trust
  Wallet's delegate has no SDK to point at another bundler; Coinbase Smart Wallet is v0.6 only,
  not planned.
- [ ] Listings, with the marketing push in mid-November 2026: BundleBear's labels, docs.erc4337.io,
  ethereum.org's developer tools and x402scan ([targets](adoption.md#targets)). The x402.org
  ecosystem page is gone, and agentic.market lists only what CDP's facilitator settles.
- [ ] Undo the workarounds for upstream issues as they're fixed
  ([upstream issues](../upstream.md)): the `@x402/svm` overrides, `tslib`, `skipLibCheck`.

## Next

### 0.6: the public interface, and a Safe by one signature

Bruno's public interface is three documents, the TypeScript client's type declarations,
OpenAPI and OpenRPC, and x402 beside them. The AX trials ([MCP](MCP.md#ax-trials-2026-11-03))
found models from Claude Haiku 5.5 up working from the client's declarations alone, once the
facts they need are stated.

1. The client's API settled, its declarations written as the reference a model reads as much
   as a developer; then published without `-dev`.
2. The relay's side of what the trials found: a refusal that says what to do next; the call's
   gas estimated, and the token's EIP-712 domain given, in the quote; a quote that shows what's
   gas and what's Bruno's.
3. [A Safe by one signature](safe.md): the user signs one EIP-712 batch; Bruno deploys their
   Safe if it isn't yet, runs the batch and is paid.
4. [Trusted targets](erc4337.md#trusted-targets-sending-without-the-runner-proposed-2026-11-03-to-ponder),
   to ponder first: the Safe and the EntryPoint (ops paying by their fees) called without the
   runner, whose deployer costs 32k gas a transaction on Prague and 196k on Amsterdam. Their
   own rules make an unpaid transaction revert; the proof of payment moves from the runner's
   measurement to them and Bruno's checks before signing, which changes the relay's core
   model.

### MCP, a project of its own

MCP spins out into a separate project that uses Bruno behind the scenes, as any client does:
intents, wallet composition, typed-data flows, skills and x402 tools, iterated as a product,
while Bruno's specification stays neutral. To ponder; its seed is [MCP](MCP.md).

## Alongside

Smaller pieces, each when there's room:

- [ ] **A cold wallet for profit.** The treasury tops the float up to `runway_days` of its
  measured spend; whatever the payee collects beyond that stays with the payee, whose key is
  hot (in the relay's secrets), as is the float's. What's kept hot is the runway times a
  buffer multiplier, a Decimal like the treasury's other multipliers (`cold_multiplier`, say;
  "1.5" as its default), for variance in prices and spend: once the payee holds more than that
  and its reserve, by enough that moving it is worth the gas, the treasury sends the excess to
  a cold address the relay holds no key for. The profit is saved, and the hot keys hold only a
  day or two of transactions. To settle: the multiplier's name and default, in which assets
  the profit is kept (as collected, or converted first), and the cold address in the
  configuration, a new key.
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

- **Base, through `upfront` x402** ([plan](x402-async.md)), set aside while `eth_sendBundle` is
  the only way Bruno sends (2026-11-04): Base has no bundles, so it means sending through the
  public mempool. If revisited: `upfront` with a choice of settler, then OP-stack L1-fee
  pricing and the deployer, USDC, pools and router on each chain.
- **More chains** ([plan](morechains.md)): atomic flows on Unichain if its bundles survive
  the move to OP Enterprise, tested locally with builder-playground first.
- **Facilitator** ([plan](facilitator.md)): revisit once `upfront` exists.
- **Agent standards** ([for analysis](agent-standards.md)): more of x402 (`upto`, its
  idempotency and receipt extensions, Permit2), EIP-2612 as a payment, ERC-8004, A2A, MPP;
  which ones Bruno's stack supports with little new, each to read before building.
- **Articles** on [blog.mailam.app](https://blog.mailam.app/) (the `mai-lam-labs/blog` repository): the x402 critique, and the runner's SELFDESTRUCT under
  EIP-8037.
- **Skills:** a `SKILL.md` for using Bruno with each wallet: the MCP project's.
- **Delegated permissions** ([plan](delegations.md)): ERC-7710 delegations and ERC-7715
  Advanced Permissions, an agent spending a user's USDC within limits, its gas through Bruno.
  Researched, not started.
- **Deploying from CI:** the Worker deployed by the release, from the tagged commit, rather than
  from a local checkout. Worth it once releases are frequent, or someone else deploys. A token
  that can deploy the Worker can read its `FLOAT_KEY` and `PAYEE_KEY` through a script of its
  own, so it's a deploy behind a protected environment with the user's approval per release, a
  minimal job with no caches, and a Cloudflare token scoped to Workers Scripts on the one
  account; `sepolia-verify` stays manual.
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
- [ ] A quote depends on the node that answered its fee history: on 2026-10-31, ethPandaOps'
  Sepolia node (ethrex) gave tips a twentieth of publicnode's and Tenderly's for the same
  blocks, and the Worker's isolates quoted 20× apart; it's no longer configured. Asking two or
  three nodes and taking the median would cost a read per node for every quote.

## Watch

- **x402 splits on EVM:** `exact`'s optional `extra.splits` (2026-10-26) lets a payment carry
  a facilitator's fee as its own leg, but only Solana defines an encoding. One for EVM would
  give the facilitator a fee in x402 itself ([option G](facilitator.md#fees-within-exact-the-options)).

- **EIP-8037:** All Core Devs (2026-10-22) postponed Hoodi's move to Amsterdam for further
  consideration. [Our reply](https://ethereum-magicians.org/t/eip-8037-state-creation-gas-cost-increase/25694/51?u=harryr)
  explains the SELFDESTRUCT refund problem.
- **Unichain bundles:** Flashbots referred
  [our questions](https://collective.flashbots.net/t/bundles-are-coming-to-unichain/5072/2?u=mailam)
  to the OP team (2026-10-27). Since Unichain's mainnet moved to OP Enterprise, run by
  Optimism, on 2026-10-29, its public endpoint refuses `eth_sendBundle`, as its testnet's has
  since its own move. Whether bundles come back is unknown; no OP Enterprise contact found.
