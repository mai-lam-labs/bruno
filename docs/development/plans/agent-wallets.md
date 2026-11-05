---
description: "Research for 0.6: what agents do onchain today, the wallets they use (MCP servers, CLIs, SDKs), who pays their gas, and the spending controls each offers; what it means for Bruno's MCP server and the AgentKit provider"
---

# Agent wallets and spending controls (research)

Research for 0.6, MCP, begun on 2026-11-03. Before Bruno's MCP tools or the AgentKit provider's
interface are designed, this establishes what agents do onchain today, through which wallets,
who pays their gas, and what controls bound their spending. Every claim links the page or API
it was read from. Where something wasn't checked, the page says so.

## The framework toolkits have stalled

Agent frameworks don't ship blockchain tools of their own. LangChain, the OpenAI Agents SDK and
the Vercel AI SDK reach chains through toolkits, or through MCP. The toolkits built for that
have stopped moving:

| Toolkit | State | npm downloads, 2026-10-04 to 11-02 |
|---|---|---|
| GOAT | Its [README](https://github.com/goat-sdk/goat) opens with "**[Archived]** This repository is a read-only historical snapshot. It is no longer actively maintained". Last code change August 2025; `@goat-sdk/core` last released May 2025 ([npm](https://registry.npmjs.org/@goat-sdk/core)). | `@goat-sdk/core` 6,919; its MCP adapter 156 |
| Coinbase AgentKit | Features stopped in March 2026 (the ERC-8004 provider, LangChain v1, AI SDK v6). Since then only fixes, mostly security: prompt injection, sanitising token metadata ([commits](https://github.com/coinbase/agentkit/commits/main)). Latest npm release 0.10.4, 2025-12-19 ([npm](https://registry.npmjs.org/@coinbase/agentkit)); PyPI 0.7.4, 2025-10-03 ([PyPI](https://pypi.org/project/coinbase-agentkit/)). | `@coinbase/agentkit` 13,893; LangChain adapter 1,638; MCP adapter 647; AI SDK adapter 541 |
| ElizaOS `plugin-evm` | Latest 1.0.13, September 2025; 2.0 alphas until June 2026 ([npm](https://registry.npmjs.org/@elizaos/plugin-evm)). The repository its npm entry names returns Not Found. | 707 |

For scale, over the same month: `ai` 122.6M, `@openai/agents` 7.8M, `langchain` 12.0M,
`@modelcontextprotocol/sdk` 256.8M, `viem` 27.8M ([npm downloads API](https://api.npmjs.org/downloads/point/last-month/ai)).
Download counts include CI and are noisy; the ratio is what matters.

What the toolkits were built for, by AgentKit's
[action providers](https://github.com/coinbase/agentkit/tree/main/typescript/agentkit/src/action-providers):
token launches (Clanker, Flaunch, Wow, Zora), DeFi (Morpho, Moonwell, Compound, Sushi, 0x, Enso,
Across, Superfluid), data (Pyth, DefiLlama, Messari, Zerion), social (Farcaster, Twitter),
payments (x402, ERC-20, WETH), identity (ERC-8004, Basenames). Base first, throughout.

## Where the activity is: the wallet vendors' own agent products

Each wallet vendor now ships an agent product of its own, reached as an MCP server, a CLI, or
skills (markdown an agent loads), rather than through a framework toolkit.

| Product | Reached as | Custody | Chains | Gas | Spending controls |
|---|---|---|---|---|---|
| **Coinbase Wallet MCP** ([skill](https://docs.cdp.coinbase.com/coinbase-for-agents/wallet-mcp/skill.md)), formerly Base MCP ([renamed](https://docs.base.org/ai-agents)) | remote MCP, `wallet-mcp.coinbase.com`, installed as a skill | the user's Coinbase smart wallet | `send_calls` on Base, Base Sepolia, Ethereum, Optimism, Polygon, Arbitrum, BSC, Avalanche ([batched calls](https://docs.cdp.coinbase.com/coinbase-for-agents/wallet-mcp/references/batch-calls.md)) | not documented, on any chain ([Wallet MCP](https://docs.cdp.coinbase.com/ai-agents/coinbase-for-agents/wallet-mcp.md)) | every write returns an approval URL the user opens in Coinbase Wallet ([approval mode](https://docs.cdp.coinbase.com/coinbase-for-agents/wallet-mcp/references/approval-mode.md)) |
| **Coinbase Agentic Wallet MCP** ([FAQ](https://docs.cdp.coinbase.com/agentic-wallet/mcp/faq.md)) | MCP (`@coinbase/payments-mcp`), CLI | an embedded wallet the user controls by email | USDC on Base, Polygon, Solana | "Free (sponsored)" | maximum per call and per session, set in the wallet app; agents "can't change them" |
| **Phantom MCP** ([overview](https://docs.phantom.com/phantom-mcp-server), [setup](https://docs.phantom.com/phantom-mcp-server/setup)) | local MCP, `@phantom/mcp-server` | Phantom's; a **new dedicated agent wallet**, not the user's own ([account types](https://docs.phantom.com/phantom-mcp-server/account-types)) | Solana, Ethereum, Bitcoin (EVM sends on Arbitrum rejected) | the agent wallet's own; it "must be funded" | isolation: "The agent can only spend what you send to its wallet" |
| **MetaMask Agent Wallet** ([docs](https://docs.metamask.io/llms-agent-wallet-full.txt)) | the `mm` CLI with skills, not MCP | a server wallet, or the user's own seed phrase | multiple EVM chains | the wallet's own ETH; a "gasless relay" via EIP-7702 for ERC-20 transfers only, and for swaps whose quote includes gas, where supported | Guard Mode: threat scanning, network and recipient allowlists, a rolling 24-hour outflow limit, approval outside the policy |
| **Circle Agent Wallets** ([agent stack](https://developers.circle.com/agent-stack.md), [chains](https://developers.circle.com/agent-stack/agent-wallets/supported-blockchains.md)) | Circle CLI and skills | not checked | Arbitrum, Arc, Avalanche, Base, Ethereum, Monad, Optimism, Polygon, Unichain | "$0 (sponsored)" for all onchain transactions, "capped, subject to fair use, and may change" ([fees](https://developers.circle.com/agent-stack/agent-wallets/fees.md)) | USDC limits over a rolling window, recipient and contract allowlists, per chain; 100 USDC a month by default ([policies](https://developers.circle.com/agent-stack/agent-wallets/wallet-operations/custom-policies.md)) |
| **Crossmint Agent Wallets** ([overview](https://docs.crossmint.com/agents/wallets/overview.md)) | SDK | a non-custodial smart wallet the user owns; the agent a signer | EVM, Solana, Stellar | sponsored by default on supported chains, "not all chains" ([onchain actions](https://docs.crossmint.com/agents/payment-methods/stablecoin-wallets/on-chain-actions.md)) | spend cap, allowed counterparties and a time window, "enforced onchain" ([scopes](https://docs.crossmint.com/wallets/guides/signers/scopes.md)) |
| **Cobo Agentic Wallet** ([pacts](https://agent-wallet-manual.mintlify.app/products/agentic-wallet/manual/start-here/what-is-a-pact.md), [MCP](https://agent-wallet-manual.mintlify.app/products/agentic-wallet/manual/developer/mcp.md)) | MCP server, SDKs, CLI, skill | MPC: "No single party holds the private key" ([introduction](https://agent-wallet-manual.mintlify.app/products/agentic-wallet/manual/start-here/introduction.md)) | Ethereum, Base, Solana "and more" | the wallet's own, estimated by `estimate_contract_call_fee` | a "pact": intent, execution plan, policies (budgets, allowlists, chain, token and contract limits) and completion conditions, after which access is revoked |
| **Openfort backend wallets** ([agentic wallets](https://www.openfort.io/docs/products/server/workflows/agentic-wallets)) | SDK | Openfort's | EVM, Solana | none held: a send without a sponsorship policy "stays pending indefinitely, with no error" | signing policies, fail-closed: network, address, value and calldata rules ([policies](https://www.openfort.io/docs/products/server/policies)) |
| **OKX Agentic Wallet** ([overview](https://web3.okx.com/onchainos/dev-docs/wallet/agentic-wallet.md), [skills](https://github.com/okx/onchainos-skills/tree/main/skills/okx-agentic-wallet)) | the `onchainos` CLI with skills; MCP for trading and market data | keys generated and used in a TEE | "nearly 20 chains", Ethereum among them | the wallet's own, except zero gas on X Layer and a Gas Station paying gas in stablecoins on Solana only | per-transaction and daily limits, a transfer whitelist, set by the user on OKX's portal |
| **Trust Wallet Agent Kit** ([skills](https://github.com/trustwallet/tw-agent-skills)) | the `twak` CLI and MCP | the user's Trust Wallet app over WalletConnect, or a local agent wallet | EVM chains through the app; 25+ chains for the agent wallet | not checked | in the app, the user approves each send, swap or approval; the agent wallet signs alone |
| **thirdweb MCP** ([MCP](https://portal.thirdweb.com/ai/mcp)) | remote MCP, `api.thirdweb.com/mcp`, with a project secret key | the developer's own server wallets, sent through its Engine | not checked | not checked | not checked |
| **evm-mcp-server** ([README](https://github.com/mcpdotdirect/evm-mcp-server)) | local MCP, community (379 stars) | a private key or mnemonic in its environment | EVM chains | the key's own | none |
| **Dynamic** ([agents](https://www.dynamic.xyz/docs/agents/overview.md)) | SDK | MPC server wallets, agent wallets acting as a user, or a user's delegated access | EVM, Solana | sponsored transactions on offer | delegated access with limited signing rights; screening policies |
| **Turnkey** ([policies](https://docs.turnkey.com/concepts/policies/overview.md)) | SDK, API, CLI, skills | keys in Turnkey's secure enclaves | not checked | the EOA's own, or a Gas Station the app pays, `sponsor: true`, executed as an EIP-7702 batch ([docs](https://docs.turnkey.com/llms-full.txt)) | a JSON policy engine: conditions over the transaction's fields, and a consensus of approvers |
| **Privy** ([controls](https://docs.privy.io/controls/overview.md)) | SDK, API | key shares reconstituted in a secure enclave | EVM, Solana | "App pays" from the app's credits; or "**User pays**": "Gas fees are covered using the wallet's stablecoin balance (USDC, USDT, EURC, USDG…) — no ETH required", for any `eth_sendTransaction` or `wallet_sendCalls`, on Ethereum, Base, Optimism, Arbitrum, Polygon and Sepolia ([gas](https://docs.privy.io/wallets/gas-and-asset-management/gas/setup.md)) | policies enforced in the enclave: transaction limits, approved destinations, contracts, action parameters; owners and signers, quorums ([delegation](https://docs.privy.io/controls/common-use-cases/delegation.md)) |

Composition already has a standard shape. Coinbase Wallet MCP's partner "plugins" aren't
servers but markdown telling the model to call a partner's API and pass the returned calls to
`send_calls` (`{to, value, data}`, EIP-5792); its twenty plugins are mostly on Base: swaps,
token launches and buying new tokens, lending, perps, gift cards.

## What each will sign without sending

Bruno composes with a wallet only through what the wallet signs and hands back: EIP-712 typed
data (an EIP-3009 payment, a Calibur batch, an EntryPoint v0.8 user operation's hash), an
EIP-7702 authorization, or a signed transaction. A wallet that only sends through its own
pipeline leaves nothing for Bruno to send.

| Wallet | Arbitrary calldata | EIP-712 typed data | A transaction, signed, not sent | EIP-7702 authorization |
|---|---|---|---|---|
| Coinbase Wallet MCP | `send_calls`, sent by the wallet after the user approves | `sign` with `typed_data` (EIP-712), approved in Coinbase Wallet ([Wallet MCP](https://docs.cdp.coinbase.com/ai-agents/coinbase-for-agents/wallet-mcp.md)); a smart wallet's signature | no: "The MCP server itself does not sign or broadcast", the Coinbase Wallet app does ([README](https://github.com/coinbase/agents/blob/main/skills/wallet-mcp/README.md)) | no |
| Phantom MCP | `evm_send`, which "simulates, signs, and broadcasts" | `evm_sign-typed` ([tools](https://docs.phantom.com/phantom-mcp-server/tools)) | no | no |
| MetaMask Agent Wallet | `mm wallet send-transaction`, signed and sent | `mm wallet sign-typed-data` ([docs](https://docs.metamask.io/llms-agent-wallet-full.txt)) | no | no (its own 7702 relay only) |
| OKX Agentic Wallet | `wallet contract-call --input-data`, sent by the wallet | `wallet sign-message --type eip712` ([CLI](https://github.com/okx/onchainos-skills/blob/main/skills/okx-agentic-wallet/references/wallet-cli-reference.md)) | no | no |
| Trust Wallet Agent Kit | no: send, swap, approve | not documented: `sign-message` is EIP-191 | no | no |
| evm-mcp-server | `write_contract` | `sign_typed_data` | not checked | no |
| Crossmint | its generic transaction path, sent by the wallet | `signTypedData` from the smart wallet, which must be deployed first when an agent's signer signs ([guide](https://docs.crossmint.com/wallets/guides/sign-message.md)) | no | no |
| Circle Agent Wallets | no: `circle wallet execute` takes an ABI signature and parameters | `circle wallet sign typed-data` ([CLI reference](https://developers.circle.com/agent-stack/circle-cli/command-reference.md)) | no | no |
| Privy | `eth_sendTransaction` | `eth_signTypedData_v4` | `eth_signTransaction` | `eth_sign7702Authorization`; also `eth_signUserOperation` and `secp256k1_sign` of a hash ([docs](https://docs.privy.io/llms-full.txt)) |
| Turnkey | through signing | `signRawPayload`, EIP-712 encoding | `signTransaction` | `signRawPayload`, EIP-7702 authorization encoding ([docs](https://docs.turnkey.com/llms-full.txt)) |
| Dynamic | yes | `signTypedData` | `signTransaction` | `delegatedSign7702Authorization` ([Node reference](https://www.dynamic.xyz/docs/node/llms.txt)) |
| Cobo Agentic Wallet | `contract_call` | `message_sign`, its types not checked | not checked | not checked |
| Openfort backend wallets | `sendEvmTransaction` | `signEvmTypedData` | `signEvmTransaction`; `signEvmHash` ([policies](https://www.openfort.io/docs/products/server/policies)) | not checked |

Two kinds of wallet, then. The consumer wallets an agent reaches as MCP servers or CLIs
(Coinbase's, Phantom's, MetaMask's, Circle's, OKX's, Crossmint's) send calls only through
themselves, but nearly all of them sign typed data arbitrarily; Trust Wallet's is the exception
found. The infrastructure wallets an agent's developer builds on (Privy, Turnkey, Dynamic,
Openfort) expose every signing primitive, each behind their policies. EIP-712
typed data is the one thing nearly all of them sign for anyone. EIP-7702 authorizations come
only from the infrastructure wallets.

## Spending controls, by where they're enforced

- **By the vendor, before signing:** Privy's enclave, Openfort's and Turnkey's policy engines,
  Circle's, MetaMask's and OKX's limits, Cobo's pacts, Lit's Vincent (policies as Lit Actions
  over keys held by Lit's network; [README](https://github.com/LIT-Protocol/Vincent), last
  pushed 2026-01-31). The agent's key can't sign outside them, but they hold only
  as long as the vendor does.
- **Onchain, by the wallet's contract:** Crossmint's signer scopes; session keys on modular
  smart accounts, limited by chain, contract, function, parameters, value, time and use
  ([Rhinestone's session limits](https://docs.rhinestone.dev/wallets/session-keys/session-limits.md)),
  or ZeroDev's, where a permission is "1 signer + N policies + 1 action" (policies on calls,
  gas, rate and time; [permissions](https://docs.zerodev.app/smart-accounts/permissions/intro));
  Safe's AllowanceModule, a per-token allowance for a delegate that can reset on an interval
  ([Safe's guide](https://docs.safe.global/home/ai-agent-quickstarts/agent-with-spending-limit));
  ERC-7710 and ERC-7715 ([delegated permissions](delegations.md)).
- **By isolation:** a separate wallet holding only what it's given (Phantom, MetaMask's server
  wallet).
- **By a human, per write:** Coinbase Wallet MCP's approval URL; MetaMask outside its policy.

The kinds of limit recur across vendors: a cap per transaction, a cap over a rolling window or
session, recipient and contract allowlists, chain and token restrictions, an expiry, and human
approval past a threshold.

## Who pays the gas

- **The vendor, as a sponsor:** Circle on all its chains, Ethereum included, within a fair-use
  cap; Coinbase's Agentic Wallet; Crossmint on its supported chains. Each needs the vendor's
  account and API key, and the sponsorship is theirs to change.
- **The app, through a sponsorship policy:** Openfort, Turnkey's Gas Station, Privy's "app pays".
- **The wallet's own stablecoins, for any transaction:** Privy's "user pays" mode, on Ethereum,
  Base, Optimism, Arbitrum and Polygon. This is Bruno's model, inside Privy's wallets.
- **A relay, paid in the token:** MetaMask, for ERC-20 transfers and gas-included swaps only;
  OKX's Gas Station, on Solana only.
- **The agent's own ETH:** Phantom, and MetaMask for anything else.
- **A relayer paid from an allowance:** Safe's AllowanceModule takes a `payment` in a token
  beside each transfer and sends it to `tx.origin`, the account sending the transaction
  ([`executeAllowanceTransfer`](https://github.com/safe-global/safe-modules/blob/main/modules/allowances/contracts/AllowanceModule.sol)).
  The delegate signs, and anyone can submit.

## Composing with Coinbase Wallet MCP: plugins, and the POST limit

Wallet MCP's way to compose is a plugin: markdown that teaches the model to call a protocol's
API (a GET to read, a GET to prepare calls) and pass the result to `send_calls`, `swap` or
`sign` ([custom plugins](https://docs.cdp.coinbase.com/ai-agents/coinbase-for-agents/wallet-mcp.md)). Its `web_request` tool reaches only allowlisted partner hosts.
Elsewhere, in Claude's and ChatGPT's consumer apps, a non-allowlisted API is "effectively
GET-only": the model may fetch only a URL the user has pasted into the chat, and "POST endpoints
are not supported" ([custom-plugins reference](https://github.com/coinbase/agents/blob/main/skills/wallet-mcp/references/custom-plugins.md)).
In coding harnesses (Claude Code, Codex, Cursor) the model's own HTTP tool makes any request.

A Bruno flow ends with a POST of what the wallet signed. As a Wallet MCP plugin, it would work
in coding harnesses only. A connected MCP server of Bruno's own takes the POST as a tool call on
any surface.

## Agent experience, as the wallets shape it

- **Refusals as tool content.** Cobo returns a policy denial "as normal tool content (not
  protocol-level tool failures), so the LLM can reason and retry", with a `suggestion`
  ([MCP](https://agent-wallet-manual.mintlify.app/products/agentic-wallet/manual/developer/mcp.md)).
- **An approval step the model waits on.** Coinbase's writes return a link the user approves
  in Coinbase Wallet; the model polls the request once, after the user says they approved.
  Trust Wallet's wait in its app, unannounced, and expire after about five minutes
  ([wallet reference](https://github.com/trustwallet/tw-agent-skills/blob/main/skills/wallet/references/wallet.md)).
- **Tool selection by the integrator.** thirdweb's endpoint takes a `tools` query parameter, and
  Cobo an `AGENT_WALLET_INCLUDE_TOOLS` list: only the tools named are listed, which keeps the
  model's context small.

## Ways Bruno can integrate, by what the wallet hands over

Worked through on 2026-11-03. Models write calldata well, with an inline snippet and a
description of it, so Bruno needs no encoders: what matters is what the wallet signs.

1. **EIP-712 typed data, which nearly every wallet signs.** Bruno sends it:
   - **An EIP-3009 payment, the call run by the runner.** Works today, from any EOA or smart
     wallet. The call's `msg.sender` is the runner, so it suits calls that name a beneficiary
     (an ERC-4626 `deposit(assets, receiver)`, Aave's `supply(…, onBehalfOf)`, a swap's
     `recipient`, mints and claims to an address). A second signed transfer (EIP-3009, or
     Permit2's `permitTransferFrom`, whose witness can commit to the call) can move the user's
     own funds to the runner, which acts on them and sends the output back: a stateless
     executor, the user's money moving only by signature.
   - **A SafeTx for the user's own Safe:** see [a Safe by one signature](safe.md). `msg.sender` is the user's account, with no delegation.
   - **A signed batch, or a v0.8 user operation, for an account already delegated by EIP-7702**
     (a Calibur `SignedBatchedCall`, MetaMask's DeleGator, Simple7702). Many accounts are
     delegated by their own wallet app; which, and to what, is to check.
   - **Protocols that already pay their relayer:** Safe's AllowanceModule (to `tx.origin`),
     ERC-7710 delegations whose caveats allow a payment.
2. **A signed transaction, not sent** (Privy, Turnkey, Dynamic, Openfort, or an agent's own
   key): Bruno funds the account and sends it in one atomic bundle, then checks it was paid.
   `msg.sender` is the user's own account. See [atomic sponsorship](sponsorship.md).
3. **An EIP-7702 authorization** (the infrastructure wallets only): carried in the submission's
   `authorizationList`, delegating the account in the transaction that first uses it; then as
   in 1.
4. **An x402 payment.** Bruno's `/x402` endpoint is an x402 resource: its 402 is the prepare
   step, the request's body the call, and the wallet's own x402 tool signs the EIP-3009
   payment. No integration on the wallet's side, where the wallet pays on a chain Bruno serves.
   The call runs from the runner, as in the first case of 1.
5. **A bundler URL the wallet lets you set:** the SDKs (viem, permissionless.js, ZeroDev), not
   the MCP wallets.

And how each reaches the model: a Bruno MCP server takes a POST from any surface; a Wallet MCP
plugin only from coding harnesses; x402 through the wallet's own tool.

## What it suggests for Bruno (hypotheses, to test)

- **Agents with a vendor wallet are rarely short of ETH.** Circle sponsors on Ethereum,
  Coinbase on Base, and Privy lets a wallet pay any transaction's gas in its stablecoins on
  Ethereum. Bruno's case is the agent whose wallet doesn't (Phantom's, OKX's, a MetaMask wallet
  doing more than transfers, Cobo's, an agent's own key), or one that doesn't want a vendor
  account and its fair-use cap.
- **The composition point is typed data, not the wallet's send.** The consumer wallets send
  only through themselves, so Bruno can't be their sender; but nearly every wallet signs
  arbitrary EIP-712 for anyone. A Bruno tool that prepares typed data, takes back the
  signature and sends composes with all of them. Which typed data matters: an EIP-3009 payment
  works from any EOA with no delegation, but its call runs from the runner, so only a call that
  doesn't depend on `msg.sender`; a Calibur batch or a v0.8 user operation acts as the account,
  but needs it delegated first, and only the infrastructure wallets sign the authorization.
- **Spending controls are where wallets differ most, and Bruno sits beside them, not in them.**
  A payment Bruno takes is a transfer the wallet's policy must allow: its payee allowlisted,
  its amount within the cap. Safe's AllowanceModule already pays a relayer within an
  allowance, which Bruno could be, if its runner counted a payment to the sending account
  rather than its payee.
- **A server of its own, not only a skill.** A skill or plugin can't POST from the chat apps;
  a connected MCP server can.
- **Distribution is skills plus one MCP**, not an integration per framework. Building for the
  toolkits, the AgentKit provider included, is a weak bet while they stand still.

## Not yet checked

- Whether Coinbase Wallet MCP's `send_calls` sponsors gas on Ethereum: its docs don't say,
  and its tools need a signed-in account even to be listed.
- Whether a Coinbase smart wallet's EIP-3009 signature passes where Bruno checks one (ERC-1271,
  or ERC-6492 while undeployed, through USDC's `bytes` overload), tested end to end.
- Trust Wallet's gas, which its skills don't mention; thirdweb's chains, gas and controls;
  Cobo's `message_sign` types.
- How many agents use any of these: no vendor publishes it.
