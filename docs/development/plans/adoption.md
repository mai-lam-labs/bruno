---
description: "Plan: how Bruno reaches users, through developers paying as they go and through agents; the lead example; the channels, and the research still to do for concrete targets"
---

# Plan: adoption

How Bruno reaches the people who need a transaction sent but hold no ETH, written 2026-11-01
for the early beta's soft launch and the slow burn after it. Start on Ethereum with projects
that can add Bruno as another route. Prefer a URL and a tested recipe. An SDK or wallet change
earns its upkeep when it opens a flow developers cannot reach through configuration.
The order of work is in [priorities](priorities.md).

## Two audiences

Users are reached through two routes, with the draft ERC underneath both.

**Developers, paying as they go.** Vertically integrated stacks such as Alchemy's (embedded
wallets, RPC, bundler and paymaster from one vendor) are expensive: usage is billed in compute
units, gas sponsorship carries an 8% admin fee on prepaid funds, and anything larger goes
through sales ([Alchemy's pricing](https://www.alchemy.com/pricing)). Teams choose them for the
big name, and because they don't want to run infrastructure of their own. Bruno needs no
infrastructure either, and with an LLM coding agent, adding it as an option is a small piece of
work. Most bundlers are behind an API key. Bruno has no key, plan, deposit or minimum, and a
transaction is paid for only when it lands. The developer integrates for free; whoever's account
pays the fee pays per transaction.

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
developers using viem directly. An integration with one framework's toolkit is a project of
its own, not part of this one.

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
second step unchanged, whatever the app is. The recipe isn't tested yet: it's to be tested on
anvil against the vendored Uniswap, with its docs page. The apps named in it are examples, not
tested.

## Channels

- **Bundler lists:** the JSON-RPC bundler interface is live, so these are a launch item.
- **viem:** the wallet client above, and the docs' recipes.
- **Open-source wallets:** any wallet where a permissionless relay or bundler can be used.
- **x402 discovery:** list the existing paid relay as a service first. List the hosted
  facilitator once a merchant has settled a payment on Ethereum. The [listing steps](#x402-listings)
  cover the two audiences and the evidence each directory needs.
- **MCP registries**, once `/mcp` exists ([MCP](MCP.md#discovery)).
- **Docs:** the lead example's recipe, and a page on what a transaction costs, for developers
  and LLMs.

Each channel gets its own URL, so it's measurable which ones bring traffic. Whether both relays
accept an unknown query parameter for this is unchecked.

## Targets

Researched 2026-11-01 from public pages. Each target lists the change it needs, its merge bar,
and whether it's active.

### Ranked

The Ethereum integration pilots below take priority over the general listings in this list.

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
4. **x402scan** ([x402scan.com](https://www.x402scan.com), Merit Systems): revisit when its
   resource scanner supports Ethereum mainnet. See [x402 listings](#x402-listings).
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

### x402 listings

Checked 2026-11-27. Lead with Ethereum mainnet and a real request and receipt.

1. **Bruno as a paid service.** Verify an unpaid production `POST /ethereum/v1/x402` with a
   complete example from the deployed release. Check that the v2 `PAYMENT-REQUIRED` header
   advertises Ethereum USDC on `eip155:1`. Submit the route and the same JSON example to
   [x402 List](https://x402-list.com/submit), which probes POST endpoints. Describe the service
   as sending an Ethereum transaction with gas paid in USDC. The hosted facilitator can
   ship independently. The production POST still needs checking.
2. **Bruno as a facilitator.** Once a merchant settles through it, have [PipRail](https://docs.piprail.com/accepting-payments/facilitator-coverage/)
   exercise the configured URL and propose a recipe or coverage entry. For [x402 List's
   facilitator listing](https://x402-list.com/submit), give the submitting addresses and the
   receipt. Ethereum is an “Other networks” entry on its form, so check manual review and
   whether they can measure L1 settlements before counting on an activity badge.
3. **Follow-up.** [x402scan's resource scanner](https://github.com/Merit-Systems/x402scan/blob/main/apps/scan/src/types/chain.ts#L263)
   enables Base and Solana, and [x402.watch's facilitator registry](https://github.com/Swader/x402facilitators#network)
   omits Ethereum L1. Track their L1 support. Coinbase's [Bazaar](https://docs.cdp.coinbase.com/api-reference/v2/rest-api/x402-facilitator/browse-the-catalog)
   lists resources discovered by its own facilitator. Bruno can publish its own merchant
   discovery metadata as demand from directories warrants.

### Ethereum integration pilots

These are prospects checked against their public interfaces. Each starts with one useful
flow that can be shown in a recipe or an article. Start conversations before the hosted
facilitator ships. Look for a merchant with a real mainnet use case and an SDK, wallet or
app team willing to try the existing relay. Use the pilots to test setup, settlement cost,
timeouts and retry behavior. Use the working recipes and receipts for listings and articles.

- **PipRail:** settle one `exact` payment on Ethereum using its configurable facilitator URL.
  Ask for a recipe or coverage entry after it works. Its docs already show another Ethereum
  facilitator, so Bruno is an additional route. [Facilitator coverage](https://docs.piprail.com/accepting-payments/facilitator-coverage/)
- **Automaton Onchain Tools:** offer Ethereum USDC alongside its existing Base option for its
  $1.50 token deep scan. Check the settlement quote against the merchant's margin first.
  [Project and pricing](https://github.com/povrouyre-wq/automaton-onchain-tools)
- **Openfort:** use a backend wallet's typed-data signer to pay for an Ethereum call from USDC
  with zero ETH. A companion example to its x402 guide is the first ask.
  [Openfort x402 guide](https://www.openfort.io/blog/x402-agentic-payments)
- **LlamaPay:** withdraw a USDC stream into a recipient holding no ETH or USDC, then pay the
  relay from the proceeds. The withdrawal can be called on the recipient's behalf. Start with
  a fork-tested recipe before proposing a UI route.
  [Withdrawal implementation](https://github.com/LlamaPay/llamapay/blob/master/contracts/LlamaPay.sol#L975-L985)
- **Elixir x402 SDK:** give its Phoenix example an Ethereum USDC route with Bruno as the
  configured facilitator. [Phoenix quickstart](https://github.com/cardotrejos/x402#phoenix-quick-start)
- **MetaMask Embedded Wallets:** try its documented Ethereum bundler override and verify the
  supported fee modes. This is a separate SDK surface from the existing Smart Accounts Kit
  recipe. [Embedded wallet configuration](https://docs.metamask.io/embedded-wallets/sdk/react/advanced/smart-accounts/)

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
- Alchemy ([pricing](https://www.alchemy.com/pricing)): compute units at $0.52 per million on
  pay-as-you-go; gas sponsorship with an 8% admin fee on prepaid funds, mainnet only on paid
  plans; enterprise terms through sales.

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

- [ ] toolvet.app
- [ ] Safe with the 4337 module (v0.3.0, EntryPoint v0.7): testing it opens Candide and Safe's
  relay-kit.
- [ ] Ambire: whether the custom bundler is in its interface, and whether its accounts pay
  without its paymaster.
- [ ] Recheck Ethereum discovery support when x402scan or x402.watch adds it.
