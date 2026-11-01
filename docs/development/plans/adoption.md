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
users new to blockchains could use their app (link to be found). Most bundlers are behind an API key. Bruno has no
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
- **x402 listings:** [x402scan](https://www.x402scan.com), which lists any URL answering with a
  valid `402`. The x402.org ecosystem page is gone
  ([x402#2466](https://github.com/x402-foundation/x402/pull/2466), closed 2026-05-26), and
  Bazaar and agentic.market list only services settled through a facilitator that catalogs them
  ([CDP: get discovered](https://docs.cdp.coinbase.com/x402/seller/get-discovered)); CDP's
  doesn't serve Ethereum or Sepolia
  ([network support](https://docs.cdp.coinbase.com/x402/network-support)). That's a reason for
  the [facilitator](facilitator.md) as well as a technical one.
- **MCP registries**, once `/mcp` exists ([MCP](MCP.md#discovery)).
- **Docs:** the lead example's recipe, and a page on what a transaction costs, for developers
  and LLMs.

Each channel gets its own URL, so it's measurable which ones bring traffic. Whether both relays
accept an unknown query parameter for this is unchecked.

## Targets

Researched 2026-11-01 from public pages. Each target lists the change it needs, its merge bar,
and whether it's active.

### Ranked

1. **BundleBear's bundler labels.** A PR to [Jam516/BundleBear](https://github.com/Jam516/BundleBear)
   adding the relay's sending accounts to
   [`erc4337_labels_bundlers.sql`](https://github.com/Jam516/BundleBear/blob/main/models/erc4337/labels/erc4337_labels_bundlers.sql)
   names Bruno on [bundlebear.com](https://www.bundlebear.com/overview/all) and the Dune
   dashboards reusing its labels.
   [ethereum.org's account abstraction page](https://ethereum.org/roadmap/account-abstraction/)
   links BundleBear. Active (last commit 2026-10-29); recent merges are the maintainer's own.
2. **docs.erc4337.io.** Its [user operations quick-start](https://docs.erc4337.io/userops/quick-start)
   already features Etherspot's [free bundler](https://github.com/etherspot/free-bundler)
   (`@etherspot/free-bundler`), a public endpoint with no key, so Bruno is a peer there. A PR to
   [eth-infinitism/aa-mkdocs](https://github.com/eth-infinitism/aa-mkdocs). Outside PRs are
   merged, rarely (last 2026-02-10).
3. **ethereum.org's developer tools**, [ethereum.org/developers/tools](https://ethereum.org/developers/tools/),
   category "Account abstraction & tx orchestration": an entry through the "Suggest a resource"
   form in [ethereum/builder-resources](https://github.com/ethereum/builder-resources/issues/new/choose)
   (or a PR to its `catalog/resources.json`). Active (last push 2026-10-27).
4. **x402scan** ([x402scan.com](https://www.x402scan.com), Merit Systems): add the URL, and
   it's probed and listed if it answers with a `402` it can parse (`accepts`, an input schema,
   `/openapi.json` or `/.well-known/x402` preferred), per its
   [discovery doc](https://github.com/Merit-Systems/x402scan/blob/main/docs/DISCOVERY.md). The
   doc names no networks, so whether it takes `eip155:1` and Sepolia is unchecked.
5. **thirdweb's SDK** ([thirdweb-dev/js](https://github.com/thirdweb-dev/js), Apache-2.0,
   active): `smartWallet({ overrides: { bundlerUrl } })`
   ([SmartWalletOptions](https://portal.thirdweb.com/references/typescript/v5/SmartWalletOptions),
   [own bundler](https://portal.thirdweb.com/connect/account-abstraction/infrastructure)) is
   documented and maintained, so an example is enough. Its default account is untested here.
6. **Ambire's extension** ([AmbireTech/extension](https://github.com/AmbireTech/extension),
   GPL-3.0, active): its shared code takes a custom bundler URL per network, ahead of its
   built-in ones ([`customBundler.ts`](https://github.com/AmbireTech/ambire-common/blob/v2/src/services/bundlers/customBundler.ts)).
   Whether its interface shows the setting, and whether its accounts (its own, untested here)
   pay a bundler without its paymaster, is unchecked.
7. **MetaMask's Smart Accounts Kit:** its
   [EIP-7702 quickstart](https://docs.metamask.io/smart-accounts-kit/get-started/smart-account-quickstart/eip7702/)
   uses viem's bundler client with a placeholder URL, and its Stateless7702 account is tested
   here. A tutorial on our side, or an example line on theirs.
8. **awesome-account-abstraction**
   ([4337Mafia/awesome-account-abstraction](https://github.com/4337Mafia/awesome-account-abstraction)),
   its bundlers section: implementations only so far, 26 open PRs, last merge 2026-06-09.

### Others

- **Plain bundler URLs, untested accounts:** Candide's
  [AbstractionKit](https://github.com/candidelabs/abstractionkit) (its own mainnet endpoint now
  needs a key), Safe's `Safe4337Pack`
  ([guide](https://docs.safe.global/sdk/relay-kit/guides/4337-safe-sdk), which hard-wires
  Pimlico with a key) and Etherspot's `GenericBundler`
  ([etherspot-modular-sdk](https://github.com/etherspot/etherspot-modular-sdk)). Candide and Safe
  both mean Safe with the 4337 module (v0.3.0, EntryPoint v0.7), untested here.
- **ZeroDev's docs**
  ([choose an infra provider](https://docs.zerodev.app/api-and-toolings/infrastructure/choose-an-infra-provider))
  call bundlers "perfectly interoperable" but have guides for ZeroDev and Pimlico only. Nothing
  new to ZeroDev until zerodevapp/sdk#282 and #283 are reviewed.
- **viem's docs** ([account abstraction](https://viem.sh/account-abstraction)) name Pimlico and
  Biconomy as bundlers; a vendor listing there is doubtful.
- **erc4337.io's compatibility results** ([bundlers.erc4337.io](https://bundlers.erc4337.io),
  from [eth-infinitism/bundler-test-executor](https://github.com/eth-infinitism/bundler-test-executor)):
  the only list of conforming bundlers, but its suite drives `debug_bundler_*` methods, so it's
  code, and the repository has been dormant since 2025-05.
- **The shared mempool** is live on Ethereum, Arbitrum and Optimism (Etherspot, Candide,
  Silius, Alchemy; source to be found). Joining it is a libp2p implementation, not a listing.
- **x402 lists:** [x402.eco](https://www.x402.eco) takes a PR with a JSON entry
  ([rawgroundbeef/x402eco](https://github.com/rawgroundbeef/x402eco)); activity unchecked.
  [xpaysh/awesome-x402](https://github.com/xpaysh/awesome-x402) closes outside PRs unmerged.
  [Pay.sh](https://pay.sh) is Solana's.
- **Wallets without a setting:** [Rabby](https://github.com/RabbyHub/Rabby) (its gas account is
  its own service), Rainbow (its own delegate). Nothing found for Zerion, Frame, Taho, Enkrypt
  or Brave. [Porto](https://github.com/ithacaxyz/porto) and
  [Daimo](https://github.com/daimo-eth/daimo) are archived.

### Apps whose users hold no ETH

- **Sablier:** a withdrawal costs about $1 in ETH on top of gas, an airdrop claim more
  ([fees](https://docs.sablier.com/concepts/fees)). Its open agent skill
  ([sablier-labs/sablier-skills](https://github.com/sablier-labs/sablier-skills),
  [guide](https://docs.sablier.com/guides/claim-with-ai-skills)) could take a path without
  ETH. A `call` here has value 0, so the fee needs a batched op that gets ETH within it (WETH,
  unwrapped).
- **Giveth** ([Giveth/giveth-dapps-v2](https://github.com/Giveth/giveth-dapps-v2)): donors pay
  gas. USDC donations through Bruno would be code on their side.

### Pricing, for the cost page

- Pimlico ([pricing](https://pimlico.io/pricing)): mainnet sponsorship is gas plus 10%;
  pay-as-you-go needs a card on file, with no monthly minimum.
- Candide's and Etherspot's endpoints take an API key (Candide's
  [AbstractionKit](https://github.com/candidelabs/abstractionkit) examples, Etherspot's
  [modular SDK](https://github.com/etherspot/etherspot-modular-sdk)).
- Biconomy shut its bundler down on 2026-02-28 and offered only a one-off upgrade script
  ([Biconomy's post](https://blog.biconomy.io/upgrade-path-from-biconomy-v2-to-nexus-post-feb-28-26)).
- Developers' own complaints weren't found by search; they need direct searches on X, Reddit
  and Farcaster.

### Grants

- **Gitcoin:** GG24's Developer Tooling & Infrastructure domain was the best fit; GG25 isn't
  scheduled yet. Watch [gov.gitcoin.co](https://gov.gitcoin.co).
- **EF ESP:** no open [wishlist](https://esp.ethereum.foundation/applicants/wishlist) or RFP since
  the [2025 pause](https://blog.ethereum.org/2025/08/29/esp-next-chapter);
  [Office Hours](https://esp.ethereum.foundation/applicants/office-hours) only.
- **Giveth's GIVbacks:** verification as a public good, reviewed within about two weeks, small
  amounts ([verification process](https://giveth.notion.site/Project-Verification-Process-a840737294c54ee2b9e5f73e7c83fac3)).
- **Octant** ([octant.app](https://octant.app)): epochs; the current window is unchecked.
- Not a fit: Optimism's Retro Funding
  ([Season 8](https://gov.optimism.io/t/budget-board-proposal-for-retro-funding-for-season-8/10097),
  measured by Superchain use), Protocol Guild, Flashbots, Arbitrum's and Base's grants.

### Open

- [ ] Safe with the 4337 module (v0.3.0, EntryPoint v0.7): testing it opens Candide and Safe's
  relay-kit.
- [ ] Ambire: whether the custom bundler is in its interface, and whether its accounts pay
  without its paymaster.
- [ ] Whether x402scan lists services on Ethereum and Sepolia.
