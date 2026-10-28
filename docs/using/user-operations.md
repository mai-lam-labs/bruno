---
description: "Sending ERC-4337 user operations through Bruno with viem's bundler client or ZeroDev's Kernel account client, the relay's endpoint as the bundler: holding ETH, paying by the op's fees as at any bundler; holding only a token, paying by the op's own calls"
---

# ERC-4337 accounts

Each chain's [JSON-RPC endpoint](json-rpc.md) answers ERC-7769's bundler methods for EntryPoint
v0.7, so an ERC-4337 library can use it as its bundler: viem's bundler client, ZeroDev's Kernel
account client, or anything built on them. No API key, paymaster or bundler account is needed.

An op pays the relay one of two ways:

- **Holding ETH, by its fees, as at any bundler.** Set the client up as for any bundler, with
  the relay as its URL. The op's gas is paid at the price the relay asks, which it answers as
  Pimlico's and ZeroDev's bundlers do (`pimlico_getUserOperationGasPrice`,
  `zd_getUserOperationGasPrice`).
- **Holding no ETH, by its own calls.** Its fees are zero, so the account needs no ETH, and its
  last call transfers a token the relay takes, such as USDC, to the relay's `payee` (from
  [the terms](rest.md#terms)). If that's too little, sending fails as underpaid with the amount
  required in the error's `data.required`, and you sign again with that amount.

As with any submission, nothing is included unless the payee receives the payment, and an op
that isn't included costs nothing. An op paying by its fees pays for its gas even if its calls
fail, as at any bundler. An op paying by its calls pays only if they succeed when its account
reverts a whole batch on a failing call, as Kernel does; other accounts' batches haven't been
checked yet.

The account can be undeployed: the op's factory and factory data deploy it, as usual. The
scripts below make a ZeroDev Kernel; other smart accounts should work the same way, but only
Kernel is tested. Each runs on Node 24 as it is, and its comment says which environment
variables to set.

## Holding ETH

### viem

viem's `createBundlerClient`, the relay as its transport, its fees asked of the relay as for
Pimlico's bundler:

```ts
--8<-- "npm-test/userop-viem-eth.ts"
```

### ZeroDev

ZeroDev's `createKernelAccountClient`, the relay as its `bundlerTransport`: it asks the relay
for its fees by default. No ZeroDev project ID is needed: it's only part of ZeroDev's own URLs.

```ts
--8<-- "npm-test/userop-zerodev-eth.ts"
```

## Holding no ETH

### viem

```ts
--8<-- "npm-test/userop-viem.ts"
```

The first attempt pays one unit, so it fails as underpaid and tells the script the price. The
second attempt pays that price and normally succeeds. It can still fail if the price rose in
between, which is why the script allows a few attempts.

### ZeroDev

The same with ZeroDev's client. Giving the op's fees skips the one method the client would
otherwise ask the relay for them.

```ts
--8<-- "npm-test/userop-zerodev.ts"
```

AgentKit's `ZeroDevWalletProvider` sends through ZeroDev's intents, not a bundler, so with
AgentKit, use the [AgentKit provider](agentkit.md) instead.

## Methods

| Method | What it does |
|---|---|
| `eth_estimateUserOperationGas` | Simulates the op with the account's stub signature, and returns its gas limits with a tenth added; `preVerificationGas` zero for an op paying by its calls, the rest of the transaction's gas for one paying by its fees |
| `pimlico_getUserOperationGasPrice`, `zd_getUserOperationGasPrice` | The price an op paying by its fees is asked, as both its fees |
| `eth_sendUserOperation` | Takes the signed op as a submission, and returns its hash |
| `eth_getUserOperationReceipt` | ERC-7769's receipt, or null until the op is included |
| `eth_supportedEntryPoints` | EntryPoint v0.7's address |
| `eth_chainId` | The chain's ID |

The [specification](../SPEC.md#interface) gives each one's rules, and [REST](rest.md) has the
same operations under `/v1/user-operations`. The endpoint isn't a node: other methods, such as
`eth_getUserOperationByHash` or `eth_call`, are refused, so give your client its own RPC URL for
reading the chain.

Each script is run as it is against both relays on a local chain, from an undeployed Kernel
v3.2, with the versions listed in [support](../support.md).
