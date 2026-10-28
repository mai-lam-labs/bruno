---
description: "Sending ERC-4337 user operations through Bruno with viem's bundler client or ZeroDev's Kernel account client: the relay's endpoint as the bundler, zero fees, and the op paying the relay in its own calls"
---

# ERC-4337 accounts

Each chain's [JSON-RPC endpoint](json-rpc.md) answers ERC-7769's bundler methods for EntryPoint
v0.7, so an ERC-4337 library can use it as its bundler: viem's bundler client, ZeroDev's Kernel
account client, or anything built on them. No API key, paymaster or bundler account is needed.

Two things differ from a usual bundler:

- **The op's fees are zero.** The relay pays the gas, so the account needs no ETH and no
  deposit, and there's no paymaster.
- **The op pays the relay in its own calls.** Its last call transfers a token the relay takes,
  such as USDC, to the relay's `payee` (from [the terms](rest.md#terms)). The relay simulates
  the op to see what it pays. If that's too little, sending fails as underpaid with the amount
  required in the error's `data.required`, and you sign again with that amount.

Both are in the `sendUserOperation` call; the client is set up as for any bundler.

As with any submission, nothing is included unless the payee receives the payment, and an op
that isn't included costs nothing. Kernel reverts a whole batch if any of its calls fails, so
an op whose calls fail takes its payment back with it and isn't included. Other accounts'
batches haven't been checked yet.

The account can be undeployed: the op's factory and factory data deploy it, as usual.

## viem

This script sends a token from a ZeroDev Kernel account with viem's `createBundlerClient`, the
relay as its transport. Other viem smart accounts should work the same way, but only Kernel is
tested. It runs on Node 24 as it is, and its comment says which environment variables to set.

```ts
--8<-- "npm-test/userop-viem.ts"
```

The first attempt pays one unit, so it fails as underpaid and tells the script the price. The
second attempt pays that price and normally succeeds. It can still fail if the price rose in
between, which is why the script allows a few attempts.

## ZeroDev

The same with ZeroDev's `createKernelAccountClient`, the relay as its `bundlerTransport`. No
ZeroDev project ID is needed: it's only part of ZeroDev's own URLs. Giving the op's fees
skips the one ZeroDev-specific method the client would otherwise call for them.

```ts
--8<-- "npm-test/userop-zerodev.ts"
```

AgentKit's `ZeroDevWalletProvider` sends through ZeroDev's intents, not a bundler, so with
AgentKit, use the [AgentKit provider](agentkit.md) instead.

## Methods

| Method | What it does |
|---|---|
| `eth_estimateUserOperationGas` | Simulates the op with the account's stub signature, and returns its gas limits with a tenth added, `preVerificationGas` zero |
| `eth_sendUserOperation` | Takes the signed op as a submission, and returns its hash |
| `eth_getUserOperationReceipt` | ERC-7769's receipt, or null until the op is included |
| `eth_supportedEntryPoints` | EntryPoint v0.7's address |
| `eth_chainId` | The chain's ID |

The [specification](../SPEC.md#interface) gives each one's rules, and [REST](rest.md) has the
same operations under `/v1/user-operations`. The endpoint isn't a node: other methods, such as
`eth_getUserOperationByHash` or `eth_call`, are refused, so give your client its own RPC URL for
reading the chain.

Both scripts are run as they are against both relays on a local chain, from an undeployed
Kernel v3.2 holding only USDC, with the versions listed in [support](../support.md).
