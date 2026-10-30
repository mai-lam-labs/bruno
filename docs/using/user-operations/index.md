---
description: "Bruno as an ERC-4337 bundler for smart accounts and EIP-7702 EOAs, with no API key: how a user operation pays the relay (in ETH by its fees, through another's paymaster such as Circle's, or in a token by its own calls at a price quoted before signing), which page to read for viem or ZeroDev, and the bundler methods answered"
---

# ERC-4337 accounts

Each chain's [JSON-RPC endpoint](../json-rpc.md), such as `https://bruno.mailam.app/sepolia`,
answers ERC-7769's bundler methods for EntryPoint v0.8 and v0.7. An ERC-4337 library can use it
as its bundler: viem's bundler client, ZeroDev's Kernel account client, or anything built on
them. No API key or bundler account is needed, nor a paymaster, though an op may use one.

## How an op pays

An op pays the relay one of three ways:

- **In ETH, by its fees, as at any bundler.** Set the client up as for any bundler, with the
  relay as its URL. Whatever fees the client gives the op, the relay's estimate sets its
  `preVerificationGas` so that what it's charged comes to the relay's price. Clients that ask
  their bundler for a gas price get it as Pimlico's and ZeroDev's bundlers answer it
  (`pimlico_getUserOperationGasPrice`, `zd_getUserOperationGasPrice`).
- **Through another's paymaster, by its fees.** The paymaster pays the op's gas from its
  deposit in the EntryPoint, which pays the relay in ETH; the paymaster takes its price from
  the account its own way, such as Circle's in USDC. Give the paymaster's gas limits as it gives
  them; left out, the relay's estimate measures them, with room to spare. Bruno has no paymaster
  of its own.
- **In a token, by its own calls, holding no ETH.** Its fees are zero, so the account needs no
  ETH, and its last call transfers a token the relay takes, such as USDC, to the relay's `payee`
  (from [the terms](../rest.md#terms)). ERC-4337 has no standard way to pay a bundler in a token,
  so this takes a few lines beyond the bundler URL. Before signing, prepare the op with a
  placeholder payment of one unit, and ask the relay its price for it with
  `postpaid_quoteUserOperation`. Then send it paying the quoted amount: the client estimates and
  signs it once. The quote holds until its `validUntilBlock`. If it's sent later and the price
  has risen, sending fails as underpaid, with the amount required in the error's
  `data.required`.

As with any submission, nothing is included unless the payee receives the payment, and an op
that isn't included costs nothing. An op paying by its fees pays for its gas even if its calls
fail, as at any bundler: the EntryPoint charges it, or its paymaster. An op paying by its calls pays only if they
succeed, since a failing call undoes all of them, the payment included, in Kernel, Coinbase
Smart Wallet, Safe and Light Account alike. An account that carries on past a failing call, as
ERC-7579's "try" mode does, still pays.

The account can be undeployed: the op's factory and factory data deploy it, as usual. Or it can
be an EOA, delegating to a smart account's code by EIP-7702 in its first op.

## Which page

Each page has one script, complete and tested as it is, for Node 24.

| Your account holds | Your library | Page |
|---|---|---|
| ETH | viem, any smart account | [viem, paying in ETH](viem-eth.md) |
| ETH | ZeroDev's Kernel | [ZeroDev, paying in ETH](zerodev-eth.md) |
| ETH, in an EOA (no smart account yet) | viem | [An EOA, with viem](viem-eoa.md) |
| ETH, in an EOA (no smart account yet) | ZeroDev | [An EOA, with ZeroDev](zerodev-eoa.md) |
| ETH, in an EOA (no smart account yet) | MetaMask's Smart Accounts Kit | [An EOA, with MetaMask's Smart Accounts Kit](metamask-eoa.md) |
| ETH, in an EOA (no smart account yet) | Alchemy's smart accounts | [An EOA, with Alchemy's smart accounts](alchemy-eoa.md) |
| Only a token, such as USDC | viem, any smart account | [viem, paying in a token](viem-token.md) |
| Only a token, such as USDC | ZeroDev's Kernel | [ZeroDev, paying in a token](zerodev-token.md) |
| Only a token, in an EOA (no smart account yet) | viem | [An EOA paying in a token, with viem](viem-eoa-token.md) |
| Only USDC, in an EOA, using Circle's paymaster | viem | [USDC through Circle's paymaster](viem-circle.md) |

The scripts make a ZeroDev Kernel v3.2, or from an EOA viem's Simple7702 account, ZeroDev's
Kernel v3.3, MetaMask's Stateless7702 or Alchemy's Modular Account v2; other smart accounts
should work the same way, but only these are tested. The scripts are tested with the current
versions of their libraries, viem included. AgentKit's
`ZeroDevWalletProvider` sends through ZeroDev's intents, not a bundler, so with AgentKit, use
the [AgentKit provider](../agentkit.md) instead.

## Methods

| Method | What it does |
|---|---|
| `eth_estimateUserOperationGas` | Simulates the op with the account's stub signature, and returns its gas limits with a tenth added, its paymaster's too; `preVerificationGas` zero for an op paying by its calls, and for one paying by its fees what makes its charge, at its fees, the relay's price |
| `pimlico_getUserOperationGasPrice`, `zd_getUserOperationGasPrice` | The price an op paying by its fees is asked, as both its fees |
| `postpaid_quoteUserOperation` | Bruno's own: what an op paying by its calls is to pay, in each token the relay takes, quoted before it's signed with its payment a placeholder |
| `eth_sendUserOperation` | Takes the signed op as a submission, and returns its hash |
| `eth_getUserOperationReceipt` | ERC-7769's receipt, or null until the op is included |
| `eth_supportedEntryPoints` | EntryPoint v0.8's and v0.7's addresses |
| `eth_chainId` | The chain's ID |

The [specification](../../SPEC.md#interface) gives each one's rules, and [REST](../rest.md) has
the same operations under `/v1/user-operations`. The endpoint isn't a node: other methods, such
as `eth_getUserOperationByHash` or `eth_call`, are refused, so give your client its own RPC URL
for reading the chain.

Each script is run as it is against both relays on a local chain, from an undeployed Kernel
v3.2 or a fresh EOA, with the versions listed in [support](../../support.md).
