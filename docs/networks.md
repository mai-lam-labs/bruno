---
description: "The networks Bruno can serve, the builders that take its bundles and how, prior art in sponsored bundles, and what Glamsterdam's gas changes and ePBS mean for Bruno"
---

# Networks and builders

## Networks

Bruno computes each transaction's gas limit from the fork's gas schedule instead of estimating
it. It works out which schedule is in force by measuring the chain, not from activation times.
That means it works on any network running a fork it knows (Prague or Amsterdam), and on an
unknown fork it stops instead of mispricing.

A network also needs Bruno's deployer (`make <net>-deployer-deploy`) and builders that take
bundles. Builders are usually the harder part to find.

## Builders

`eth_sendBundle` is Flashbots' API, and each builder extends it in its own way. Bruno needs one
property from a builder: it must never include a reverting transaction. Every builder that
takes bundles guarantees this for a bundle with a single transaction (a standalone bundle).
Fewer builders let one transaction in a bundle be dropped while the rest still land
(`droppingTxHashes`).

This is based on each builder's documentation as of 2026-10-10, and on a probe on 2026-10-20
with a bundle that couldn't land:

- **Support `droppingTxHashes`:** Titan, BuilderNet (since v1.3), Quasar and BTCS. rbuilder
  implements it when `version` is unset or v2 and later. Titan's, BuilderNet's and Quasar's
  bundle hashes change when the field is set. BTCS's hash only covers the transactions, so the
  probe couldn't tell either way.
- **Don't list it:** the Flashbots relay, which only documents `revertingTxHashes`; Blink; and
  beaverbuild, which documents neither, although its bundle hash changes with the field. Bruno
  sends these builders standalone bundles.
- **Gone:** rsync's endpoint no longer resolves.
- **`revertingTxHashes`** is never used, because a builder may include a listed transaction
  even though it reverted, and then its gas isn't paid.
- **Elsewhere:** bloXroute takes bundles as `blxr_submit_bundle`, through its paid API.
- **Testnets** have few builders. On Hoodi, Titan takes `droppingTxHashes` but built only about
  one block in 23. On Sepolia, the ePBS builder `builder-sepolia.nflaig.dev` takes bundles,
  but not `droppingTxHashes`.

## Prior art: sponsored bundles

**Sponsored bundles** were pioneered by builder0x69 and are offered by
[Titan](https://docs.titanbuilder.xyz/api/eth_sendbundle#sponsored-bundles) (since 2023) and
[Quasar](https://docs.quasar.win/api/eth_sendbundle#sponsored-bundles). If a transaction in a
bundle would fail with `LackOfFundForGasLimit`, the builder puts its own ETH transfer in front
of it to cover its gas and value. The bundle is only included if running it increases the
builder's balance by more than that transfer. A
[proposal](https://collective.flashbots.net/t/proposal-builder-funded-gas-sponsorship-for-bundles/5820)
from July 2026 would bring the same feature to BuilderNet and rbuilder.

This is the same post-paid idea as Bruno's, enforced by the builder that fronts the gas, with
the builder's own ETH balance as the payee. Bruno generalises it:

- **Payment:** in ETH or any token, to any payee, without a transfer to a particular builder's
  fee recipient. A call that pays in a token doesn't need to swap it to ETH.
- **Who can offer it:** the runner checks the payment on-chain, so a relay that isn't a builder
  can give the same guarantee through any builder that drops reverting transactions.
- **One submission works with every builder,** because its payee doesn't depend on which
  builder includes it.
- **The client signs one call, not a transaction.** The relay sets the fees, the sender and the
  timing.

A builder that already sponsors bundles has most of what it needs to serve Bruno's interface
([specification](SPEC.md)) natively: it simulates, fronts gas from its own account and checks
that it's repaid. It would need to use the runner's payment check instead of its own balance
check, or apply that check directly against the block's state.

## Glamsterdam and ePBS

Sepolia activated Amsterdam on 2026-10-06 (1791294816). Hoodi was due to follow on 2026-10-26,
but All Core Devs postponed it on 2026-10-22, and Ethereum mainnet comes after Hoodi. Two
changes matter for Bruno:

- **Gas (EIP-2780, 7976, 8037 and 8038):** creating an account now costs 183,600 gas, so the
  runner's CREATE2 makes up most of a submission's overhead: about 230,000 gas on Amsterdam,
  compared with about 70,000 on Prague (see [the design's gas section](DESIGN.md#gas)).
- **ePBS (EIP-7732):** the builder's bid becomes part of the protocol. Bundle endpoints are run
  by builders themselves, so which builders serve which network, and which `eth_sendBundle`
  extensions they support, is being worked out again during the migration. Endpoints from
  before ePBS, such as Titan's on Hoodi, may not carry over.

Bruno targets Sepolia, which runs both today.
