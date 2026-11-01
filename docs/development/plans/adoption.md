---
description: "Plan: how Bruno reaches users, through developers paying as they go and through agents; the lead example; the channels, and the research still to do for concrete targets"
---

# Plan: adoption

How Bruno reaches the people who need a transaction sent but hold no ETH, written 2026-11-01
for the early beta's soft launch and the slow burn after it. The rule from
[priorities](priorities.md) holds throughout: an integration is a URL, with nothing for us to
maintain upstream. Pull requests elsewhere are listings and docs, not code.

## Two audiences

Users are reached through two routes, with the draft ERC underneath both.

**Developers, paying as they go.** Embedded-wallet and account-abstraction vendors bill early
teams like enterprises: one team reported $500 a month to Alchemy out of a $3,500 spend, so that
users new to blockchains could use their app. Most bundlers are behind an API key. Bruno has no
key, plan, deposit or minimum, and a transaction is paid for only when it lands. The developer
integrates for free; whoever's account pays the fee pays per transaction.

- **What Bruno replaces:** the bundler and the paymaster, with gas sponsorship. It doesn't
  replace login and key management (Privy, Turnkey, Web3Auth and the like), so the pitch says
  so.
- **Users holding a token:** the fee comes from the token, through the bundler URL or the REST
  and x402 interfaces.
- **Users holding nothing:** a signed transfer's payer is separate from the call, so over REST
  and x402 the developer's own account can pay for a user's call, per transaction. A user op
  paying by its calls pays from its own account, so this doesn't apply to user ops.

**Agents, through MCP and x402.** An agent is its own developer: no app and no integration stand
between it and Bruno. This is the shorter route, and why [MCP](MCP.md) is the bigger lever.
Agent frameworks are reached through MCP, not an integration each. The exception is a framework
that builds its own wallet and sends imperatively (AgentKit, GOAT, ElizaOS's EVM plugin): there,
a viem wallet client sending through Bruno is the one artefact serving all of them, as well as
developers using viem directly ([AgentKit's actions](agentkit.md)).

**The public good:** an open specification heading for an ERC, a relay under the AGPL with two
implementations, no keys. It gives both routes their credibility, and it's what grant rounds
fund.

The claim common to both routes: where builders drop reverting transactions (Ethereum), a call
that would revert costs nothing; on chains without bundles that drop reverts, the call is paid
for upfront over x402. The difference is the chain's.

## The lead example: no gas, any token

A user has no ETH and holds a token with no EIP-3009 and no permit, such as USDT. Every swap
venue and most apps need an approval first, and the approval needs gas. With EIP-7702 the
delegation is a signature, so the first transaction can be one call from the user's account:

1. a Uniswap swap of part of the user's token into one Bruno accepts, enough for the fee;
2. the app's own calls: approvals (USDT's set to zero first), swaps through whichever router or
   venue the app uses, transfers, deposits;
3. Bruno's fee, in the accepted token.

Bruno only needs a route from the user's token to one it accepts. The app's calls go into the
second step unchanged, whatever the app is. The recipe is tested on anvil against the vendored
Uniswap; the apps named in it are examples, not tested.

## Channels

- **Bundler lists:** the JSON-RPC bundler interface is live, so these are a launch item.
- **viem:** the wallet client above, and the docs' recipes.
- **Open-source wallets:** any wallet where a permissionless relay or bundler can be used.
- **x402 listings:** the x402.org ecosystem page and Coinbase's agentic.market. Bazaar listings
  come only through a facilitator, which is a reason for the [facilitator](facilitator.md) as
  well as a technical one.
- **MCP registries**, once `/mcp` exists ([MCP](MCP.md#discovery)).
- **Docs:** the lead example's recipe, and a page on what a transaction costs, for developers
  and LLMs.

Each channel gets its own URL, so it's measurable which ones bring traffic. Whether both relays
accept an unknown query parameter for this is unchecked.

## Research for concrete targets

Each target found is recorded with what the change would be (a listing, a docs line, a setting),
its merge bar, and whether the project is active.

- [ ] **Open-source wallets** that let the user or the app set the bundler, the RPC or the
  relay, or that support EIP-7702 delegation to an account Bruno serves.
- [ ] **Bundler lists:** where developers look for a bundler (erc4337.io, SDK docs listing
  bundlers, awesome-lists, chain docs), and what a listing needs.
- [ ] **Teams paying for sponsorship:** public complaints about bundler and embedded-wallet
  pricing, and the open-source apps sponsoring gas today, as the people the cost page is for.
- [ ] **Apps with a "no gas" problem:** open-source apps on Ethereum whose users hold tokens but
  not ETH (payments, payroll, airdrop claims, exchange withdrawals in USDC or USDT).
- [ ] **x402 listings** beyond x402.org and agentic.market: directories and lists of x402
  services that don't require a facilitator.
- [ ] **Grant rounds** for public goods that fund infrastructure like this.
