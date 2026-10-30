---
description: "viem's createBundlerClient with Bruno as the bundler: a smart account (a ZeroDev Kernel here) holding no ETH, only a token such as USDC, sends a user operation whose fees are zero and whose last call pays the relay in that token, at a price quoted before it's signed"
---

# viem, paying in a token

For a smart account that holds no ETH, only a token the relay takes, such as USDC, sending with
viem's `createBundlerClient`, the relay's chain endpoint (such as
`https://bruno.mailam.app/sepolia`) as its transport. No API key or paymaster is needed.

The op's fees are zero, so the account needs no ETH: the relay pays the gas. Its last call pays
the relay's `payee` in the token, the payee read from the relay's `/v1/config`. The op pays only
if its calls succeed. An undeployed account is deployed by the op.

Before signing, the script prepares the op with a placeholder payment of one unit, which signs
nothing, and asks the relay its price for it with `postpaid_quoteUserOperation`. Then it sends
the op paying the quoted amount, so the op is signed once. The quote holds until its
`validUntilBlock`; sent later, an op whose price has risen fails as underpaid, the amount
required in the error's `data.required`, and is quoted again.

The script makes a ZeroDev Kernel v3.2 with `@zerodev/sdk`; any viem smart account is used the
same way. Its comment says which environment variables to set.

```ts
--8<-- "npm-test/userop-viem.ts"
```

It prints the op's hash, the account, whether it succeeded, its transaction and what it paid
in the token's smallest unit.

Holding ETH? See [viem, paying in ETH](viem-eth.md). How an op pays, and the methods the relay
answers: [ERC-4337 accounts](index.md).
