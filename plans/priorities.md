# Priorities

Where Bruno stands, what's next and why. Kept up to date as things land; last updated
2026-10-24 (the board: integrations first).

## Where things stand

Live on Ethereum and Sepolia at v0.3.1 (bruno.mailam.app; release, image and deploy done).
Atomic x402 on Ethereum mainnet, within the spec: the payment settled in the call's own
transaction. Facilitators settle on Ethereum (Daydreams), but none can make the payment
conditional on the call. Everything below is planned, not built.

| Workstream | State | Buys | Costs |
|---|---|---|---|
| Signed 3009 transfer as `submit`'s payment | the relay has the path; REST/JSON-RPC don't expose it | USDC-only users without x402; the MCP flows rest on it | small; a SPEC change |
| **MCP** (`v0X0-MCP.md`) | plan, detailed | the agent surface: registries, composing with wallet MCPs | the biggest build: core a few weeks, both relays |
| x402 article + upstream issues (`articles/x402-critique.md`) | draft | positioning, top of the funnel | the author's time, slowly |
| Docs / blog / skills | none | long-tail copy, a home for news and musings | the site is cheap; writing isn't |
| **x402-async, `upfront`** (`v0X0-x402-async.md`) — **next** | plan | the most chains (no bundler needed: paid before sending, a revert is the client's), Bazaar visibility, a paid MCP tool | L2 pricing and a treasury per chain it executes on; cross-chain payment deferred (a cross-chain treasury) |
| More chains, Unichain first (`v0X0-morechains.md`) | plan | a second chain with the guarantee | OP-stack L1-fee pricing, 1 s blocks on the Worker, more operations |
| Facilitator (`v0X0-facilitator.md`) | plan, six fee options | Bazaar presence, its own discovery | sponsored gas on Ethereum, abuse, a forwarder to audit; L2s are CDP's ground |

## Now: the integration sprint (the board, 2026-10-24)

The board's verdict: integrations, now, do or die. Everything else waits behind them. Each is
published as our own package, so nothing waits on another project's review; upstream PRs go
alongside.

1. **TypeScript client** (`clients/typescript/`, `@mailam/bruno`): quote, submit, status, and
   paying over x402 with any signer of typed data (viem's accounts, a wallet MCP's), no
   dependencies. The foundation of every integration below. **Published on JSR (2026-10-25),**
   `0.4.0-dev.2`.
2. **AgentKit action provider** (Coinbase's agent kit; ~40 third-party providers): our
   package, and a PR upstream (its merges have slowed: we don't wait on it). **Published on JSR
   (2026-10-25)** as `@mailam/bruno-agentkit`, `0.4.0-dev.2`. Next: publishing from the release
   workflow (GitHub OIDC, once both are linked to the repo); a smart wallet's own signature of
   its USDC transfer (the `bytes` overload).
3. **MCP server, local** (npm, wrapping the live API): the MCP Registry, Glama, Anthropic's
   directory. The relay-side `/mcp` (`v0X0-MCP.md`) comes later.
4. **Listings:** the x402.org ecosystem page (PR), Coinbase's agentic.market.
5. **GOAT, ElizaOS, Vercel AI SDK** tools, on the client.
6. **Base, via `upfront`,** in parallel: most AgentKit agents are on Base, and Bruno serves
   Ethereum and Sepolia. The x402-async slices, pulled forward.
7. **Smart-wallet payers,** as x402's reference facilitator takes them (its text says 65 bytes;
   decided 2026-10-24 to follow the code). **ERC-1271 done** (uncommitted): any-length
   signatures, the `bytes` overload, a contract payer's transfer estimated as it runs (402 at
   what it needs), refusals asking its `isValidSignature`; tested against a vendored Coinbase
   Smart Wallet on both relays. **Next: ERC-6492,** an undeployed account deployed by its first
   payment, its factory allowlisted (Coinbase Smart Wallet's first).

Each integration ships with its tutorial. The measure: distinct paying agents (payers) a week,
by integration.

## Easy wins (days)

1. **Ask in public about Unichain's bundles** (posted 2026-10-23, see Watch): a reply on the Flashbots Collective thread
   "Bundles are coming to Unichain" (testnet `eth_sendBundle`; gossip and fallback blocks),
   where the builder's own team answers, citable. Not email: it reaches sales, not answers.
   Nothing waits on it: builder-playground (op-rbuilder, revert protection on) tests locally,
   and mainnet (a dropped bundle free, an included one cents) checks live.
2. **Open the request-bound nonce issue upstream:** no code, the most constructive point in
   the article.
3. **Publish what exists:** README, SPEC, DESIGN at docs.mailam.app/bruno; `llms.txt`.
4. **Signed transfer as `submit`'s payment:** small, unblocks MCP and the gasless-transfer
   tutorial.

## Then x402-async, in slices (Base pulled into the sprint)

Decided 2026-10-23: first, ahead of MCP. It unlocks the most chains and the Bazaar; no bundler
is needed, since the payment settles before the call is sent and a revert is the client's.

1. **`upfront` with a pluggable settler:** Bruno itself (every chain) or a facilitator
   (where one serves the chain), facilitators never required. Proven first on Base Sepolia,
   which all four keyless facilitators serve (x402.org, PayAI and Dexter with `bazaar`,
   Daydreams); self-settled on Sepolia, where none does.
2. **Execution on L2s** (Unichain Sepolia funded, Base, Arbitrum…): OP-stack L1-fee pricing
   first; the deployer, USDC, pools and router on each (a treasury per chain), checked by
   preflight; sent by plain `eth_sendRawTransaction`. Here a facilitator becomes optional for
   a Bazaar listing (CDP or another), pluggable, never required.
3. **x402 over MCP** as a paid tool, the settlement's hash in hand at once.

## Then MCP, in slices

Each slice ships with a tutorial that's real when published.

1. Stateless `/mcp`, the `bruno` and `3009` groups, both eras; listed in the MCP Registry.
2. `erc20` / `uniswap` helpers.
3. Calibur.
4. Tasks and Prompts.

## Slow burns

- **Articles / blog:** one site (docs.mailam.app with a `/blog`) rather than two; each article
  follows something shipped and links to it. Queued: the x402 critique; the runner's
  SELFDESTRUCT and EIP-8037.
- **Skills:** `SKILL.md` files for "Bruno with wallet X", written as each MCP slice lands; also
  usable as Claude Code skills and as MCP Prompts.
- **More chains:** atomic flows on Unichain once its sender is tested locally (builder-playground).
- **Facilitator:** furthest from the niche; revisit when MCP and async exist and the forwarder
  (option D) has a use: revenue splits for paid tools.

## Order

The integration sprint (client → AgentKit → local MCP → listings → frameworks; Base and
smart-wallet payers alongside) → the rest of x402-async → the relay-side MCP → articles as
integrations land → atomic chains (Unichain) → facilitator. The article frames `upfront` as x402's own flow, beside atomic, never instead. The scarce resource is attention: one front at a time.

## Watch

- **BuilderNet:** a May 2026 report of a bundle transaction included reverting despite default
  semantics (cause unknown). The mainnet guarantee rests on builders dropping reverts.
- **EIP-8037:** ACDE 2026-10-22 (from a transcript summary): the Hoodi migration is cancelled;
  SELFDESTRUCT and our thread weren't mentioned. ChainSecurity's exploit (state gas returned
  when a child frame reverts) puts refunds and the reserve first in line if 8037 is revisited
  (Ansgar). A same-transaction SELFDESTRUCT refill will be judged on whether gas left rises
  mid-execution: applied at transaction end, it doesn't. Sepolia, on Amsterdam's rules, stops on
  an unrecognised schedule until it's added. Our post: Magicians #51.
- **Flashbots Collective, "Bundles Are Coming to Unichain":** our reply (testnet
  `eth_sendBundle` not whitelisted on sepolia.unichain.org; gossip and fallback blocks) posted
  2026-10-23, awaiting moderation.
- **docs.gelato.cloud's certificate:** expired 2026-10-09; reported by ticket (2026-10-23).

## Decided

- 2026-10-24: **integrations first** (the board): the sprint above ahead of every plan.

- 2026-10-23: no mainnet evidence run (`ethereum-verify`); Sepolia's testing stands, mainnet
  settlements accrue with use.
- 2026-10-23: x402-async first. Bruno settles its own payments on the call's chain; facilitators
  optional and pluggable, for listings only; same-chain only, cross-chain gas deferred to "teh borg"
  (the author's cross-chain gas project) once it's deployed.

## Done

- 2026-10-23: groundwork for L2s (uncommitted on `next-0.4.0`): OP-stack fees beyond gas in
  both relays (measured, the formula checked against the oracle); a batch cap per RPC node;
  preflight checks the block time and the 5 s step floor (`min_step_seconds`). Base Sepolia
  and Unichain Sepolia funded, our deployer and x402's Permit2 proxy on both.

- 2026-10-22: v0.3.1 (RPC nodes shuffled, `config.local.json`, `make rpc-check`): released,
  image published, deployed.
