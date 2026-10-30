---
description: "An EOA holding only USDC, no ETH, sends ERC-4337 user operations through Bruno with viem, its gas paid in USDC through Circle's paymaster (EntryPoint v0.8, an EIP-2612 permit): Circle's quickstart with Bruno as the bundler"
---

# USDC through Circle's paymaster, with viem

For an account that holds USDC and no ETH, on a chain where Circle's paymaster runs, such as
Ethereum and Sepolia. This is Circle's own quickstart, with the relay's chain endpoint (such as
`https://bruno.mailam.app/sepolia`) as the bundler. The account is an EOA made an ERC-4337
account by EIP-7702 (viem's `toSimple7702SmartAccount`, EntryPoint v0.8), its first op
delegating it.

The op's paymaster data carries an EIP-2612 permit letting Circle's paymaster take USDC: here,
all the account can spare. Circle takes a prefund of the op's whole `maxCost` in USDC before
its calls and refunds what the op didn't use, so the permit must cover the prefund, not only
the cost. Circle's quickstart permits 10 USDC, which a first op from an EOA can be over. The
paymaster pays the op's gas from its deposit in the EntryPoint, which pays the relay in ETH,
and takes its price in USDC from the account. The op's fees are the relay's gas price
(`pimlico_getUserOperationGasPrice`, as Circle's quickstart asks Pimlico's bundler).

Circle's paymaster charges an op whose calls fail as one that succeeds. Its postOp limit is
given as Circle gives it, 35,000: it refuses one below its own gas charge. Its validation's is
left to the relay's estimate: Circle's quickstart fixes it at 200,000, too little where a new
storage slot costs more (EIP-8037, from Glamsterdam) for an account's first permit, which sets
a new allowance. Its comment says which environment variables to set, Circle's paymaster's
address among them.

```ts
--8<-- "npm-userops/userop-viem-circle.ts"
```

It prints the op's hash, the account, whether it succeeded, its transaction and what the
paymaster paid the relay, in wei.

Without a paymaster, an account holding only a token pays the relay in it directly: see
[viem, paying in a token](viem-token.md). How an op pays, and the methods the relay answers:
[ERC-4337 accounts](index.md).
