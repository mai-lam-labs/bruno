# Plan: Bruno as an x402 facilitator

Bruno settling x402 payments for other sellers: `POST /verify`, `POST /settle`,
`GET /supported`, x402 v2's facilitator interface (§7), so a resource server anywhere can
point its x402 middleware at Bruno instead of CDP. Bruno already does a facilitator's work
for its own endpoint (verifying an EIP-3009 authorization, refusing with x402's codes,
settling it on chain); this opens it to anyone, permissionlessly: no account, no API key.

Normative text (`scratchpad/x402spec/`, coinbase/x402 at `0e6e8c5`): x402 v2 §6.1 (payment
flows), §7 (facilitator interface), §8 (discovery); `schemes/exact/scheme_exact.md` and
`scheme_exact_evm.md`; `schemes/auth-capture/scheme_auth_capture.md` and
`scheme_auth_capture_evm.md`; `extensions/bazaar.md`.

## Where Bruno would stand out

- **Ethereum mainnet.** CDP's facilitator settles on Base, Polygon, Arbitrum, Monad and World,
  not Ethereum.
- **Settlement through bundles:** a settlement that would fail never lands, so a facilitator
  running on Bruno pays nothing for failures (insufficient funds, a nonce used meanwhile).
- **Permissionless:** no account or key for the seller; `GET /supported` is the whole
  onboarding.
- **Its own Bazaar:** §8's `GET /discovery/resources` lists what a facilitator has settled for,
  so sellers settling through Bruno are discoverable through Bruno.

## Getting paid: the fee question

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

## Fees within `exact`: the options

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

| | Destination bound | Custody | Transfers | Needs |
|---|---|---|---|---|
| A shared key | no | facilitator | 2 | nothing |
| B derived key | yes | facilitator | 2 | a key per seller |
| C three transfers | as A/B | as A/B | 3 | nothing |
| D forwarder | yes | none | 2 (+ a sweep now and then) | a contract, a deployment per seller |
| E 402 on `/settle` | yes | none | 2 | seller SDK support, the seller's own signer |
| F `auth-capture` | yes | escrow (client-bounded) | escrow lifecycle | the escrow on the chain, client support |

## Decisions (proposed)

- **`exact` first, sponsored, where settlement is cheap** (Base, other L2s: `upfront`-style
  settlement, no bundler guarantee needed for a lone transfer); on Ethereum, `exact`
  sponsored only within a configured budget, since each settlement costs real gas.
- **`auth-capture` for a real fee,** on Base now, elsewhere once the canonical escrow is.
- **Settlement:** Bruno's existing 3009 settlement, as a plain transaction (not a runner).
  `/settle` answers once included, with the hash. Where Bruno sends through bundles it signs
  the settlement once, a fee cap covering its window, so the hash is fixed at broadcast;
  `settlement_pending` (which MUST carry that hash) if inclusion can't be confirmed in time.
- **`/verify`:** read-only, as `take`/`refusal` do now: signature, balance, nonce unused,
  validity window, simulation.
- **`signers`:** the accounts settling, per network (`eip155:<id>`).
- **Bazaar:** the `bazaar` extension's metadata recorded from settled payments and served at
  `/discovery/resources`.
- **Shared with `v0X0-x402-async.md`:** Bruno's own `upfront` payments settle through the
  same code, its own facilitator rather than CDP's where it runs.

## Open questions

1. **Sponsoring `exact` on Ethereum:** a monthly budget, a per-seller limit (by `payTo`), or
   not at all?
2. **`auth-capture`:** worth its lifecycle now, on Base only, or wait for the escrow to reach
   other chains?
3. **Deploying the canonical escrow** on Ethereum ourselves, if Base's deployment is
   reproducible (CREATE2 factory and salt): ours to do, or Base's?
4. **Which fee option** (A–F): D or E for permissionless and non-custodial; F where the escrow
   is; A–C only if custody is acceptable. Possibly D and F both.
5. **Abuse:** a free facilitator is a gas faucet for anyone settling dust; rate limits by
   `payTo` and a minimum amount per network?
