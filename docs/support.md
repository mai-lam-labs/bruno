---
description: "What works where, and how thoroughly each part is tested (on anvil, live on Sepolia, or untested): chains, payment methods, standards, implementations and clients"
---

# Support

This page lists what works where, and how thoroughly each part has been tested:

- **Anvil:** tested locally at the Prague and Amsterdam forks, against contracts fetched from
  mainnet, through both relay implementations.
- **Live:** tested on Sepolia with real transactions.
- **Untested.**

Features that aren't done yet are listed in the [design](DESIGN.md#not-yet).

## Chains

| Chain | Served at bruno.mailam.app | Builders | Tested |
|---|---|---|---|
| Ethereum | Yes, at `/ethereum` | Titan, BuilderNet, Quasar and BTCS (`droppingTxHashes`); Flashbots and beaverbuild (standalone bundles) | The treasury runs live; `make ethereum-verify` hasn't been run |
| Sepolia (Amsterdam, ePBS) | Yes, at `/sepolia` | nflaig's ePBS builder (standalone bundles) | Live: both payment methods, against both relays |
| Unichain Sepolia, Base Sepolia | No | None | The deployer, Calibur and x402's Permit2 proxy are deployed, but Bruno can't send on OP-stack chains yet |

Bruno can serve any chain on a fork it knows (Prague or Amsterdam) that has the deployer and a
builder that takes bundles. See [networks and builders](networks.md).

## Payment methods

| Payment method | Tokens | Who can pay | Tested |
|---|---|---|---|
| **The call pays** (a submission, over REST or JSON-RPC) | ETH and WETH; on Ethereum also USDC, USDT and DAI (priced by Uniswap v3); on Sepolia also USDC | Any call that pays the payee the amount while it runs, wherever the money comes from: the account's own funds through its EIP-7702 delegate (with or without an authorization in the submission), what the call releases or earns, or another account's signed payment | Anvil: ETH, WETH, USDC and USDT from accounts delegated to Calibur, including ETH the call releases by unwrapping WETH, and calls to contracts that pay. Live: ETH from an account delegated to Calibur |
| **x402** v2, `exact` scheme, EIP-3009 | USDC | A plain key (checked with ecrecover) | Anvil and live |
| | | A contract (checked with ERC-1271), such as a Coinbase Smart Wallet | Anvil |
| | | An account delegated to Calibur (checked with ERC-1271 against its root key) | Anvil |

Not supported yet: x402 payments through Permit2 (for tokens without EIP-3009), and ERC-6492
(smart accounts that aren't deployed yet).

## Standards

- **EIP-7702:** type-4 transactions, and delegations included in a submission.
- **EIP-3009 and EIP-712:** x402 payments.
- **ERC-1271:** contracts as payers.
- **x402 v2:** the `exact` scheme, the `bazaar` extension and `/.well-known/x402`.
- **Flashbots' `eth_sendBundle`:** `droppingTxHashes`, standalone bundles and
  `X-Flashbots-Signature`.
- **EIP-1474:** error codes over JSON-RPC.
- **OpenAPI and OpenRPC:** documents describing both interfaces.
- **Uniswap v2 and v3:** pools used for prices.

## Implementations

| Relay | How it runs | Tested |
|---|---|---|
| Python (Litestar) | `make serve`, or the Docker image `ghcr.io/mai-lam-labs/bruno` | Anvil; its bundle path live on Sepolia |
| Cloudflare Worker | Deployed at bruno.mailam.app | Anvil (under workerd); live on Sepolia, deployed and verified with real payments |

Both implement the same interface, and every server test runs against each of them.

## Clients

| Client | How it pays | Runtimes | Tested |
|---|---|---|---|
| `@mailam/bruno` (JSR) | Submissions, including EIP-7702 authorizations; x402 with any signer of typed data (such as viem's accounts) | Deno, Node and Bun; installs with npm, pnpm or Bun | Anvil, installed the way npm users get it. Browsers and Cloudflare Workers are untested |
| `@mailam/bruno-agentkit` (JSR) | x402, in USDC, from an AgentKit agent's wallet | Deno, Node and Bun | Anvil: an agent using AgentKit's viem wallet provider |
| x402's Python SDK (`x402[httpx,evm]`) | x402 | Python | Anvil: it pays through both relays, and its models parse every response |

## Project status

Bruno is a proof of concept, written in October 2026.

- **Locally,** every path runs on anvil at Prague and at Amsterdam, against contracts fetched
  from mainnet, through a mock builder that applies rbuilder's rule for committing bundles.
- **Live on Sepolia,** through an ePBS builder using standalone bundles, a paying submission
  landed, and an underpaying one was dropped and cost nothing. The Cloudflare Worker, running
  on its own alarms, landed a submission there in its second slot. It also settled an x402
  payment in about 40 seconds for a client holding only USDC: the request stayed open until
  the transaction landed, and the runner left nothing in state.
