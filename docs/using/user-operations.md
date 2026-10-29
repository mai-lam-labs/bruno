---
description: "Sending ERC-4337 user operations through Bruno with viem's bundler client or ZeroDev's Kernel account client, the relay's endpoint as the bundler: holding ETH, paying by the op's fees as at any bundler, from a smart account or an EOA (EIP-7702, viem's Simple7702 account); holding only a token, paying by the op's own calls"
---

# ERC-4337 accounts

Each chain's [JSON-RPC endpoint](json-rpc.md) answers ERC-7769's bundler methods for EntryPoint
v0.8 and v0.7, so an ERC-4337 library can use it as its bundler: viem's bundler client, ZeroDev's Kernel
account client, or anything built on them. No API key, paymaster or bundler account is needed.

An op pays the relay one of two ways:

- **Holding ETH, by its fees, as at any bundler.** Set the client up as for any bundler, with
  the relay as its URL. Whatever fees the client gives the op, the relay's estimate sets its
  `preVerificationGas` so that what it's charged comes to the relay's price. Clients that ask
  their bundler for a gas price get it as Pimlico's and ZeroDev's bundlers answer it
  (`pimlico_getUserOperationGasPrice`, `zd_getUserOperationGasPrice`).
- **Holding no ETH, by its own calls.** Its fees are zero, so the account needs no ETH, and its
  last call transfers a token the relay takes, such as USDC, to the relay's `payee` (from
  [the terms](rest.md#terms)). If that's too little, sending fails as underpaid with the amount
  required in the error's `data.required`, and you sign again with that amount.

As with any submission, nothing is included unless the payee receives the payment, and an op
that isn't included costs nothing. An op paying by its fees pays for its gas even if its calls
fail, as at any bundler: the EntryPoint charges it. An op paying by its calls pays only if they
succeed, since a failing call undoes all of them, the payment included, in Kernel, Coinbase
Smart Wallet, Safe and Light Account alike. An account that carries on past a failing call, as
ERC-7579's "try" mode does, still pays.

The account can be undeployed: the op's factory and factory data deploy it, as usual. Or it can
be an EOA, delegating to a smart account's code by EIP-7702 in its first op. The scripts below
make a ZeroDev Kernel, or viem's Simple7702 account from an EOA; other smart accounts should work
the same way, but only these are tested. Each runs on Node 24 as it is, and its comment says which environment
variables to set.

## Holding ETH

### viem

viem's `createBundlerClient`, the relay as its transport and nothing else:

```ts
--8<-- "npm-test/userop-viem-eth.ts"
```

### ZeroDev

ZeroDev's `createKernelAccountClient`, the relay as its `bundlerTransport`: it asks the relay
for its fees by default. No ZeroDev project ID is needed: it's only part of ZeroDev's own URLs.

```ts
--8<-- "npm-test/userop-zerodev-eth.ts"
```

### An EOA, with viem

viem's `toSimple7702SmartAccount` makes the EOA an ERC-4337 account on EntryPoint v0.8, and the
bundler client is set up the same way. viem doesn't sign the EOA's authorization itself, so the
script signs it while the EOA isn't delegated yet, and gives it to `sendUserOperation`; the relay's
transaction carries it.

```ts
--8<-- "npm-test/userop-viem-7702.ts"
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
| `eth_estimateUserOperationGas` | Simulates the op with the account's stub signature, and returns its gas limits with a tenth added; `preVerificationGas` zero for an op paying by its calls, and for one paying by its fees what makes its charge, at its fees, the relay's price |
| `pimlico_getUserOperationGasPrice`, `zd_getUserOperationGasPrice` | The price an op paying by its fees is asked, as both its fees |
| `eth_sendUserOperation` | Takes the signed op as a submission, and returns its hash |
| `eth_getUserOperationReceipt` | ERC-7769's receipt, or null until the op is included |
| `eth_supportedEntryPoints` | EntryPoint v0.8's and v0.7's addresses |
| `eth_chainId` | The chain's ID |

The [specification](../SPEC.md#interface) gives each one's rules, and [REST](rest.md) has the
same operations under `/v1/user-operations`. The endpoint isn't a node: other methods, such as
`eth_getUserOperationByHash` or `eth_call`, are refused, so give your client its own RPC URL for
reading the chain.

Each script is run as it is against both relays on a local chain, from an undeployed Kernel
v3.2 or a fresh EOA, with the versions listed in [support](../support.md).
