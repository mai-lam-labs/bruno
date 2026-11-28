---
description: "Accept x402 payments on Ethereum with Bruno as your facilitator: your facilitator URL and receipt address, what a payment becomes on chain, the fee and how you're told it, the limits you can put in the URL, and what each refusal means"
task: "I want to accept x402 payments on Ethereum"
---

# Accepting x402 payments

For a merchant whose x402 server needs a facilitator on Ethereum. Bruno is one, at a URL you
make from the address you want paid. Your buyers pay as they pay any x402 resource (the
`exact` scheme, with EIP-3009). You need no ETH and no account with Bruno, and Bruno's fee
comes out of each payment as it settles.

## Set it up

1. Choose your **payout address**: where you want the money.
2. Your **facilitator URL** is `https://bruno.mailam.app/x402/facilitator/<payout address>/`.
   Give it to your x402 server as its facilitator. It answers x402 v2's `/supported`,
   `/verify` and `/settle`.
3. `GET` the URL to learn your **receipt address** on each chain:

    ```json
    {
      "payout": "0x…your payout address",
      "payTo": { "eip155:1": "0x…your receipt address on Ethereum" }
    }
    ```

4. Put the receipt address, not the payout address, in your payment requirements' `payTo`.
   Everything else is an ordinary `exact` requirement:

    ```json
    {
      "scheme": "exact",
      "network": "eip155:1",
      "amount": "1000000",
      "asset": "0x…the token",
      "payTo": "0x…your receipt address",
      "maxTimeoutSeconds": 120,
      "extra": { "name": "…the token's EIP-712 name", "version": "…its version" }
    }
    ```

A payment whose `payTo` isn't your receipt address is refused, so a buyer can't be pointed at
someone else's.

## What a payment becomes

The receipt address is a wallet Bruno holds the key to, one for each payout address. When
your server calls `/settle`, Bruno sends three transfers of the token:

1. the buyer's payment, to the receipt wallet;
2. the payment less the fee, from the receipt wallet to your payout address;
3. the fee, from the receipt wallet to Bruno.

For a buyer that signs with a key they are one transaction, so they happen together or not
at all. A buyer that's a deployed smart wallet has its transfer in a transaction of its own,
ahead of the other two in one bundle.

The first time a receipt wallet is paid in a token, it keeps one base unit of it (0.000001
USDC), which makes every later settlement cheaper. That comes out of your first payout.

## The fee

The fee is what the settlement's gas costs when `/settle` is called, in the token paid, with
Bruno's margin. It moves with Ethereum's gas price, and it's about the same whatever the
payment's size. A payment that doesn't exceed the fee is refused.

Bruno tells your server the fee in the `EXTENSION-RESPONSES` header of its answers to
`/verify` and `/settle`, which x402 keeps from the buyer. It's base64 of JSON, keyed by the
extension's name (the figures here are an example):

```json
{
  "bruno-fee": {
    "asset": "0x…the token",
    "amount": "1000000",
    "fee": "182000",
    "kept": "0",
    "payout": "818000"
  }
}
```

Each amount is in the token's base units, as a decimal string, and `fee`, `kept` and
`payout` add up to `amount`. On `/verify` it's the price now, as a quote. On `/settle` it's
what was charged. x402's reference server puts it on the result as `extensionResponses`. The
extension is listed in `/supported`'s `extensions` as `bruno-fee`.

## Limits you can set

Add terms to the URL's path, after the payout address, each as `name=value`:

```
https://bruno.mailam.app/x402/facilitator/<payout>/tokens=<token>,<token>/max-fee=<token>:<amount>/
```

- `tokens`: the tokens you take. A payment in any other is refused. Without it, every token
  Bruno settles.
- `max-fee`: the most the fee may be for a token, in its base units. A payment whose fee
  would be more isn't settled. Give several as `<token>:<amount>,<token>:<amount>`. A token
  with no cap settles at whatever the fee is.

Your receipt address is the same whatever the terms, so you can change them without changing
`payTo`. The terms limit what's done through that URL.

## When a payment isn't settled

`/settle` holds your request until the payment is included, for up to five blocks (about a
minute). If no block includes it by then, the answer is `success: false` with the reason
`bruno_not_included`: nothing moved, and the buyer's payment is unused. Send the same payment
to `/settle` again to try again, while its authorization is still valid.

Deliver what was bought once `/settle` answers `success: true`. Give buyers a
`maxTimeoutSeconds` of a couple of minutes or more: x402's reference client signs a payment
that's valid for that long, and a settlement can't outlast its payment.

A payment that was already settled is answered with the same transaction, so calling
`/settle` twice is safe. Only the first answer carries the fee.

## Why a payment is refused

`/verify` answers `isValid: false` with an `invalidReason`, and `/settle` answers
`success: false` with an `errorReason`. Most are x402's own. Four are Bruno's:

| Reason | What it means |
|---|---|
| `bruno_asset_not_accepted` | The token isn't one Bruno settles, or isn't in your `tokens` |
| `bruno_fee_over_max` | The fee is over your `max-fee` for the token |
| `bruno_payment_under_fee` | The payment doesn't exceed the fee |
| `bruno_not_included` | Not included within the wait. Nothing moved: send it again |
| `invalid_exact_evm_payload_recipient_mismatch` | The payment isn't to your receipt address |
| `invalid_exact_evm_payload_authorization_value_mismatch` | The amount signed isn't the amount required |
| `invalid_exact_evm_payload_signature` | The signature isn't the buyer's |
| `insufficient_funds` | The buyer holds less than the payment |
| `invalid_exact_evm_payload_undeployed_smart_wallet` | The buyer is a smart wallet that isn't deployed yet |
| `invalid_network` | The payment is on a chain Bruno doesn't settle on |

A facilitator URL that isn't one (a payout address that isn't an address, a term Bruno
doesn't know) is answered with status 400, naming the part at fault.

## What it takes, and what it doesn't

- **EIP-3009 only.** The token must take `transferWithAuthorization`, as `exact` on EVM has
  it by default. `/supported` lists the chains.
- **USDC today.** Bruno settles a token whose transfer it has measured and can give an exact
  gas limit. On Ethereum and Sepolia that's USDC.
- **Buyers with a key, or a deployed smart wallet.** A smart wallet that isn't deployed yet
  can't pay: it signs exactly the price, which leaves nothing to deploy it with.
- **Bruno holds the receipt key.** A payment only reaches you through a settlement Bruno
  makes. If someone lands a buyer's signed transfer on its own, outside a settlement, the
  money is in the receipt wallet and isn't paid out by itself.
