---
description: "Send your own transactions from an account that holds a token and no ETH, from Python over Bruno's REST API with no client library: a whole script with httpx and eth-account that quotes a sponsorship, checks what it's given to sign, signs it without sending and submits it"
---

# Sponsored, from Python over REST

For an account with a private key that holds a token Bruno takes, such as USDC, and no ETH,
from Python or any language without a Bruno client. It's the same sponsorship as
[with the TypeScript client](index.md), done as three HTTP requests and two signatures.

The script uses httpx for the requests and eth-account to sign. Its comment says which
environment variables to set.

```python
--8<-- "docs/using/sponsorship/sponsor.py"
```

It prints the account, the status, the hashes of the four transactions that landed (the
relay's first, the account's transfer, the account's payment, and the relay's last) and what
it paid, in the token's smallest unit.

The script does by hand what the TypeScript client's `sponsor` does:

1. It reads the relay's forwarder from `/v1/config`, on its own, so that it can check the quote
   against it.
2. It asks for a quote. The answer has the transfer filled in as a transaction to sign, with
   its nonce, gas and fees, and the payment to sign after it.
3. It checks what it was given before signing anything: the transfer is the one it asked for,
   and the payment is a transfer of the quoted price to the forwarder, in the same token, at
   the next nonce, and both are for the chain it means. A signed transaction stays valid, so
   the chain matters. Don't skip this. The relay fills in what you sign, so your code is what
   makes sure it's only what you asked for.
4. It signs both with `sign_transaction`, which doesn't send them, and submits them.
5. It reads the status until the sponsorship is in a block or has failed, for ten minutes at
   most.

The requests and their fields are on the
[REST page](../rest.md#sponsoring-an-accounts-own-transactions). The transfer here is left for
the quote to give its gas, which suits a transfer. For a call that does more, give its `gas`
([the gas of your transactions](../rest.md#the-gas-of-your-transactions)).

What the account needs, and what keeps the bundle whole, are the same as
[with the client](index.md#what-you-need).
