---
description: "An EOA (a plain private key) holding ETH sends ERC-4337 user operations through Bruno as ZeroDev's EIP-7702 Kernel account: createKernelAccount with eip7702Account (Kernel v3.3, EntryPoint v0.7), the client signing the EOA's authorization, the relay the bundler, no ZeroDev project ID"
---

# An EOA, with ZeroDev

For an EOA, a plain key, that holds ETH and has no smart account. ZeroDev's
`createKernelAccount`, given the key as `eip7702Account`, makes the EOA itself a Kernel v3.3
account on EntryPoint v0.7, by EIP-7702: its first op brings its authorization, delegating it
to Kernel, and the relay's transaction carries it. The client signs the authorization itself.
Later ops need none. The address stays the EOA's.

Give `createKernelAccountClient` the relay's chain endpoint (such as
`https://bruno.mailam.app/sepolia`) as its `bundlerTransport`. No ZeroDev project ID is needed:
it's only part of ZeroDev's own URLs.

The client asks the relay for its gas price (`zd_getUserOperationGasPrice`) by default, and the
op pays for its gas in ETH at that price, from the EOA's ETH. As at any bundler, the op pays for
its gas even if its calls fail. Its comment says which environment variables to set.

```ts
--8<-- "npm-userops/userop-zerodev-7702.ts"
```

It prints the op's hash, the account, whether it succeeded, its transaction and what it paid
in wei.

How an op pays, and the methods the relay answers: [ERC-4337 accounts](index.md).
