---
description: "Plan, to become its own project: an MCP server for agents that uses Bruno behind the scenes; the direction, the AX trials, and the 2026-10-23 design under review"
---

# Plan: MCP, a project of its own

**MCP spins out into its own project (2026-11-03), to ponder.** It would use Bruno behind the
scenes, as any client does, and be the opinionated product layer Bruno's specification
shouldn't be: intents, wallet composition, typed-data flows, skills, x402 tools, iterated as a
product. Bruno's own public interface is the TypeScript client's declarations, OpenAPI and
OpenRPC, and x402 ([priorities](priorities.md#next)). This page is the new project's seed:
the direction, the AX trials, and the earlier design below them.

Bruno as a stateless MCP server, for agents. The primary goal is composability with the rest
of the MCP ecosystem: an agent's wallet MCP holds the keys and signs, builder MCPs produce
calldata, and Bruno sends the result. Bruno never handles key material. Plain MCP, no x402:
every transaction pays Bruno as it runs, so there's nothing to charge for at the door, and
the barrier to entry is an MCP client.

Normative text, fetched to `scratchpad/`: MCP `2026-07-28` (`scratchpad/mcpspec/`,
modelcontextprotocol/modelcontextprotocol at `3f6e5e1`): `basic/index`, `basic/versioning`,
`basic/transports/streamable-http`, `basic/patterns/cancellation`, `server/{discover,tools}`,
`changelog`; and `2025-11-25` Streamable HTTP (`legacy_transports.mdx`) for legacy clients.

## Direction (2026-11-03)

The research on [agent wallets](agent-wallets.md) changed the plan's premises; the decisions
below it are kept for reference and under review. What's changed:

- **Models write calldata well**, with an inline snippet and a description, so Bruno needs no
  helper encoders: the `erc20` and `uniswap` groups go. What a call does is the model's and the
  wallet's to show the user; that a call which would revert never lands, at no cost, is Bruno's.
- **Every wallet signs EIP-712, and almost none hands back a transaction to send.** The agent
  wallets reached as MCP servers or CLIs (Coinbase's, Phantom's, MetaMask's, OKX's, Circle's)
  each send only through their own pipeline, but sign arbitrary typed data; only the
  infrastructure wallets (Privy, Turnkey, Dynamic, Openfort) sign a transaction without sending
  it, or an EIP-7702 authorization. So Calibur, which needs the account delegated first, reaches
  only those, and the consumer wallets need another route.
- **A server, not only a skill.** A skill or Wallet MCP plugin can't POST from Claude's or
  ChatGPT's chat apps; a connected MCP server's tool can.

### The narrow first tools

`quote`, `submit` and `status`, as the relay has them, and nothing else until they're used.
Each tool's description carries the contract: what the call's `msg.sender` is, how it pays, what
simulation guarantees.

### Three routes, by what the wallet signs

1. **The user's own Safe, for every wallet that signs typed data:** one EIP-712 signature of a
   batch; Bruno deploys the Safe if it isn't yet, runs the batch and is paid. Now Bruno's own
   feature: [a Safe by one signature](safe.md).
2. **A signed transaction, for the infrastructure wallets and agents' own keys:**
   [atomic sponsorship](sponsorship.md), the account funded and the transaction sent in one
   atomic bundle that also proves Bruno was paid.
3. **A signed EIP-3009 transfer, the call run by the runner:** as REST takes it now
   (`payment.transfer`); for calls that name a beneficiary rather than rely on `msg.sender`.
   And x402: the wallets' own x402 tools pay Bruno's `/x402` already, with nothing of ours.

### Agent experience

From the wallets ([agent wallets](agent-wallets.md#agent-experience-as-the-wallets-shape-it)):
a refusal returned as tool content the model can act on, not a protocol error; a pending
approval the model waits on and polls once; the integrator choosing which tools are listed.

## AX trials (2026-11-03)

Whether a small model, given one sentence and nothing but its tools' descriptions (no skill,
no instructions in a system prompt), gets something done through Bruno. Run against the relay
on anvil, graded onchain. The task: "Send 5 USDC from my wallet to 0x…", from a wallet holding
20 USDC and no ETH. Models: Claude Haiku 5.5 and Claude Sonnet 5.5.

Two routes were tried:

- **MCP-style tools:** prototype `bruno_quote`, `bruno_submit` and `bruno_status` over the REST
  API; a stand-in wallet like evm-mcp-server's (address, balances, typed-data signing, a send
  that fails for want of ETH); and server-side code execution, whose sandbox has no Ethereum
  libraries (keccak only through `pycryptodomex`). The quote estimates the call's gas itself and
  returns the submission complete but for its signature, and the payment's typed data.
- **Code-first:** a single tool running TypeScript under Node with `viem` and `@mailam/bruno`
  installed, the client's type declarations (about 20 KB) as the only reference, and the
  wallet's key in the environment, as an agent's own key would be.

The call is USDC's `transferWithAuthorization`, which works whoever sends it, so the runner can
make it: the model has the wallet sign two EIP-3009 authorizations, the transfer and Bruno's
fee.

### Results

Each run could be answered once, "Yes, go ahead", if it stopped short of done:

| | Completed | Asked first | Turns | Cost a run |
|---|---|---|---|---|
| Haiku, MCP tools, first version | 3 of 5 | n/a | 3–10 | about $0.02 |
| Haiku, MCP tools, revised | 3 of 3 | 1 | 7–15 | $0.004–0.012 |
| Haiku, code-first | 2 of 3 | 3 | 6–11 | $0.006–0.009 |
| Sonnet, MCP tools, revised | 2 of 2 | 2 | 8–9 | $0.07–0.09 |
| Sonnet, code-first | 2 of 2 | 1 | 5–7 | $0.09 |

The first version ran before prompt caching was on; caching cut a run's cost by about two
thirds.

### What failed, and what fixed it

- **A refusal that says what happened, not what to do.** A model quoted a plain `transfer`
  (which reverts, the runner holding nothing), explained the cause correctly, and gave up. Fixed
  by refusals as guidance: the call is made by a one-off contract; to move the payer's tokens,
  sign an EIP-3009 transfer and quote that instead, with the token's domain.
- **Large objects copied between tools.** A model passed the quote's `submission` back as a JSON
  string, its signature lost inside, and repeated it on an unhelpful error three times. Fixed by
  taking the object or a string, and errors naming exactly what's missing. Not carrying blobs
  between tools at all would remove the step.
- **Guessed facts.** Models signed with USDC's EIP-712 domain from memory: right on anvil and
  mainnet, wrong on Sepolia (`"USDC"`). Fixed by stating the chain's USDC address and domain in
  the quote's description, which a server for one chain can do.
- **Encoding by hand.** Without libraries, models built calldata with `printf` in bash, taking up
  to three tries to align it. Code-first removes this: models used the client's
  `transferOf`, `transferTypedData`, `transferCalldata` and `payAndSubmit` as declared.
- **Code-first's own failures** were the client's, on a chain it doesn't know: no USDC alias and
  no EIP-3009 domain for chain 31337. On Sepolia or mainnet they wouldn't arise.

### What it shows

- **Both routes work, from Haiku up, from one sentence,** once the interface is right. Neither
  is decisively better: code-first suits agents that run code (coding harnesses, frameworks),
  MCP the chat apps whose wallets are MCP servers. Cost is the model's, not the route's: under a
  cent a task on Haiku, 7–9 cents on Sonnet.
- **The interface lessons hold for both:** refusals that say what to do next; forgiving input,
  with errors in the model's terms; facts stated, not left to be guessed; a tool's description
  stating its contract (the `msg.sender` caveat, read and followed).
- **Models stop at the fee.** Every model, on both routes, asked before paying a fee of 30–45%
  on a small transfer, and an unattended agent might decline. The fee was inflated on anvil (the
  tests' fee floor is 1 gwei a gas, 100 times the deployed relays' 0.01 gwei), but on Ethereum
  the gas alone is visible on a small amount: about 200k gas at 1 gwei is about $0.50 with ETH
  at $2,500. How a quote presents its price (what of it is gas, what is Bruno's), and the gas
  Bruno's own part takes, bear on agents' use more than the choice of interface.

## Decisions (2026-10-23, under review)

- **Endpoint and toolsets:** `POST /mcp` serves every tool; `POST /mcp/{groups}` serves only
  the named groups, comma-separated in any combination and order (`/mcp/bruno,calibur,3009`).
  Every listed tool's schema and description go into the model's context, so an integrator
  takes only what its agent uses. Each path is its own MCP server as the spec sees it: one
  endpoint, a fixed `tools/list` (deterministic order: the groups' fixed order, not the
  path's), `ttlMs` long and `cacheScope: "public"`. An unknown group is a 404 with no MCP
  body. A tool called outside the path's groups is `-32602` "Unknown tool", as for any.
- **Groups:**
  - `bruno`: the relay itself: `account`, `quote`, `submit`, `status`.
  - `calibur`: Calibur's signed batch: `calibur_batch`, `calibur_execute`.
  - `3009`: `payment_authorization`.
  - `erc20`: `erc20_approve`, `erc20_transfer`, `weth_unwrap`.
  - `uniswap`: `uniswap_swap_to_eth`, `uniswap_price`.
- **Helpers are encoders.** Most of what an agent needs from Bruno beyond sending is calls
  encoded correctly, which models do badly by hand. Bruno has the encoders already (the
  treasury's swap through SwapRouter02, `approve`, WETH's `withdraw`; the pools' prices; the
  tokens' reads), so they're exposed as they are, pure functions of their arguments and the
  configuration. Every helper returns `calls: [{to, value, data}]`, the shape `calibur_batch`
  takes, so helpers chain into a batch without the model reshaping anything; a builder MCP's
  `transactionRequest` is one more such call.
- **Submit, then poll.** `submit` answers as soon as the relay accepts the submission, with
  its id; `status` gives the stage (waiting, sending, confirming, done, failed) and the
  transaction once included. Nothing is held: no long requests, no SSE, and a disconnect
  loses nothing, the id being the handle MCP's stateless design asks for.
- **Two ways to pay, both in the transaction:**
  - **The call pays:** `payment: {token, amount}`, as REST `submit` has it: the payee gains
    at least the amount during the call (a Calibur batch's payment leg, or anything else).
  - **A signed EIP-3009 transfer:** `payment: {authorization, signature}`: the runner makes
    the `transferWithAuthorization` to the payee and then the call, as x402's settlement does
    inside. An account holding only USDC, with no ETH and no delegate, can then use Bruno
    with any wallet MCP that signs typed data. `submit` takes it already, on REST and JSON-RPC
    (`payment.transfer`, since 0.5.2), so MCP stays a transport of one semantics (SPEC.md:
    `sendTransaction`'s payment).
- **No atomic x402 over MCP.** x402's settlement response requires the transaction's hash
  (even `settlement_pending` must carry a broadcast one), and atomic settlement has none
  before inclusion: a transaction is signed afresh each block. An x402 tool would have to hold
  until mined, which is what this transport avoids. Atomic x402 stays on REST
  (`/{chain}/v1/x402`), held there as the spec requires. `upfront` x402 (`x402-async.md`)
  has its settlement's hash at once, so it can come to MCP as a paid tool once that lands.
- **Both protocol eras on the same endpoints (dual-era).** Modern (`2026-07-28`): no
  handshake, version and capabilities in each request's `_meta`, mirrored in headers. Legacy
  (`2025-06-18`, `2025-11-25`): `initialize` answered with the same capabilities and no
  `Mcp-Session-Id`, which 2025-11-25 allows (a server MAY assign one);
  `notifications/initialized` answered `202`. Most clients in use are still on the handshake:
  the dual-era v2 SDKs shipped this month (python-sdk v2.3.0, typescript-sdk v2.3.1).
- **No state.** Nothing is kept between requests beyond what the relay keeps already (a
  submission, by its id). A prepared batch or authorization is returned whole and taken back
  whole, re-validated.
- **Calibur first, only Calibur.** It's vendored and tested already; its batch is EIP-712
  (any wallet MCP that signs typed data signs it) and, with `executor` zero, anyone may
  execute it, the runner included. Other delegates (MetaMask's delegator, Coinbase's) are
  each a group of their own once vendored and tested, not before.
- **A 7702 authorization is accepted, never prepared for signing:** almost no wallet MCP
  signs one (only Privy's API, of those found). A Calibur flow assumes an account already
  delegated; `account` says whether it is.
- **Chains:** a `chain` argument (an enum of the configured networks' names) on every tool
  that needs one, not a path segment: one server per group set, whatever the chain.

## Tools

| Tool | Group | In → out |
|---|---|---|
| `account` | bruno | chain, address → delegation (the delegate, if any; whether it's Calibur), nonce, ETH and configured token balances |
| `quote` | bruno | chain, call, gas, authorizationList? → the price in each accepted asset, by either payment (as `GET quote`) |
| `submit` | bruno | chain, call, gas, authorizationList?, payment → submission id (as `POST submissions`) |
| `status` | bruno | chain, id → stage, confirmations, transaction (as `GET status`) |
| `calibur_batch` | calibur | chain, account, calls [{to, value, data}], payment leg {token, amount}?, deadline? → `SignedBatchedCall` typed data `{domain, types, primaryType, message}` |
| `calibur_execute` | calibur | chain, the typed data's message, signature → the call `{to: account, data: execute(…)}` for `submit` |
| `payment_authorization` | 3009 | chain, payer, amount, token? → `TransferWithAuthorization` typed data to the payee: a fresh nonce, validBefore from the submission's deadline |
| `erc20_approve` | erc20 | chain, token, spender, amount → calls |
| `erc20_transfer` | erc20 | chain, token, to, amount → calls |
| `weth_unwrap` | erc20 | chain, amount → calls (WETH `withdraw`) |
| `uniswap_swap_to_eth` | uniswap | chain, token (a configured one), amount in, recipient, min out? → calls: `approve` to the router, then its multicall along the token's configured path, unwrapped to ETH. Min out defaults to the pools' price less a stated slippage |
| `uniswap_price` | uniswap | chain, token (a configured one) → its price in ETH from the configured pools, and the block it was read at |

The `uniswap` group covers the configured tokens and their measured paths only: what the
treasury swaps by, known to work. A general swap is a builder MCP's job (LI.FI, Kyber).

Server `instructions` (in `server/discover` and the legacy `initialize`) describe the
composition below, for the path's groups.

A model-driven flow, no keys in Bruno:

1. A builder MCP (LI.FI, a DEX) gives calldata.
2. `calibur_batch` wraps it, with the payment leg, as typed data.
3. The wallet MCP signs it (`sign` typed_data, `evm_sign-typed`, `sign_typed_data`).
4. `calibur_execute` → `submit`; `status` until done.

Or, for an account that isn't delegated, paying in USDC: `quote` → `payment_authorization`
→ the wallet signs → `submit` with the signed transfer → `status`.

A builder's call never goes to Bruno as-is: it would run from the runner, and whatever it
returns would go to the runner. It goes inside the client's own delegate batch (SPEC.md:
the client's contract).

## Protocol requirements (2026-07-28)

- **Request metadata:** `MCP-Protocol-Version`, `Mcp-Method`, and `Mcp-Name` for
  `tools/call`, each checked against the body (`Mcp-Name` Base64-sentinel-decoded first). A
  mismatch or a missing header: `400`, `-32020` HeaderMismatch. A request missing
  `_meta`'s `protocolVersion` or `clientCapabilities`: `400`, `-32602`. An unsupported
  version: `400`, `-32022`, `data.supported` and `data.requested`. An unknown method: `404`,
  `-32601`.
- **`server/discover`** (required): `supportedVersions`, `capabilities: {tools: {}}`,
  `_meta["io.modelcontextprotocol/serverInfo"]`, `instructions`, `ttlMs`, `cacheScope`.
- **Every result** carries `resultType: "complete"` and `serverInfo` in `_meta`.
- **`tools/list`** carries `ttlMs` and `cacheScope`.
- **Transport:** JSON responses only (every tool answers promptly). GET and DELETE: `405`.
  `Mcp-Session-Id` and `Last-Event-ID` ignored, never minted.
- **Results:** each tool declares an `outputSchema`; its result carries `structuredContent`
  conforming to it, and the same JSON as a text block in `content` (SHOULD, for clients that
  pass only `content` to the model).
- **Tool errors** (a refused submission, a bad signature, an unknown id) are results with
  `isError: true` and the reason as text, no `structuredContent`: the spec binds
  `structuredContent` to the declared schema without exempting errors. Malformed requests are
  JSON-RPC errors.
- **`Origin`:** MUST be validated, and a present, invalid one answered `403`. Bruno is
  remote and unauthenticated, so DNS rebinding (the threat the rule addresses) has nothing to
  reach; what counts as invalid is an open question below.
- **Tool names** `[A-Za-z0-9_.-]`, `inputSchema` JSON Schema 2020-12 with
  `additionalProperties: false`; `outputSchema` for each tool's structured result.

## Implementation

Python first, then the Worker, module for module; golden vectors for anything the Worker must
reproduce byte for byte (Calibur's typed data and `execute` calldata, the 3009 typed data,
each helper's calls).

1. **`submit` takes a signed transfer** (REST, JSON-RPC): done in 0.5.2 (`payment.transfer`).
2. **`bruno/calibur.py`** (from `bruno/tests/calibur.py`, which then imports it): the
   typed data and `execute` encoding, as types. Calibur's address per chain in the
   configuration; `make preflight` checks its code on each chain.
3. **`bruno/server/mcp.py`:** JSON-RPC over POST, both eras, the header and `_meta` checks,
   the groups, the tools as calls into `operations`. msgspec Structs in `wire.py` for the MCP
   envelope and each tool's arguments; tool `inputSchema`s and `outputSchema`s generated from
   those Structs (msgspec's JSON Schema), not written by hand.
4. **Helpers:** the tools call the encoders where they live (`router`, `prices`, `tokens`),
   on both sides already (the treasury's). No generic ABI encoding: each helper encodes the
   one contract call it names; arbitrary calls come from builder MCPs.
5. **Worker:** `worker/src/mcp.ts` and `calibur.ts`, the same.
6. **Discovery:** the MCP Registry (`server.json`) once it's deployed.

## Tests

- **Protocol:** the MCP Python SDK v2 drives the modern era (`server/discover`,
  `tools/list`, `tools/call`, version mismatch, header mismatch) and a 1.x client the legacy
  one, against both relays.
- **Composition on anvil:** `calibur_batch` → signed with a local key as a wallet MCP
  would → `calibur_execute` → `submit` → `status` until done; `payment_authorization` →
  signed → `submit` with the transfer → done; the same transfer over REST;
  `uniswap_swap_to_eth` → `calibur_batch` (payment leg in USDC) → done, the account's ETH
  up by at least the min out, against the vendored pools.
- **Groups:** each path's `tools/list` is exactly its groups' tools; a tool outside them is
  unknown.
- **Live:** `check-sepolia` adds one `submit` with a signed transfer through the Worker's
  `/mcp`.

## Docs

- **SPEC.md:** the signed transfer as a payment; the MCP binding as a transport of the
  existing semantics; the tools' contracts.
- **Design:** the decisions above; why no x402 over MCP; why groups.
- **README.md:** an MCP section next to x402's: the endpoint, the groups, the composition
  flow, a client configuration snippet.
- **DEPLOYMENT.md:** Calibur's addresses, the preflight check.

## Ecosystem (research, 2026-10-23)

Superseded in part by [agent wallets](agent-wallets.md) (2026-11-03): the framework toolkits
have stalled, and the wallet vendors' own agent products, their gas and their spending controls,
are surveyed there. What follows is kept until that research replaces it.

Gathered by research passes on the vendors' own docs; each item cites its source in the
session record, and **[unverified]** marks what couldn't be confirmed. Three roles matter:
**signers** (hold keys, sign what Bruno prepares), **builders** (produce the calls), and
**checkers** (data, simulation). Bruno is the **sender**, and there's no other: no gas relay,
paymaster or bundler publishes an MCP (Pimlico, ZeroDev, Biconomy, Gelato, Candide,
Etherspot), except Alchemy's 4337-only paymaster tools and Q402 (below).

### Signers

| Signer | Kind | Keys | Signs typed data | 7702 auth | Ethereum / Sepolia | Fits Bruno by |
|---|---|---|---|---|---|---|
| **Phantom MCP** (`@phantom/mcp-server`, preview) | official MCP | a dedicated agent wallet, remote KMS, policy-checked | `evm_sign-typed {typedData, chainId}` | no | yes / yes | 3009; Calibur if already delegated |
| **Privy Agent CLI** | official CLI, not MCP | remote, ephemeral session keys, browser approval at login | `eth_signTypedData_v4` | `eth_sign7702Authorization` | yes / [unverified] | everything, the only one: 3009, Calibur, delegating |
| **MetaMask Agent Wallet** (`@metamask/agent-wallet`, `mm`) | official CLI + Claude Code plugin, not MCP | self-custodial, guardrails (limits, 2FA) | `mm wallet sign-typed-data` | [unverified] | multi-EVM | 3009; Calibur if delegated. Already paired with Aave's MCP (prepare → `mm` signs) |
| **Coinbase Wallet MCP** (`wallet-mcp.coinbase.com`) | official, hosted | the user's Coinbase Wallet; approval by URL, `get_request_status` polling | `sign` (`typed_data`) | no | yes / **no** (Base Sepolia only) | 3009 only, and see the smart-wallet gotcha |
| **browser-web3-signer** (nikicat; supersedes mcp-wallet-signer) | community, tiny | the user's MetaMask/Rabby via EIP-6963, per-request approval | `{domain, types, primaryType, message}` | no (browsers don't expose it) | yes / yes | the human-in-the-loop demo: the user's own wallet |
| **evm-mcp-server** (mcpdotdirect, 379★) | community | a local private key, no approval step | `sign_typed_data {domainJson, typesJson, primaryType, messageJson}` | no | yes / yes | local and anvil testing |
| Others | | | | | | CDP CLI (server wallets in a TEE; its 7702 call broadcasts its own transaction); OKX Agentic Wallet, Ledger Agent Stack, Fireblocks AI Link, Zerion, Trust (typed data [unverified]); Turnkey, Crossmint, Para, thirdweb, Dynamic, Safe, WalletConnect: no signing MCP found |

**Hero** (hero.io/mcp) is Hero Browser automation for Web3 browsing: no keys, no signing, its
docs stop the agent before any wallet confirmation. No wallet product named Hero turned up
(ethereum.org's agent-tooling list included); WalletChan MCP and Coinbase's are the nearest
names on that list.

Gotchas, for the tools and their descriptions:

1. **Smart-wallet signers can't sign a Calibur batch.** Coinbase's wallet signs as a smart
   wallet (ERC-1271, ERC-6492-wrapped when undeployed). A Calibur batch needs the delegated
   EOA's own key. For 3009, a contract payer is taken since 0.5.0: the runner uses USDC's
   `bytes`-signature overload (FiatToken v2.2) and deploys an undeployed wallet first, from its
   ERC-6492 wrapping.
2. **`EIP712Domain` listed in `types`,** `salt` included: several signers (CDP, ethers'
   low-level path, web3.py, go-ethereum) reportedly refuse typed data without it.
3. **No browser wallet signs a 7702 authorization for a page;** MetaMask warns on smart-account
   upgrades. Delegating is the account owner's own act, in their wallet.
4. **Argument shapes differ** (an object, four JSON strings, undocumented): every tool returns
   the standard `{domain, types, primaryType, message}` and its description says to pass it
   through unchanged.
5. **A domain whose `verifyingContract` is the signer's own account** (Calibur's) drew no
   documented refusals, but no confirmation either: test each signer; expect scanner warnings.
6. **Sepolia** is confirmed only for Phantom, browser-web3-signer and evm-mcp-server.

### Builders

| Builder | Access | Output | Approvals | Notes |
|---|---|---|---|---|
| **LI.FI** (`mcp.li.quest/mcp`) | official, hosted, keyless (200 req / 2 h) | `get-quote` → `transactionRequest {to, data, value, gasLimit, chainId}`; `fromAddress`, `toAddress` | not included: `get-allowance` against `estimate.approvalAddress` (may differ from `to`) | `get-quote-with-calls` appends calls (deposit after a swap) |
| **Aave** (`mcp.aave.com`, `com.aave/mcp`) | official, hosted, v3 + v4 | `prepare_*` → unsigned tx or EIP-712; `preview_action` simulates (health factor) | [unverified] | already paired with MetaMask's agent wallet |
| **Morpho** (`mcp.morpho.org`) | official, hosted, no auth, pre-1.0 | `morpho_prepare_*` → `data.calls[]` + `data.simulation`, `expires_at`; `signature_required` → sign → `continuation` | likely in `calls` [unverified] | a list of calls: a batch already |
| **KyberSwap** | official, stdio, keyless, 18 chains | `swap-build` → `{to, data, value}`; `swap-simulate` | for limit orders; swaps [unverified] | |
| **1inch** | official, API key or OAuth | classic swap → tx; Fusion → EIP-712 order | [unverified] | the key rules it out of a keyless flow |
| Others | | | | Relay (quotes only, links out); Uniswap (no MCP: skills over its Trading API); 0x (docs MCP; skills); Across (skills, MCP [unverified]); Circle (codegen only, no CCTP); CoW, Odos, Lido, ENS, OpenSea: community, mostly key-in-env or read-only |

### Checkers

**Alchemy** (`mcp.alchemy.com/mcp`, OAuth, 168 tools): `simulateExecutionBundle`,
`simulateAssetChanges`, `ethCall`, `ethEstimateGas`, traces. **Tenderly** (`mcp.tenderly.co`,
OAuth): single-transaction simulation with decoded traces and balance diffs (7702 [unverified]).
Blockscout (ABIs, decoded transactions), Etherscan (early access), Dune, The Graph, Nansen,
Moralis, QuickNode: data only. Bruno's own `quote` measures the call against the chain, so a
checker is optional, for an agent wanting decoded asset changes.

### Neighbours

- **Q402** (`@quackai/q402-mcp`): gasless stablecoin payments through its own 7702 relayer,
  12 chains with Ethereum, ~46 tools; an API key, a consent token per payment, $200 caps;
  0 stars. Payments only, not arbitrary calls: the nearest relay over MCP, not a general one.
- **x402 Bazaar MCP** (CDP, unauthenticated): `search_resources`, `proxy_tool_call`; listings
  come from x402's `bazaar` extension. Not Bruno's route while MCP carries no x402.
- **Cloudflare Agents SDK:** `withX402` for paid MCP tools on Workers (stateful `McpAgent`).
- **Agent frameworks:** Coinbase AgentKit (`getMcpTools()`, its own wallet providers), GOAT
  (200+ actions, a key), thirdweb: they bring their own signing; Bruno is a tool they add.
  Reached through MCP, not an integration each; for those that send from their own wallet, a
  viem wallet client sending through Bruno ([adoption](adoption.md)).
- **Prior art for the shape:** Aave's and opcode-fi's MCPs already return unsigned EIP-712 (and
  7702, opcode) for an external wallet: prepare → sign → submit is the ecosystem's pattern.

### Discovery

- **MCP Registry** (preview): `server.json` with a `remotes` URL, `mcp-publisher publish`. A
  domain namespace (`app.mailam/*`) proven over HTTP: the Worker serves
  `/.well-known/mcp-registry-auth`. The registry feeds aggregators, not hosts.
- **Glama** carries most crypto listings (Aave, Blockscout, Tenderly); **Anthropic's Connectors
  Directory** is what Claude users install from (Blockscout points there); ethereum.org's
  agent-tooling list.

## Composition

How the pieces meet, per flow, and what Bruno's tools bridge. Throughout: Bruno prepares,
the signer signs, Bruno sends; no keys cross.

**1. A builder's action, from a delegated account, gas paid from the account (the main flow).**
`account` (delegated to Calibur? balances) → builder (`fromAddress` = the account) →
`calibur_batch` (the builder's calls, approvals, the payment leg) → signer (typed data) →
`calibur_execute` → `submit` → `status`. Signers: Phantom, Privy, MetaMask, browser-web3-signer,
evm-mcp-server. Builders: LI.FI, Kyber, Aave, Morpho.

**1a. The same, with Bruno's helpers as the builder.** For what Bruno encodes itself: an
account holding only USDC gets ETH with no ETH in advance: `uniswap_price` →
`uniswap_swap_to_eth` (recipient the account) → `calibur_batch` (those calls, the payment leg
in USDC) → signer → `calibur_execute` → `submit`. No builder MCP in the loop: Bruno and a
signer are enough.

**2. A USDC-only account, not delegated.** `quote` → `payment_authorization` → signer → `submit`
with the signed transfer → `status`. The call runs from the runner, so it can only be one that
needs no sender: one whose effect doesn't depend on `msg.sender` (a claim to a named
recipient, a permit-based action, a mint for an address). Signers: any that sign typed data
from an EOA, Coinbase's only with the `bytes` overload.

**3. Delegating first.** Only Privy signs the authorization; Bruno takes it in
`authorizationList` with flow 1's batch, one transaction. Otherwise the owner delegates in
their own wallet, once.

**4. Typed-data-only builder outputs** (1inch Fusion, Kyber limit orders, Morpho's
`signature_required`) aren't calls: they go signer → builder, off-chain, and never reach Bruno.

**5. Cross-chain.** A bridge's source leg is a call (flow 1); the destination leg or a
withdrawal's finalization is a separate call on the other chain: flow 1 or 2 again there, paid
from what arrives. This is where "nothing in advance" is the whole point: no ETH needed on the
destination.

What the tools bridge, from the mismatches the research found:

- **`transactionRequest` → a batch call:** `calibur_batch` takes `{to, data, value}`, ignores
  `gasLimit`, `gasPrice`, `from`, and refuses a `chainId` other than the chain's. `value` hex or
  decimal; native value comes from the account.
- **Approvals:** builders leave them out (LI.FI's spender is `approvalAddress`, not `to`).
  `calibur_batch` takes `approvals: [{token, spender, amount}]`, reads each allowance, and
  prepends an `approve` only where it's short.
- **Lists:** Morpho's `calls[]` and multi-step routes are several batch calls, in order.
- **Gas:** a builder's `gasLimit` is per transaction; Bruno measures the whole batch, payment
  leg and approvals included, as `quote` does for any call.
- **The payment leg's amount** is fixed before signing, and the price moves with the base fee:
  `calibur_batch` quotes the batch (with a placeholder signature) and sizes the leg with a
  stated margin; the surplus is a tip, the shortfall a refusal at `submit`, re-prepared.
- **Sender:** builders are given the account as `fromAddress` and recipient, never Bruno: under
  7702 the account is `msg.sender`.

## Brainstorm: beyond the core tools (candidates, not decided)

MCP has more than tools, and the other features cost the model no context until used, which
answers the context-bloat concern as much as the groups do.

- **Tasks extension** (`io.modelcontextprotocol/tasks`): `submit` returns a task to a client
  that declares the extension: `taskId` the submission id (already durable), `pollIntervalMs`
  the slot, `completed` with the transaction, `failed` with the reason. The client polls
  `tasks/get` itself; the model sees the outcome without calling `status`. `status` stays for
  clients without it. Client support is thin today (no entries in the extensions matrix).
- **Prompts** (user-invoked templates, e.g. slash commands): the flows above as named
  workflows: "swap with no ETH", "pay gas in USDC", "claim without ETH", "rescue tokens from a
  compromised account" (flow 1 or 2, sent to builders that drop reverts, out of the sweepers'
  reach). Arguments with **completion** (chains, tokens).
- **Resources** (read on demand): `bruno://{chain}/config` (tokens, payee, Calibur's address),
  `bruno://{chain}/submissions/{id}` as a template, SPEC.md's client contract. Instead of tools
  for things a model reads rarely.
- **Skills extension** (`io.modelcontextprotocol/skills`): the same workflows as `SKILL.md`,
  progressive disclosure; ChatGPT partial, little else yet.
- **MCP Apps** (`io.modelcontextprotocol/ui`; Claude, ChatGPT, VS Code, Cursor, Goose): a UI
  resource rendered inline: the batch decoded for the user before signing (calls, approvals,
  the payment), and the submission's stages live. **App-only tools** (`visibility: ["app"]`)
  stay out of the model's context. Signing from inside the sandboxed iframe isn't a given
  (extensions injecting into it [unverified]; Electron hosts have none); the app's `ui/open-link`
  can hand off to a Bruno signing page in the user's own browser (EIP-6963, their own wallet),
  which submits directly. Speculative: the `camera` permission and ERC-4527 QR codes for
  air-gapped signers (Keystone).
- **URL-mode elicitation** (via multi-round-trip requests): `submit` answers `input_required`
  with a URL to that signing page and is retried once signed. It needs the page's signature
  found again on the retry, by the typed data's hash: short-lived state the plan otherwise
  avoids.
- **More tools:**
  - **3009 from contract payers:** the `bytes`-signature overload, so smart wallets (Coinbase)
    can pay by flow 2. A relay change, not only a tool.
  - **`explain`:** a prepared batch or authorization decoded to plain text (calls, approvals,
    payment, deadline), for the signer's consent prompt; or folded into `calibur_batch`'s
    output.
  - **Allowance reads** folded into `calibur_batch` (above) rather than a tool of their own.

## Open questions

1. **`Origin`:** accept any (Bruno has no local surface and no credentials), or only absent
   and Bruno's own? Default: accept any, documented as why the rule doesn't bite.
2. **The signed transfer on REST and JSON-RPC `submit` too:** done in 0.5.2.
3. **Base:** out of scope (one sequencer, no builders dropping reverts); stated as a fact.
4. **A `chain` header** (`x-mcp-header` on `chain`, so Cloudflare's rate limiting could see
   it): default no.
5. **Which brainstorm items join this release:** default Tasks (when declared) and Prompts; Resources
   for config; Apps, elicitation and Skills later.
6. **3009's `bytes` overload** for contract payers: this release or later.
7. **The payment leg's margin** in `calibur_batch`: a fixed multiplier (Decimal, as the config's
   others), or an argument with a default.
8. **Calibur only, or user operations too.** This plan was written on 2026-10-23, before the
   relay became an ERC-4337 bundler (0.5.1, 2026-10-29), so every flow above goes through a
   Calibur batch. A wallet MCP that holds a smart account, rather than an EOA's key, could send
   user operations through the relay's JSON-RPC as it is, paying by their fees or by a call in a
   token. To decide before building: whether the `bruno` group gets user-operation tools (a
   quote before signing, the send, the receipt) beside or instead of part of the Calibur
   group, and which wallet MCPs hold a smart account that signs user operations.
