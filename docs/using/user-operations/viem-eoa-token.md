---
description: "An EOA (a plain private key) holding no ETH, only a token such as USDC, sends ERC-4337 user operations through Bruno as an EIP-7702 smart account (viem's toSimple7702SmartAccount, EntryPoint v0.8), its fees zero and its last call paying the relay in that token, at a price quoted before it's signed"
---

# An EOA paying in a token, with viem

For an EOA, a plain key, that holds no ETH, only a token the relay takes, such as USDC, and has
no smart account. viem's `toSimple7702SmartAccount` makes the EOA itself an ERC-4337 account on
EntryPoint v0.8, by EIP-7702: its first op brings its authorization, delegating it to
eth-infinitism's Simple7702Account, and the relay's transaction carries it. viem doesn't sign
the authorization itself, so the script signs it while the EOA isn't delegated yet. The address
stays the EOA's.

The op's fees are zero, so the EOA needs no ETH: the relay pays the gas. Its last call pays the
relay's `payee` in the token, the payee read from the relay's `/v1/config`. The op pays only if
its calls succeed.

Before signing, the script prepares the op with a placeholder payment of one unit, which signs
nothing, and asks the relay its price for it with `postpaid_quoteUserOperation`. Then it sends
the op paying the quoted amount, so the op is signed once. The quote holds until its
`validUntilBlock`; sent later, an op whose price has risen fails as underpaid, the amount
required in the error's `data.required`, and is quoted again.

Its comment says which environment variables to set.

```ts
--8<-- "npm-test/userop-viem-7702-token.ts"
```

It prints the op's hash, the account, whether it succeeded, its transaction and what it paid
in the token's smallest unit.

Holding ETH? See [An EOA, with viem](viem-eoa.md). How an op pays, and the methods the relay
answers: [ERC-4337 accounts](index.md).
