# Priorities

Where Bruno stands, what's next and why. Kept up to date as things land; last updated
2026-10-23 (x402-async moved first).

## Where things stand

Live on Ethereum and Sepolia at v0.3.1 (bruno.mailam.app; release, image and deploy done).
x402 on Ethereum mainnet, within the spec, settled atomically with the call: no facilitator
offers it. Everything below is planned, not built.

| Workstream | State | Buys | Costs |
|---|---|---|---|
| Signed 3009 transfer as `submit`'s payment | the relay has the path; REST/JSON-RPC don't expose it | USDC-only users without x402; the MCP flows rest on it | small; a SPEC change |
| **MCP** (`v0X0-MCP.md`) | plan, detailed | the agent surface: registries, composing with wallet MCPs | the biggest build: core a few weeks, both relays |
| x402 article + upstream issues (`articles/x402-critique.md`) | draft | positioning, top of the funnel | the author's time, slowly |
| Docs / blog / skills | none | long-tail copy, a home for news and musings | the site is cheap; writing isn't |
| **x402-async, `upfront`** (`v0X0-x402-async.md`) — **next** | plan | the most chains (no bundler needed: paid before sending, a revert is the client's), Bazaar visibility, a paid MCP tool | L2 pricing and a treasury per chain it executes on; cross-chain payment deferred (a cross-chain treasury) |
| More chains, Unichain first (`v0X0-morechains.md`) | plan | a second chain with the guarantee | OP-stack L1-fee pricing, 1 s blocks on the Worker, more operations |
| Facilitator (`v0X0-facilitator.md`) | plan, six fee options | Bazaar presence, its own discovery | sponsored gas on Ethereum, abuse, a forwarder to audit; L2s are CDP's ground |

## Easy wins (days)

1. **Ask Uniswap/Unichain about the bundle endpoint** (its future, gossip, fallback): long lead
   time, and it decides whether `v0X0-morechains.md` exists.
2. **Open the request-bound nonce issue upstream:** no code, the most constructive point in
   the article.
3. **Publish what exists:** README, SPEC, DESIGN at docs.mailam.app/bruno; `llms.txt`.
4. **Signed transfer as `submit`'s payment:** small, unblocks MCP and the gasless-transfer
   tutorial.

## The main build: x402-async, in slices

Decided 2026-10-23: first, ahead of MCP. It unlocks the most chains and the Bazaar; no bundler
is needed, since the payment settles before the call is sent and a revert is the client's.

1. **`upfront` on the chains we run,** paid on the call's chain and settled by Bruno itself:
   each chain's treasury replenishes its own float, as now. No facilitator, no new chain.
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
- **More chains:** after Unichain answers.
- **Facilitator:** furthest from the niche; revisit when MCP and async exist and the forwarder
  (option D) has a use: revenue splits for paid tools.

## Order

x402-async (`upfront` on our chains, then L2 execution) → the small API change →
MCP core → article and issues → tutorials as slices land → atomic chains (Unichain) →
facilitator. The article frames `upfront` as x402's own flow, beside atomic, never instead. The scarce resource is attention: one front at a time.

## Watch

- **BuilderNet:** a May 2026 report of a bundle transaction included reverting despite default
  semantics (cause unknown). The mainnet guarantee rests on builders dropping reverts.
- **EIP-8037:** ACDE 2026-10-22 (from a transcript summary): the Hoodi migration is cancelled;
  SELFDESTRUCT and our thread weren't mentioned. ChainSecurity's exploit (state gas returned
  when a child frame reverts) puts refunds and the reserve first in line if 8037 is revisited
  (Ansgar). A same-transaction SELFDESTRUCT refill will be judged on whether gas left rises
  mid-execution: applied at transaction end, it doesn't. Sepolia, on Amsterdam's rules, stops on
  an unrecognised schedule until it's added. Our post: Magicians #51.
- **docs.gelato.cloud's certificate:** expired 2026-10-09; reported by ticket (2026-10-23).

## Decided

- 2026-10-23: no mainnet evidence run (`ethereum-verify`); Sepolia's testing stands, mainnet
  settlements accrue with use.
- 2026-10-23: x402-async first. Bruno settles its own payments on the call's chain; facilitators
  optional and pluggable, for listings only; same-chain only, cross-chain gas deferred to "teh borg"
  (the author's cross-chain gas project) once it's deployed.

## Done

- 2026-10-22: v0.3.1 (RPC nodes shuffled, `config.local.json`, `make rpc-check`): released,
  image published, deployed.
