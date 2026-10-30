---
description: "An EOA (a plain private key) holding ETH sends ERC-4337 user operations through Bruno as MetaMask's EIP-7702 smart account: the Smart Accounts Kit's toMetaMaskSmartAccount with Implementation.Stateless7702 (EntryPoint v0.7), the script signing the EOA's authorization, viem's bundler client with the relay's URL"
---

# An EOA, with MetaMask's Smart Accounts Kit

For an EOA, a plain key, that holds ETH and has no smart account. MetaMask's Smart Accounts Kit
(`@metamask/smart-accounts-kit`), with `Implementation.Stateless7702`, makes the EOA itself an
ERC-4337 account on EntryPoint v0.7, by EIP-7702: its first op brings its authorization,
delegating it to MetaMask's `EIP7702StatelessDeleGator`, and the relay's transaction carries it.
Later ops need none. The address stays the EOA's.

Set viem's bundler client up as for any bundler, with the relay's chain endpoint (such as
`https://bruno.mailam.app/sepolia`) as its transport and nothing else. Two things the script
does that the kit doesn't:

- **The authorization.** The kit doesn't sign it, so the script signs it while the EOA isn't
  delegated yet, and gives it to `sendUserOperation`.
- **No factory.** Until the EOA is delegated, the kit refuses to name a factory. An EIP-7702
  account has none, so the script's account answers `getFactoryArgs` with nothing.

The kit names MetaMask's contracts by chain, and refuses a chain it doesn't list; the script
takes Ethereum's, which are at the same addresses on Sepolia. The kit also reports its use to
MetaMask unless `DO_NOT_TRACK` (or `CI`) is set.

The op pays for its gas in ETH, at the fees viem gives it, from the EOA's ETH: the relay's
estimate sets its `preVerificationGas` so that what the EntryPoint charges comes to the relay's
price. As at any bundler, the op pays for its gas even if its calls fail. Its comment says which
environment variables to set.

```ts
--8<-- "npm-userops/userop-metamask-7702.ts"
```

It prints the op's hash, the account, whether it succeeded, its transaction and what it paid
in wei.

How an op pays, and the methods the relay answers: [ERC-4337 accounts](index.md).
