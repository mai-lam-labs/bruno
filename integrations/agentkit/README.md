# @mailam/bruno-agentkit

Lets an agent built on Coinbase's [AgentKit](https://github.com/coinbase/agentkit) act on Ethereum
while holding only USDC and no ETH. It pays [Bruno](https://bruno.mailam.app), the permissionless
gas relay, over [x402](https://x402.org) from its own wallet, and the payment only goes through if
its transaction succeeds and lands. There's no API key and no account to set up.

## Install

```sh
npx jsr add @mailam/bruno-agentkit     # npm
pnpm i jsr:@mailam/bruno-agentkit      # pnpm
bunx jsr add @mailam/bruno-agentkit    # Bun
deno add jsr:@mailam/bruno-agentkit    # Deno
```

It works wherever AgentKit does: Node 20 or later, Bun or Deno.

## Use

```ts
import { AgentKit } from "@coinbase/agentkit";
import { brunoActionProvider } from "@mailam/bruno-agentkit";

const agentkit = await AgentKit.from({
  walletProvider, // any EVM wallet provider, on Ethereum mainnet or Sepolia
  actionProviders: [brunoActionProvider()],
});
```

From there, use AgentKit's framework extensions (LangChain, Vercel AI SDK, MCP) as you normally
would.

## Actions

| Action              | What it does                                                                                                 |
| ------------------- | ------------------------------------------------------------------------------------------------------------ |
| `send_usdc_gasless` | Sends USDC from the wallet to any address. The wallet needs the amount plus Bruno's fee in USDC, and no ETH. |
| `send_call_gasless` | Sends any call (`to`, `data`, optional `gas`) and pays Bruno in USDC.                                        |
| `get_gasless_price` | Bruno's price for a call, in USDC, without sending it.                                                       |

Every action that pays takes a `maxFee`: the most USDC to pay Bruno, in whole units as a decimal
(`"5"` by default). If Bruno's price is higher than that, it's refused before anything is signed.

`send_call_gasless` runs the call from Bruno's own contract, **not from the agent's wallet**. Its
description tells the agent this: it's for calls whose effect doesn't depend on who sends them (a
claim to a named recipient, a signed authorization, a mint for an address). Moving USDC from the
wallet is what `send_usdc_gasless` is for.

## Options

```ts
brunoActionProvider({
  url: "https://bruno.mailam.app", // the relay (this is the default)
  fetch, // a fetch of your own, if you need one
  chains: {
    // chains beyond Ethereum mainnet and Sepolia that your relay serves, by chain id: each with
    // its tokens, USDC among them
    "31337": [{ symbol: "USDC", address: "0x…", decimals: 6 }],
  },
});
```

## Limits

- **Chains:** Ethereum mainnet and Sepolia, and any other chain your relay serves, given in
  `chains`.
- **Wallets:** any EVM wallet provider: a key (a viem or CDP key wallet), or a smart wallet (CDP's
  smart wallet provider, ZeroDev, or a viem smart account), deployed or not. A smart wallet that
  isn't deployed yet is deployed by the same transaction that takes its first payment, so it never
  needs ETH. Tested through AgentKit's viem wallet provider, with Coinbase Smart Wallets v1 and
  v1.1, and with ZeroDev's Kernel v3.2 made as `ZeroDevWalletProvider` makes it. That provider
  itself can't be run on a local chain, as it takes its chain from AgentKit's list of public
  networks. CDP's smart wallet provider makes Coinbase Smart Wallets v1.1, but needs CDP's servers,
  so it isn't tested itself.
- **Telemetry:** the actions send none. They're registered directly rather than through AgentKit's
  `@CreateAction` decorator, so AgentKit's per-action events aren't sent. AgentKit's wallet
  providers still report themselves to Coinbase when created (the wallet's address and chain),
  whatever actions they're given.

## Links

- [@mailam/bruno](https://jsr.io/@mailam/bruno): the client this is built on, for paying for calls
  outside AgentKit
- [Bruno](https://bruno.mailam.app)
- [Source](https://github.com/mai-lam-labs/bruno)
