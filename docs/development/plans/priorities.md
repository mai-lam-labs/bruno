---
description: "Bruno's released capabilities, the 0.7.0 work in progress, Ethereum adoption priorities, and a proposed 1.0 boundary"
---

# Priorities

Last reviewed 2026-11-27. The [support matrix](../../support.md) records the testing
evidence. The working note on the 0.7.0 branch records what is being built and what remains
to be verified for that release.

## Released through 0.6.1

- **Calls and x402.** REST and JSON-RPC submissions can pay from calls or by signed EIP-3009
  transfer, including ERC-1271 and ERC-6492 payers. Bruno also sells its own Ethereum relay
  service over x402 v2 `exact` with USDC. Its public endpoint is documented and tested on
  Sepolia. The support matrix records that `make ethereum-verify` has yet to be run.
- **ERC-4337.** Bundling for EntryPoint v0.7 and v0.8, including EIP-7702 accounts, fees paid
  in ETH, token-paid calls, and compatible external paymasters. The released docs have
  runnable viem, permissionless.js, ZeroDev, MetaMask and Alchemy recipes.
  [Bundler guide](../../using/user-operations/index.md)
- **0.6.0.** Gas estimates, quote breakdowns, typed data for signed fees, and refusals that
  tell a developer what to do next. Ops paying by their fees can go directly to EntryPoint.
- **0.6.1.** Direct settlement for allowlisted calls paid by signed transfer, and sponsorship
  for an account's own signed transactions. Both relays and the TypeScript client support
  sponsorship, with [viem, ethers, Python and EIP-7702 examples](../../using/sponsorship/index.md).
- **Distribution.** The TypeScript client is published on npm and JSR as a `-dev`
  prerelease. The docs build emits per-page Markdown, `llms.txt` and
  `llms-full.txt`, alongside the HTML.

The [ERC-6492 plan](erc6492.md) records the design behind shipped signed payments. The
[sponsorship plan](sponsorship.md) holds the remaining limitations, and [parity](parity.md)
documents how to review work across the relays.

## Direction

Focus on Ethereum mainnet, with Sepolia for live rehearsal. Bruno sends through builders
using `eth_sendBundle`. Developers can add Bruno alongside their current wallets, SDKs and
payment providers. Hosted x402 settlement on Ethereum mainnet is part of 1.0 and gives
merchants another route on a chain many facilitator integrations omit. Ordered calls,
sponsorship, bundling and hosted settlement use the shared relay core. Put most engineering
effort into that core, and use early integrations to find where it needs work.
[Adoption plan](adoption.md)

## In progress for 0.7.0

1. **Ordered calls.** Support `calls[]` across quotes, submissions, sponsorship and Bruno's
   paid x402 route. Finish parity across both relays, schemas, clients, examples and pending
   Worker rows from the prior release. Verify the published request shape against the deployed
   relay before using it in a listing.
2. **Ethereum x402 facilitator.** Build the hosted flow on ordered calls. The merchant gets a
   route and a derived receipt address. The buyer signs ordinary USDC `exact` and Bruno takes
   a gas-sensitive fee from the proceeds. Provide the merchant-run adapter example as well.
   [Current design and outstanding decisions](facilitator.md#hosted-facilitator-on-ethereum)
3. **LLM discovery.** The branch has changes for homepage links, a task-first `llms.txt`
   index, and rebased links in `llms-full.txt`. Verify those after publishing. Use one
   release-matched Ethereum x402 example in the guide, discovery metadata and verification
   run. A coding agent given the homepage should be able to find the example, request an
   unpaid quote, and identify its chain, token and amount.

## Adoption to do next

- **List the paid relay.** Start with x402 List after confirming the documented POST in
  production. This work can proceed on the current release using its request format.
- **Find early integration partners now.** Start with one merchant who can pilot a useful
  Ethereum paid route and one SDK or wallet team that can exercise the relay core. Automaton
  Onchain Tools is the first merchant lead. Start a PipRail conversation around the adapter
  and confirm the integration with a real merchant receipt once hosted settlement is ready.
  Openfort or LlamaPay can exercise a transaction paid from USDC. Use the pilots to learn
  where fees, timeouts, retries and onboarding get in the way.
- **Publish examples and articles around working flows.** The x402 articles are in the blog
  repository. Link readers from an article to the matching recipe and from a listing to
  the same release's docs. The rest of the candidate list is in
  [adoption](adoption.md#ethereum-integration-pilots).

## 0.8 and 0.9: harden the core

Suggested split, to adjust as the pilots and release work expose failures:

- **0.8, recovery and correctness.** Test what happens when a node is stale or unavailable,
  a process restarts after accepting or broadcasting work, a request is retried, or a bundle
  is partially included or reorged. Reconcile status against receipts and make sure retries
  do not create a second payment or payout. Investigate the stale-node burn-in failures and
  verify sponsorship quote limits against the sequence the relay will submit.
- **0.9, production operation.** Exercise those cases on the supported deployments and
  rehearse an upgrade and recovery with pending work. Settle treasury key and reserve
  handling, hosted receipt-key continuity, and forwarding funds paid outside a bundle.
  Use merchant and relay pilots to tune the timeout, pricing and retry behavior that
  developers see. Verify the mainnet flows with the published clients and examples.

## When an integration calls for it

- **Account integrations.** Safe by one signature, Light Account, and EntryPoint v0.9 remain
  candidates. Pick one when a real integration needs it.
  [Safe](safe.md) · [Wallets](smart-wallets.md) · [EntryPoint v0.9](erc4337.md#entrypoint-v09-not-built-2026-11-25)
- **Agent tools.** Framework adapters, permission delegation and more agent payment
  standards are exploratory, and an adapter for one framework's toolkit is a project of its
  own. MCP is a separate project that can use the same public interface.
  [MCP](MCP.md) · [Delegated permissions](delegations.md) · [Agent standards](agent-standards.md)
- **More chains and payment schemes.** Revisit when a partner needs them and the chain's
  submission infrastructure supports the guarantees for that flow. The Ethereum facilitator
  uses the existing bundle path. Upfront settlement on other chains is a separate proposal.
  [More chains](morechains.md) · [Upfront x402](x402-async.md)
- **Release tooling and upstream.** The working note tracks duplicate CI runs on the same
  commit. [Release procedure](../releasing.md) · [Upstream issues](../upstream.md)

## 1.0 target

Bruno 1.0 is a dependable Ethereum mainnet relay and hosted x402 facilitator, backed by the
same tested core. Early partners exercise both offers before the interfaces are frozen.

- **Stable Ethereum interfaces.** Settle the public REST, JSON-RPC, x402 and TypeScript
  contracts. Document the transition to
  `calls[]`, and what callers can rely on for pricing, timeout, retry and settlement.
- **Production evidence.** Verify on Ethereum with the published clients and docs. Cover
  the paid relay, sponsorship, a supported bundler path and hosted x402 settlement with an
  early merchant. Show how to recover interrupted work and reconcile the result of a retry.
- **Accurate recipes.** Starting from the homepage, a developer or coding agent can get an
  unpaid quote and find a runnable example for each supported route. Publish the support
  matrix for the environments we actually verify. Test browsers and Workers before marking
  them supported on JSR.
- **Stable packages.** Publish the client without `-dev` once its interface and support
  claims match the release.

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
  give the facilitator a fee in x402 itself ([facilitator research](facilitator.md#fees-within-exact-the-options)).

- **EIP-8037:** All Core Devs (2026-10-22) postponed Hoodi's move to Amsterdam for further
  consideration. [Our reply](https://ethereum-magicians.org/t/eip-8037-state-creation-gas-cost-increase/25694/51?u=harryr)
  explains the SELFDESTRUCT refund problem.
- **Unichain bundles:** Flashbots referred
  [our questions](https://collective.flashbots.net/t/bundles-are-coming-to-unichain/5072/2?u=mailam)
  to the OP team (2026-10-27). Since Unichain's mainnet moved to OP Enterprise, run by
  Optimism, on 2026-10-29, its public endpoint refuses `eth_sendBundle`, as its testnet's has
  since its own move. Whether bundles come back is unknown; no OP Enterprise contact found.
