---
description: "Plan: what's left of the hosted x402 facilitator, the merchant-run adapter example, and earlier fee research"
---

# Plan: Bruno as an x402 facilitator

The hosted facilitator is built, on both relays: a merchant's URL from its payout address,
x402 v2's `/supported`, `/verify` and `/settle` under it, and a settlement of three EIP-3009
transfers through a receipt wallet. What a merchant does is in
[Accepting x402 payments](../../using/facilitator.md), and why it's built this way is on its
[design page](../design/facilitator.md). This page lists what isn't built.
[x402 v2 facilitator interface](https://github.com/x402-foundation/x402/blob/main/specs/x402-specification-v2.md#7-facilitator-interface)

## Not built

- **A page for a payout address.** `GET` of a facilitator URL answers JSON. A person setting
  one up wants a page: the receipt address, the terms the URL carries, the fee now, and the
  steps.
- **A stock merchant and a stock buyer, end to end.** The tests speak x402's `/verify` and
  `/settle` directly. A script with x402's own server middleware and client, run as the
  other user docs' scripts are, would show the integration a merchant makes, and that the
  reference server hands the merchant the fee from `EXTENSION-RESPONSES`.
- **Buyers that are smart wallets not deployed yet.** Refused now. The buyer signs exactly
  the price, so the deployment's cost has nowhere to come from but the merchant's part.
- **Tokens the relay can't bound.** A token is settled only where its key-signed transfer is
  marked `bounded` in `targets.json`, which is USDC on Ethereum and Sepolia.
- **The fee on a repeated `/settle`.** Only the answer that made the settlement carries the
  split: a finished submission keeps its status and not what it was.
- **A float's rotation.** Receipt addresses follow the float's key, so rotating it changes
  every merchant's `payTo`, and what an old receipt wallet holds needs the old key.
- **Stray payments.** A buyer's transfer landed on its own, outside a settlement, leaves the
  money in the receipt wallet. Nothing pays it out.
- **Distribution.** Verify one merchant settlement on Ethereum and use its receipt for the
  first SDK integration. Add merchant-resource discovery if a directory or partner needs it.
  [Adoption plan](adoption.md#x402-listings)

## Merchant-run facilitator

Publish “Be your own x402 facilitator” under Using Bruno → Integrations. The merchant
keeps its own receipt key and adds a small adapter to its x402 server. It validates the
buyer payment, quotes the buyer's USDC transfer with the receipt wallet as fee payer,
then submits that call and the merchant's signed fee to Bruno. The fee can come from
the payment that just arrived. Return the hash containing the buyer's transfer and
reuse the original submission on retries. Explain that the merchant's standalone fee
authorization remains independently usable until expiry. Show the flow with a stock
buyer and a merchant starting with zero USDC.

## Earlier fee research

These notes record the alternatives considered before the hosted design above. Deployment,
pricing and SDK observations are dated where they were checked.

### Getting paid: the fee question

**`exact` has no fee.** Its summary table: "Fees: None"; settlement "MUST produce exactly one
identifiable transfer of `amount` of `asset` to `payTo`". The facilitator pays the settlement's
gas (the client signs an authorization, not a transaction: `scheme_exact.md`, fee payer
"sponsored by the facilitator, or self-funded by the payer", and a 3009 payer can't fund it).
So with `exact` a facilitator either sponsors or bills the seller off chain. CDP bills (an API
key account; 1,000 settlements a month free, then $0.001 each), which Bruno can't without
accounts.

**`auth-capture` has a fee in the scheme.** Built on base/commerce-payments
(`AuthCaptureEscrow`); the facilitator's terms in `/supported`'s `extra`: `feeRecipient`,
`minFeeBps`, `maxFeeBps`, which the server copies verbatim into its requirements. The client's
signature commits to the bounds (`PaymentInfo`); the fee is taken on chain at `charge` or
`capture` (v1.1: an absolute `feeAmount` within the bounds). Equal bounds fix it. bps are the
spec's unit on the wire; Bruno's configuration keeps Decimal multipliers and converts.

Its costs: a lifecycle (authorize, capture, void, refund, reclaim, two deadlines), not one
transfer; `charge`/`capture` need the seller's consent (a receiver authorizer's signature, or
delegated to the facilitator: trust within the client's bounds); the escrow deployed on the
chain; clients and sellers that speak it (most pay `exact` today).

**Checked 2026-10-23: the escrow is on Base only.** `eth_getCode` on the v1.1 and v1.0 escrows
and 3009 collectors (`scratchpad/escrow_check.sh`): code on Base and Base Sepolia; none on
Ethereum, Sepolia, Optimism, Arbitrum, Polygon, Unichain, Linea, BNB Chain, World Chain,
Avalanche. The README says CREATE2, "the same address on Base Mainnet and Base Sepolia"; the
repo's `Deploy.s.sol` uses plain `new`. A facilitator MUST reject any escrow but the two
canonical addresses, so `auth-capture` on another chain needs the canonical set deployed
there: by whom, and whether anyone can (the factory, salt and exact bytecode used on Base) is
to find from Base's creation transactions.

Within `exact` itself there are ways to take a fee with the seller's cooperation: below.

### Fees within `exact`: the options

`exact` binds one thing: the client signs a transfer of exactly `amount` to `payTo`, and
"operations incidental to making that payment, such as fee payment … do not disqualify it"
(`scheme_exact.md`). So a fee is possible if `payTo` isn't the seller's final address but one
the settlement forwards from, net of the fee. It takes the seller's cooperation: the seller
(the paid service) publishes a `payTo` it got from, or agreed with, the facilitator. Each
option below is that, differing in what `payTo` is and so in what the client's signature
binds, who holds the funds, and how many transfers a settlement makes.

**What matters in comparing them:**

- **Binding:** the client's signature commits only to `payTo`. If the seller's destination
  isn't determined by `payTo`, it arrives in the `/settle` request as unsigned input, and
  anyone holding a client's payload (a proxy, a log, a middleman) could settle it to a
  destination of their own. Plain `exact` can't be redirected so.
- **Custody:** whether funds ever rest where the facilitator could take them.
- **Transfers per settlement:** a USDC transfer is ~30–60k gas; creating a balance slot (a
  zero balance made non-zero) costs more again (EIP-8037's state cost on Amsterdam).
- **Failures:** the facilitator pays the settlement's gas, and a transfer that fails
  (insufficient funds, a nonce used meanwhile) reverts and spends it for nothing: bundles
  where gas is real (Ethereum), the public mempool where it isn't (L2s).

**A. A shared long-term facilitator key.** `payTo` is one facilitator account for every
seller. Settlement: the client's transfer in, then the amount less the fee out to the seller:
two transfers, the fee simply staying. Simplest, and atomic if both are in one transaction.
But the destination isn't bound (any payload can be settled to anyone), and it's custodial:
every seller's funds pass through one key, and sellers trust the facilitator to forward.

**B. A key derived per seller** (`P·H(destination, …)`, stealth-address style, from the
facilitator's public key `P`). `payTo` commits to the destination, so a payload can't be
redirected by a third party. But the facilitator holds the derived private key
(`sk·H(…)`) and could sweep it anywhere: still custodial, the seller trusting the facilitator,
and each derived account needs gas or a sweep path. Transfers as A.

**C. Three transfers to fixed addresses.** `payTo` as A or B, and settlement sends the fee to
the facilitator's payee and the rest to the seller, leaving `payTo` empty: three transfers,
and `payTo`'s balance slot created afresh each time. Clean accounting, the most gas; binding
and custody as A or B.

**D. A CREATE2 forwarder, no key.** `payTo` = CREATE2(deployer, H(destination, token, fee
bounds), forwarder code): a pure function anyone can recompute, so the seller verifies its
`payTo` commits to its destination, and "negotiating" is computing an address. The code can
only send to its destination, keeping a fee within the bounds baked into it (`auth-capture`'s
client-bounded fee, carried by the address instead of an escrow, so no escrow per chain).
Settlement, one transaction from Bruno's runner: the client's transfer into the forwarder,
then the forwarder's transfer of the amount less the fee to the destination: two transfers,
the runner measuring what arrived by balance difference, as it measures payments now.

- **The fee stays in the forwarder:** a payment arrives and is split in the same transaction,
  so between transactions its balance is only accumulated fees, swept to Bruno now and then
  as a third leg of a settlement that makes it worth it. Its balance never returns to zero,
  so a payment into it never creates its balance slot (the payee's kept unit, again).
- **Deployed once, kept:** the runner self-destructs because it's single-use; a forwarder is
  reused by its seller, and creating an account costs 183,600 gas on Amsterdam each time.
  Deployed by the first settlement, counterfactual until then.
- **Fee bounds:** fixed in the address while gas moves with the base fee, so a fraction of
  `amount` plus a fixed floor; the facilitator picks the fee within them per settlement, and
  `/verify` refuses a payment whose fee can't cover its settlement rather than settle at a
  loss.
- Non-custodial and bound; the costs: a contract to write, audit and vendor; a deployment per
  seller; sellers changing their `payTo`.

**E. 402 on `/settle`** (outside the facilitator spec). The seller's server pays Bruno per
settlement by x402, and Bruno's runner makes both transfers (the client's to the seller, the
seller's fee to Bruno) in one transaction through a bundle, so neither lands without the
other. `payTo` stays the seller's own address: bound, non-custodial, no contract. But no seller
SDK expects a 402 from its facilitator, and the seller needs funds and a signer for the fee.

**F. `auth-capture`.** The spec's own fee (above): client-signed bounds, on chain; the escrow on
Base only, a lifecycle to implement, clients and sellers that speak it.

**G. `extra.splits`, if it reaches EVM** (to investigate). x402 added an optional multi-payee
splits extension to `exact` on 2026-10-26 (x402-foundation/x402 `477bdc7`,
`scheme_exact.md`, "Multi-Payee Splits"): the requirements carry `extra.splits`, absolute
amounts to further recipients, `payTo` receiving the remainder, each leg "a distinct on-chain
transfer", in "the same atomic settlement". The spec names the use: "This enables platform
fees, revenue sharing, referral commissions, and fee-payer cost recovery". A facilitator's fee
as a split leg would be client-signed, bound and non-custodial, with no contract and `payTo`
the seller's own. It's a facilitator capability, advertised in `/supported`'s `extra` (the
maximum number of splits), and "absent advertisement means no support".

But only Solana defines it (`scheme_exact_svm.md` §5); "Other networks MAY specify their own
encoding", and networks without one MUST reject a non-empty `splits`. On EVM an EIP-3009
authorization pays a single `to`, so an encoding needs a signature per leg (a 3009
authorization each, with nonces bound together) or Permit2's batch transfer through x402's
proxy. To investigate: whether an EVM encoding is in progress upstream, which of the two fits
`exact`'s rules (one identifiable transfer per leg, the client verifying every recipient), and
whether Bruno's runner settling all legs in one bundle makes the per-leg authorizations atomic
where a plain transaction wouldn't.

| | Destination bound | Custody | Transfers | Needs |
|---|---|---|---|---|
| A shared key | no | facilitator | 2 | nothing |
| B derived key | yes | facilitator | 2 | a key per seller |
| C three transfers | as A/B | as A/B | 3 | nothing |
| D forwarder | yes | none | 2 (+ a sweep now and then) | a contract, a deployment per seller |
| E 402 on `/settle` | yes | none | 2 | seller SDK support, the seller's own signer |
| F `auth-capture` | yes | escrow (client-bounded) | escrow lifecycle | the escrow on the chain, client support |
| G `extra.splits` | yes | none | 1 per leg | an EVM encoding (none yet), client support |
