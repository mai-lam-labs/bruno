---
description: "An EOA (a plain private key) holding ETH sends ERC-4337 user operations through Bruno as an EIP-7702 smart account with permissionless.js: to7702SimpleSmartAccount on EntryPoint v0.8, its first operation bringing the EOA's authorization, the relay's URL the smart account client's bundler transport and nothing else, no Pimlico API key"
---

# An EOA, with permissionless.js

For an EOA, a plain key, that holds ETH and has no smart account. permissionless.js's
`to7702SimpleSmartAccount` makes the EOA itself an ERC-4337 account on EntryPoint v0.8, by
EIP-7702: its first op brings its authorization, delegating it to eth-infinitism's
Simple7702Account, and the relay's transaction carries it. Later ops need none. The address
stays the EOA's.

Set `createSmartAccountClient` up with the relay's chain endpoint (such as
`https://bruno.mailam.app/sepolia`) as its `bundlerTransport`, and nothing else: no Pimlico API
key or paymaster is needed. permissionless.js doesn't sign the authorization itself, so the
script signs it while the EOA isn't delegated yet, and gives it to `sendUserOperation`.

The op pays for its gas in ETH, at the fees viem gives it, from the EOA's ETH: the relay's
estimate sets its `preVerificationGas` so that what the EntryPoint charges comes to the relay's
price. As at any bundler, the op pays for its gas even if its calls fail. Its comment says which
environment variables to set.

```ts
--8<-- "npm-userops/userop-permissionless-7702.ts"
```

It prints the op's hash, the account, whether it succeeded, its transaction and what it paid
in wei.

How an op pays, and the methods the relay answers: [ERC-4337 accounts](index.md).
