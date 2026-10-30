---
description: "An EOA (a plain private key) holding ETH sends ERC-4337 user operations through Bruno as an EIP-7702 smart account: viem's toSimple7702SmartAccount on EntryPoint v0.8, its first operation bringing the EOA's authorization, the relay's URL the bundler client's only setting"
---

# An EOA, with viem

For an EOA, a plain key, that holds ETH and has no smart account. viem's
`toSimple7702SmartAccount` makes the EOA itself an ERC-4337 account on EntryPoint v0.8, by
EIP-7702: its first op brings its authorization, delegating it to eth-infinitism's
Simple7702Account, and the relay's transaction carries it. Later ops need none. The address
stays the EOA's.

Set the bundler client up as for any bundler, with the relay's chain endpoint (such as
`https://bruno.mailam.app/sepolia`) as its transport and nothing else. viem doesn't sign the
authorization itself, so the script signs it while the EOA isn't delegated yet, and gives it to
`sendUserOperation`.

The op pays for its gas in ETH, at the fees viem gives it, from the EOA's ETH: the relay's
estimate sets its `preVerificationGas` so that what the EntryPoint charges comes to the relay's
price. As at any bundler, the op pays for its gas even if its calls fail. Its comment says which
environment variables to set.

```ts
--8<-- "npm-userops/userop-viem-7702.ts"
```

It prints the op's hash, the account, whether it succeeded, its transaction and what it paid
in wei.

Holding only a token? See [An EOA paying in a token, with viem](viem-eoa-token.md). How an op
pays, and the methods the relay answers: [ERC-4337 accounts](index.md).
