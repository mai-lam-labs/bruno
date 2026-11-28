---
description: "Why the hosted x402 facilitator settles through a receipt wallet Bruno holds the key to, why a merchant's terms are in its URL and not in its receipt address, why a settlement that isn't included is answered as unused, and what a merchant relies on"
---

# The hosted facilitator

[Accepting x402 payments](../../using/facilitator.md) says what a merchant does and what it's
answered. This page records why it's built the way it is.

## Why a receipt wallet

Under `exact`, a buyer signs an EIP-3009 transfer of the whole price to one address, the
merchant's `payTo`, and nothing else. A facilitator that's paid out of the payment has to
stand between the buyer and the merchant, and a stock buyer has to be able to pay it without
knowing. So `payTo` is an address Bruno can sign for, one for each payout address, and the
payment is forwarded from it in the transaction that receives it.

The wallet is a plain key. Nothing is deployed for a merchant, and no contract of Bruno's
holds anyone's money between blocks. A key is also what the relay can bound the gas of: a
transfer a key signed costs what the token's measurement says, where a contract's signature
check costs whatever its code does ([gas](gas.md)).

A settlement is a submission like any other, made by Bruno itself: the buyer's transfer and
the wallet's payout as its calls, and the wallet's transfer of the fee as its payment. Nothing
in the relay knows a settlement from another submission paid by a signed transfer, which is
what [several calls](runner.md) and their grouping through Multicall3 were built for. The fee
is checked as every payment is: the transaction that doesn't pay Bruno's payee the amount
doesn't land.

## The key

A receipt key is `keccak256(root ‖ payout address)`, the root a key derived from the float's
on a path of its own. Nothing is stored, both relays derive the same address, and a merchant
needs no account: its payout address is its identity. One receipt key says nothing of
another's, or of the root.

Receipt addresses follow the float's key. Rotating the float changes every merchant's
`payTo`, and what an old receipt wallet holds needs the old key. That's unsolved.

## The fee

The fee is the relay's own quote for the three transfers, taken when `/settle` is called.
The wallet's two authorizations can't be signed until the fee is known, and their calldata
is part of what's priced. So the quote prices them at their dearest, with no byte of theirs
zero, and the fee signed then covers whatever they come to hold. The settlement is taken at
that fee by the same check as any submission, which is why it isn't refused as underpaid a
moment later.

The receipt wallet keeps one base unit of a token the first time it's paid in it, as the
[forwarder](sponsorship.md#the-forwarder) does. A balance that returns to zero has its
storage slot cleared, and setting it again at every payment would cost each settlement far
more than one unit.

`/verify` makes the same settlement and runs it through the relay's own check of a
submission (its plan, its price and its simulation) without queueing it. So what `/verify`
says is what `/settle` would do at that block, by the same code.

## The merchant's terms

A merchant's limits are segments of its URL's path, `name=value`. x402's reference client
builds each endpoint's URL by joining strings, so a path prefix reaches Bruno unchanged and a
query string wouldn't. Nothing changes in the merchant's client or its buyers'.

The terms aren't part of the receipt key. Binding them in was considered: a payment signed to
an address derived from the terms could only be settled under them. It would also change
`payTo` every time a merchant changed a cap, and make one merchant's receipt addresses a
set to keep track of. So there is one receipt address for a payout address, and the terms
limit the call they come with.

What that leaves: whoever holds a buyer's signed payment, the buyer or the merchant, can
settle it through the same payout's URL with other terms, or none. The buyer pays the same
either way and the merchant is still the one paid, so there's nothing to gain by it.

## Not included, and asked again

x402 has a code for a settlement still in doubt, `settlement_pending`: a transaction was
broadcast, it may still confirm, and its hash is given so the caller can look. Bruno doesn't
use it. Bruno signs a new transaction for each block and sends it to builders, never to the
public pool, so a hash given at one block is of a transaction that can't land in the next.

What Bruno can say is stronger. A settlement has a deadline five blocks on, and once that
block has passed without it nothing of it can land. `/settle` then answers that the payment
wasn't settled and is unused, and the same payment sent again is a new settlement. A stock
merchant server doesn't serve a buyer whose settlement failed, so the buyer isn't served and
isn't charged.

Five blocks is about a minute on Ethereum, under the 90 seconds the reference client gives a
facilitator for one answer. The relay's own x402 route holds for longer because it's a
resource, which states its own `maxTimeoutSeconds`.

A payment being settled, or settled, is found by the digest its buyer signed, as a user
operation is found by its hash. A second `/settle` for it is answered by the first one's
submission.

## What the merchant is told, and where

x402 has no notion of a facilitator's fee. The fee's split is answered in the
`EXTENSION-RESPONSES` header, as an extension of Bruno's own. The body's `extensions` would
reach the buyer, since the reference resource server encodes the whole settle result into the
buyer's `PAYMENT-RESPONSE`, and a merchant's fee is its own business.

The reasons Bruno adds are named `bruno_…`, written as x402's are. The specification doesn't
hold a reason to its list, and none of its codes says a token isn't taken or a facilitator
declined on its own terms.

## What a merchant relies on

- **Bruno holds the receipt key.** A payment reaches the merchant only through a settlement
  Bruno makes.
- **A buyer's transfer can be landed on its own.** x402's `exact` uses
  `transferWithAuthorization`, which anyone holding the signed payment can submit. The money
  is then in the receipt wallet with no settlement to pay it out. Bruno doesn't reconcile
  that. Every facilitator of `exact` has the same gap.
- **The wallet's own authorizations last a little past the deadline,** two slots, for blocks
  that come late. Within that time, if the buyer's transfer has landed by another route,
  anyone holding the wallet's two can land them too. That pays the merchant and Bruno what
  the settlement would have.
- **A key-signed payment settles in one transaction,** so the chain keeps it whole. A smart
  wallet's settles as a bundle, which a builder keeps whole ([bundling](bundling.md)).

## Set aside

- A buyer that's a smart wallet not deployed yet. It signs exactly the price, so its
  deployment would come out of the merchant's part.
- A token whose transfer the relay can't bound. Its settlement would need a runner and a gas
  the wallet's own transfers can't be given ahead.
- A forwarding contract with the fee receiver set at its deployment, as
  [x402-foundation/x402#2886](https://github.com/x402-foundation/x402/pull/2886) proposes:
  a deployment for each token and merchant where a key needs none
  ([upstream](../upstream.md)).
