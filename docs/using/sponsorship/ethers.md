---
description: "Send your own transactions from an account that holds a token and no ETH, signing with ethers: a whole script with @mailam/bruno and an ethers v6 Wallet that has a transfer sponsored, with the few lines that make a Wallet the client's signer"
---

# Sponsored transactions with ethers v6

For an account with a private key that holds a token Bruno takes, such as USDC, and no ETH,
in a project that uses ethers. It's the same sponsorship as
[with viem](index.md): the account signs its transactions and one more that pays, and Bruno
sends them between two of its own.

The [TypeScript client](../typescript.md) doesn't depend on any wallet library. All it asks of
a signer is the account's address and a function that takes a transaction and returns it
signed, as raw bytes, without sending it. An ethers `Wallet` does that with
`signTransaction`, so the signer is a few lines, which the script has in full. Its comment says
which environment variables to set.

```ts
--8<-- "npm-userops/sponsor-ethers.ts"
```

It prints the account, the status, the hashes of the four transactions that landed (the
relay's first, the account's transfer, the account's payment, and the relay's last) and what
it paid, in the token's smallest unit.

The `Wallet` is made without a provider, so it can only sign. `bruno.sponsor` checks what it's
about to have signed before the wallet sees it: the calls you gave, on your chain, and a
transfer of the price to the relay's forwarder, and nothing else.

What the account needs, and what keeps the bundle whole, are the same as
[with viem](index.md#what-you-need).
