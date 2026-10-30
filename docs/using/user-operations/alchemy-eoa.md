---
description: "An EOA (a plain private key) holding ETH sends ERC-4337 user operations through Bruno as Alchemy's EIP-7702 smart account: @alchemy/smart-accounts' toModularAccountV2 in 7702 mode (Modular Account v2, EntryPoint v0.7), the script signing the EOA's authorization, viem's bundler client with the relay's URL, no Alchemy API key"
---

# An EOA, with Alchemy's smart accounts

For an EOA, a plain key, that holds ETH and has no smart account. Alchemy's
`@alchemy/smart-accounts`, with `toModularAccountV2` in `"7702"` mode, makes the EOA itself a
Modular Account v2 on EntryPoint v0.7, by EIP-7702: its first op brings its authorization,
delegating it to Alchemy's `SemiModularAccount7702` (v1.0.0, the package's default), and the
relay's transaction carries it. The EOA signs for itself. Later ops need none. The address stays
the EOA's.

Set viem's bundler client up as for any bundler, with the relay's chain endpoint (such as
`https://bruno.mailam.app/sepolia`) as its transport and nothing else: no Alchemy API key is
needed. viem only stubs the authorization, to estimate the op, so the script signs it while the
EOA isn't delegated yet, and gives it to `sendUserOperation`. The package needs viem 2.45 or
later.

The op pays for its gas in ETH, at the fees viem gives it, from the EOA's ETH: the relay's
estimate sets its `preVerificationGas` so that what the EntryPoint charges comes to the relay's
price. As at any bundler, the op pays for its gas even if its calls fail. Its comment says which
environment variables to set.

```ts
--8<-- "npm-userops/userop-alchemy-7702.ts"
```

It prints the op's hash, the account, whether it succeeded, its transaction and what it paid
in wei.

How an op pays, and the methods the relay answers: [ERC-4337 accounts](index.md).
