---
description: "ZeroDev's createKernelAccountClient with Bruno as the bundler, no ZeroDev project ID: a Kernel account holding ETH sends a user operation that pays for its gas in ETH, by its fees, at the gas price the relay answers"
---

# ZeroDev, paying in ETH

For a ZeroDev Kernel account that holds ETH, sending with `createKernelAccountClient`. Give it
the relay's chain endpoint (such as `https://bruno.mailam.app/sepolia`) as its
`bundlerTransport`. No ZeroDev project ID is needed: it's only part of ZeroDev's own URLs.

The client asks the relay for its gas price (`zd_getUserOperationGasPrice`) by default, and the
op pays for its gas in ETH at that price, from the account's ETH. An undeployed account is
deployed by the op. As at any bundler, the op pays for its gas even if its calls fail.

The script makes a Kernel v3.2. Its comment says which environment variables to set.

```ts
--8<-- "npm-test/userop-zerodev-eth.ts"
```

It prints the op's hash, the account, whether it succeeded, its transaction and what it paid
in wei.

Holding only a token? See [ZeroDev, paying in a token](zerodev-token.md). How an op pays, and
the methods the relay answers: [ERC-4337 accounts](index.md).
