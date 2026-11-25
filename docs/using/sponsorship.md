---
description: "Send your own transactions from an account that holds a token and no ETH: a whole script with @mailam/bruno and viem that has a transfer sponsored, the relay funding the account and paid in the same token; what the wallet must do, paying in ETH, and what keeps the bundle whole"
---

# Your own transactions, sponsored

For an account with a private key that holds a token Bruno takes, such as USDC, and no ETH. A
sponsorship sends ordinary transactions from the account itself: Bruno sends the account the
ETH they need, sends them as the account signed them, and is paid in the token by one more
transfer the account signs. All of it goes to block builders as one bundle, included whole or
not at all. The account needs no smart account and no delegate, and its address is the
`msg.sender` of each call.

The script below sends a token from such an account. It uses the
[TypeScript client](typescript.md) and viem. Its comment says which environment variables to
set.

```ts
--8<-- "npm-test/sponsor.ts"
```

It prints the account, the status, the hashes of the four transactions that landed (the
relay's first, the account's transfer, the account's payment, and the relay's last) and what
it paid, in the token's smallest unit.

`bruno.sponsor` does three things, which you can also do one at a time
([quote, sign and submit yourself](typescript.md#quote-sign-and-submit-yourself)) or without
the client, over [REST](rest.md#sponsoring-an-accounts-own-transactions) or
[JSON-RPC](json-rpc.md#sponsoring-an-accounts-transactions):

1. It asks the relay what the calls cost. The answer has each call filled in as a transaction
   to sign, and the payment to sign after them.
2. It checks that what it was given to sign is the calls you gave, on your chain, and a
   transfer of the price to the relay's forwarder and nothing else. Then it has the wallet sign
   each one without sending it. A price over `max` is refused before anything is signed.
3. It submits the signed transactions.

## What you need

- **A wallet that signs without sending.** viem's local accounts do (`privateKeyToAccount`,
  `mnemonicToAccount`). A browser wallet's account sends what it signs, and can't be used.
- **The token, not ETH.** The account pays out of the token it holds. To pay in ETH instead,
  the account must already hold the price in ETH: Bruno sends it the ETH for gas, not the
  payment.
- **An unused nonce.** The transactions are signed at the account's next nonces. One
  sponsorship of an account is pending at a time.
- **Gas for anything but a simple call.** Left to the relay, a call's gas is what it used in a
  simulation and a tenth more, which suits a transfer or an approval. Give `gas` on a call that
  does more, or that depends on an earlier call
  ([the gas of your transactions](rest.md#the-gas-of-your-transactions)).
- **Transactions that send little or no ETH.** Bruno sends the account what its transactions
  need, and all of it together is capped at about what one large transaction's gas costs. A
  transaction sending more ETH than that is refused with `sponsored-over`.

## What keeps it whole

A builder keeps the bundle whole, not the chain. If a builder includes only part of it, Bruno
stops and the status is `rejected` with the reason `bundle-broken`. A signed transaction also
stays valid for as long as its nonce is unused, whatever the deadline. If your calls must
never be left half done, make them one call instead. The REST page
[explains both](rest.md#what-whole-or-not-at-all-depends-on).
