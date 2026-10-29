---
description: "viem's createBundlerClient with Bruno as the bundler, the relay's URL its only setting: a smart account (a ZeroDev Kernel here) holding ETH sends a user operation that pays for its gas in ETH, by its fees, as at any bundler"
---

# viem, paying in ETH

For a smart account that holds ETH, sending with viem's `createBundlerClient`. Set the client up
as for any bundler, with the relay's chain endpoint (such as `https://bruno.mailam.app/sepolia`)
as its transport and nothing else. No API key or paymaster is needed.

The op pays for its gas in ETH, at the fees viem gives it: the relay's estimate sets its
`preVerificationGas` so that what the EntryPoint charges comes to the relay's price. The payment
comes from the account's ETH, and an undeployed account is deployed by the op. As at any
bundler, the op pays for its gas even if its calls fail.

The script makes a ZeroDev Kernel v3.2 with `@zerodev/sdk`; any viem smart account is used the
same way. Its comment says which environment variables to set.

```ts
--8<-- "npm-test/userop-viem-eth.ts"
```

It prints the op's hash, the account, whether it succeeded, its transaction and what it paid
in wei. The account needs no token for itself: here it sends one, to show a call.

Holding only a token? See [viem, paying in a token](viem-token.md). How an op pays, and the
methods the relay answers: [ERC-4337 accounts](index.md).
