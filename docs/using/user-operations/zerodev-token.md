---
description: "ZeroDev's createKernelAccountClient with Bruno as the bundler, no ZeroDev project ID: a Kernel account holding no ETH, only a token such as USDC, sends a user operation whose fees are zero and whose last call pays the relay in that token"
---

# ZeroDev, paying in a token

For a ZeroDev Kernel account that holds no ETH, only a token the relay takes, such as USDC,
sending with `createKernelAccountClient`, the relay's chain endpoint (such as
`https://bruno.mailam.app/sepolia`) as its `bundlerTransport`. No ZeroDev project ID is needed:
it's only part of ZeroDev's own URLs.

The op's fees are zero, so the account needs no ETH: the relay pays the gas. Giving the fees
also skips the one method the client would otherwise ask the relay for them. Its last call pays
the relay's `payee` in the token, the payee read from the relay's `/v1/config`. The first
attempt pays one unit, so it fails as underpaid, the amount required in the error's
`data.required`; the second signs again at that, and normally succeeds. It can still fail if the
price rose in between, which is why the script allows a few attempts. The op pays only if its
calls succeed. An undeployed account is deployed by the op.

The script makes a Kernel v3.2. Its comment says which environment variables to set.

```ts
--8<-- "npm-test/userop-zerodev.ts"
```

It prints the op's hash, the account, whether it succeeded, its transaction and what it paid
in the token's smallest unit.

Holding ETH? See [ZeroDev, paying in ETH](zerodev-eth.md). How an op pays, and the methods the
relay answers: [ERC-4337 accounts](index.md).
