---
description: "What works where, and how thoroughly each part is tested (on anvil, live on Sepolia, or untested): chains, payment methods, smart wallets, standards, implementations and clients"
---

# Support

This page lists what works where, and how thoroughly each part has been tested:

- **Anvil:** tested locally at the Prague and Amsterdam forks, against contracts fetched from
  mainnet, through both relay implementations.
- **Live:** tested on Sepolia with real transactions.
- **Untested.**

Features that aren't done yet are on the [roadmap](development/plans/priorities.md#known-gaps).

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
| **A signed transfer** (a submission's `payment.transfer`, over REST or JSON-RPC) | USDC | A plain key, or a contract (ERC-1271), such as a smart wallet, deployed or not (ERC-6492) | Anvil: a key, and a Coinbase Smart Wallet deployed and not |
| **x402** v2, `exact` scheme, EIP-3009 | USDC | A plain key (checked with ecrecover) | Anvil and live |
| | | A smart wallet (checked with ERC-1271), such as a Coinbase Smart Wallet | Anvil |
| | | A smart wallet that isn't deployed yet (ERC-6492), deployed by the same transaction | Anvil, including with x402's own Python client |
| | | An account delegated to Calibur (checked with ERC-1271 against its root key) | Anvil |

Not supported yet: x402 payments through Permit2, for tokens without EIP-3009.

## Smart wallets

Any smart wallet can pay, deployed or not, as long as it checks signatures with ERC-1271 and,
while undeployed, wraps them as ERC-6492 says. Bruno calls any factory: it keeps no list of
allowed ones.

What a wallet's signature check costs is part of the price. For the wallets below, Bruno knows
it exactly, measured on each fork. For any other wallet, it charges the most it knows, unless
the client gives `verifyGas`; if that turns out short, the signed payment is priced again.

| Wallet | Owner | Factory | Created by | Tested |
|---|---|---|---|---|
| Coinbase Smart Wallet v1 | A key | `0x0BA5ED0c6AA8c49038F819E587E2633c4A9F428a` | viem's `toCoinbaseSmartAccount` (`version: "1"`) | Anvil: x402, submissions, AgentKit, deployed and not |
| Coinbase Smart Wallet v1.1 | A key | `0xBA5ED110eFDBa3D005bfC882d75358ACBbB85842` | CDP smart accounts, AgentKit's `CdpSmartWalletProvider`, the Base Account SDK, viem (`version: "1.1"`) | Anvil: AgentKit, deployed and not |
| ZeroDev Kernel v3.2 | A key (ZeroDev's ECDSA validator) | `0xd703aaE79538628d27099B8c4f621bE4CCd142d5`, ZeroDev's meta-factory, calling `0x7a1dBAB750f12a90EB1B60D2Ae3aD17D4D81EfFe` | AgentKit's `ZeroDevWalletProvider`, `@zerodev/sdk`'s `createKernelAccount` (`KERNEL_V3_2`) | Anvil: x402, AgentKit (through `@zerodev/sdk`, with AgentKit's options), deployed and not |

Wallets with passkey (WebAuthn) owners aren't measured yet.

To have another wallet added, [request it on GitHub](https://github.com/mai-lam-labs/bruno/issues/new?template=smart-wallet.yml).

## Standards

- **EIP-7702:** type-4 transactions, and delegations included in a submission.
- **EIP-3009 and EIP-712:** x402 payments.
- **ERC-1271:** contracts as payers.
- **ERC-6492:** smart wallets that aren't deployed yet, deployed by the transaction that takes
  their payment, and wallets prepared by their signature.
- **ERC-4337's `factory` and `factoryData`:** the names a deployment is given in.
- **ERC-4337 and ERC-7769:** user operations for EntryPoint v0.8 and v0.7, sent through the
  chain's endpoint as a bundler's; EIP-7702 accounts'; another's paymaster. Tested with
  Circle's paymaster and Pimlico's singleton paymaster (its sponsoring mode), both v0.8.
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
| `@mailam/bruno` (JSR) | Submissions, including EIP-7702 authorizations, or paid by a signed transfer; x402 with any signer of typed data (such as viem's accounts, smart accounts included, deployed or not) | Deno, Node and Bun; installs with npm, pnpm or Bun | Anvil, installed the way npm users get it. Browsers and Cloudflare Workers are untested |
| `@mailam/bruno-agentkit` (JSR) | x402, in USDC, from an AgentKit agent's wallet: a key, or a smart wallet, deployed or not | Deno, Node and Bun | Anvil: an agent using AgentKit's viem wallet provider, with a key, Coinbase Smart Wallets v1 and v1.1, and ZeroDev's Kernel v3.2 |
| viem's bundler client (`createBundlerClient`, viem 2.57) and ZeroDev's (`createKernelAccountClient`, `@zerodev/sdk` 5.5) | ERC-4337 user operations (EntryPoint v0.7 and v0.8), paying the relay by their fees in ETH, or in a token by their own calls, their fees zero, at a price quoted before they're signed | Node | Anvil, each library's current version: both send through both relays from an undeployed Kernel v3.2, holding ETH (paying by its fees, at the relay's gas price) or only USDC (paying in it), and wait for the receipt; and from an EOA holding ETH, delegating in its first op, as viem's `toSimple7702SmartAccount` (EntryPoint v0.8), and on v0.7 ZeroDev's EIP-7702 Kernel v3.3, MetaMask's Stateless7702 (`@metamask/smart-accounts-kit` 2.0) and Alchemy's Modular Account v2 in 7702 mode (`@alchemy/smart-accounts` 5.3); Circle's paymaster quickstart, from an EOA holding only USDC |
| x402's Python SDK (`x402[httpx,evm]`) | x402 | Python | Anvil: it pays through both relays, from a key and from an undeployed smart wallet, and its models parse every response |

## Project status

Bruno is a proof of concept, written in October 2026.

- **Locally,** every path runs on anvil at Prague and at Amsterdam, against contracts fetched
  from mainnet, through a mock builder that applies rbuilder's rule for committing bundles.
- **Live on Sepolia,** through an ePBS builder using standalone bundles, a paying submission
  landed, and an underpaying one was dropped and cost nothing. The Cloudflare Worker, running
  on its own alarms, landed a submission there in its second slot. It also settled an x402
  payment in about 40 seconds for a client holding only USDC: the request stayed open until
  the transaction landed, and the runner left nothing in state.
