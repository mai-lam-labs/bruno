---
description: "What works where, and how far each is proven (anvil, live on Sepolia, untested): chains, payments, standards, relays and clients"
---

# Support

What works where, and how far each is proven: **anvil** (locally, at Prague and Amsterdam,
against contracts fetched from mainnet, through both relays), **live** (on Sepolia, with real
transactions), or **untested**. What's missing is listed in [DESIGN.md, Not yet](DESIGN.md#not-yet).

## Chains

| Chain | Served at bruno.mailam.app | Builders | Proven |
|---|---|---|---|
| Ethereum | Yes, `/ethereum` | Titan, BuilderNet, Quasar, BTCS (`droppingTxHashes`); Flashbots, beaverbuild (standalone) | The treasury runs live; `make ethereum-verify` not run |
| Sepolia (Amsterdam, ePBS) | Yes, `/sepolia` | nflaig's ePBS builder (standalone) | Live: both payment paths, against both relays |
| Unichain Sepolia, Base Sepolia | No | — | Deployer, Calibur and x402's Permit2 proxy deployed; no sender for OP-stack chains yet |

Any chain on a known fork (Prague, Amsterdam) with the deployer and a bundle-taking builder can
be served (Networks, below).

## Paying

| Payment | Tokens | Payer | Proven |
|---|---|---|---|
| **The call pays** (`submit`, REST or JSON-RPC) | ETH, WETH, and on Ethereum USDC, USDT, DAI (Uniswap v3 priced); on Sepolia USDC | Any call that pays the payee the amount as it runs, whatever the source: an account's own funds through its EIP-7702 delegate (its authorization in the submission or not), what the call releases or earns, another account's signed payment | Anvil: ETH, WETH, USDC, USDT from accounts delegated to Calibur, among them ETH the call releases by unwrapping WETH; calls to contracts that pay. Live: ETH from an account delegated to Calibur |
| **x402** v2, `exact`, EIP-3009 | USDC | A key (ecrecover) | Anvil and live |
| | | A contract (ERC-1271): Coinbase Smart Wallet | Anvil |
| | | An account delegated to Calibur (ERC-1271, its root key) | Anvil |

Not yet: x402 by Permit2 (tokens without EIP-3009), ERC-6492 (an undeployed smart account).

## Standards

EIP-7702 (type-4 transactions, delegations in a submission), EIP-3009 and EIP-712 (x402
payments), ERC-1271 (contract payers), x402 v2 (`exact`, the `bazaar` extension,
`/.well-known/x402`), Flashbots' `eth_sendBundle` (`droppingTxHashes`, standalone,
`X-Flashbots-Signature`), EIP-1474 error codes over JSON-RPC, OpenAPI and OpenRPC documents,
Uniswap v2 and v3 pools for prices.

## Relays

| Relay | Runs | Proven |
|---|---|---|
| Python (Litestar) | `make serve`; Docker, `ghcr.io/mai-lam-labs/bruno` | Anvil; its bundle path live on Sepolia |
| Cloudflare Worker | bruno.mailam.app | Anvil (under workerd); live on Sepolia, deployed and verified with real payments |

Both answer the same interface; every server test runs against each.

## Clients

| Client | Pays | Runtimes | Proven |
|---|---|---|---|
| `@mailam/bruno` (JSR) | `submit`, with any EIP-7702 authorizations; x402 with any signer of typed data (viem's accounts) | Deno, Node, Bun; installed by npm, pnpm, Bun | Anvil, as npm users install it; browsers and Cloudflare Workers untested |
| `@mailam/bruno-agentkit` (JSR) | x402, in USDC, from an AgentKit agent's wallet | Deno, Node, Bun | Anvil: an agent with AgentKit's viem wallet provider |
| x402's Python SDK (`x402[httpx,evm]`) | x402 | Python | Anvil: pays through both relays; its models parse every answer |

## Status

A proof of concept, written in October 2026.

- **Locally:** every path runs on anvil at Prague and at Amsterdam, against contracts fetched
  from mainnet, through a mock builder that applies rbuilder's commit rule.
- **Live:** on Sepolia, through an ePBS builder, in standalone bundles: a paying submission
  landed, and an underpaying one was dropped and cost nothing. The Cloudflare Worker port,
  under workerd and on its own alarms, landed a submission there in its second slot, and
  settled an x402 payment in about 40 seconds: a client holding only USDC, its request held
  until the transaction landed, the runner leaving nothing in state.
