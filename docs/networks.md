---
description: "The networks Bruno can serve, the builders that take its bundles and how, prior art in sponsored bundles, and Glamsterdam's gas and ePBS"
---

# Networks and builders

## Networks

A transaction's gas limit is computed from the fork's gas schedule, never estimated. Which
schedule is in force is measured from the chain, not from activation times: any network on a
known fork (Prague, Amsterdam) works, and an unknown fork stops the relay rather than misprice.
A network also needs the deployer (`make <net>-deployer-deploy`) and builders that take
bundles, which are the scarcer.

## Builders

`eth_sendBundle` is Flashbots' API, and each builder extends it its own way. The mechanism
needs one property: a reverting transaction isn't included. Every builder that takes bundles
gives that to a single-transaction bundle (standalone); fewer let one transaction of a bundle
be dropped while the rest land (`droppingTxHashes`). As documented on 2026-10-10, and
probed on 2026-10-20 with a bundle that can't land:

- **`droppingTxHashes`:** Titan, BuilderNet (since v1.3), Quasar, BTCS. rbuilder
  implements it, with `version` unset or v2 and later. Titan's, BuilderNet's and Quasar's
  bundle hashes change with the field; BTCS's hash covers only the transactions, so shows
  nothing either way.
- **Not listed:** the Flashbots relay, which documents only `revertingTxHashes`; Blink;
  beaverbuild, which documents none, though its bundle hash changes with the field. Bruno sends
  them standalone bundles.
- **Gone:** rsync's endpoint no longer resolves.
- **`revertingTxHashes`** is never used: a listed transaction may be included reverted, its
  gas unpaid.
- **Elsewhere:** bloXroute takes bundles as `blxr_submit_bundle`, through its paid API.
- **Testnets:** few builders. On Hoodi, Titan takes `droppingTxHashes` but built about one
  block in 23. On Sepolia, the ePBS builder `builder-sepolia.nflaig.dev` takes bundles,
  without `droppingTxHashes`.

## Prior art: sponsored bundles

**Sponsored bundles**, pioneered by builder0x69 and offered by
[Titan](https://docs.titanbuilder.xyz/api/eth_sendbundle#sponsored-bundles) since 2023 and by
[Quasar](https://docs.quasar.win/api/eth_sendbundle#sponsored-bundles). A bundle transaction
that would fail with `LackOfFundForGasLimit` gets the builder's ETH transfer prepended,
covering its gas and value. The bundle is included only if executing it increases the
builder's balance by more than the transfer. A
[proposal](https://collective.flashbots.net/t/proposal-builder-funded-gas-sponsorship-for-bundles/5820)
(July 2026) brings the same to BuilderNet and rbuilder.

This is the same post-paid condition, enforced by the builder that fronts the gas, with the
builder's own ETH balance as the payee. Bruno generalises it:

- **Payment:** ETH or any token, to any payee, with no transfer to a particular builder's fee
  recipient. A call paying in a token needs no swap to ETH.
- **Who can offer it:** the runner checks the payment on-chain, so a relay that isn't a
  builder can make the same guarantee through any builder that drops reverting transactions.
- **One submission for every builder:** its payee doesn't depend on which builder includes
  it.
- **The client signs one call,** not a transaction: the relay sets fees, sender and timing.

A builder already sponsoring bundles has the parts to serve the interface ([SPEC.md](SPEC.md)) natively. It
simulates, fronts gas from its own account and checks repayment. It would need the runner's
payment check in place of its own balance check, or that check made directly against the
block's state.

## Glamsterdam and ePBS

Sepolia activated Amsterdam on 2026-10-06 (1791294816). Hoodi was to follow on 2026-10-26, but
All Core Devs deferred it on 2026-10-22, and Ethereum comes after Hoodi. Two changes matter
here:

- **Gas (EIP-2780, 7976, 8037, 8038):** creating an account now costs 183,600 gas, so the
  runner's CREATE2 dominates a submission's overhead: about 230,000 gas on Amsterdam, against
  about 70,000 on Prague ([DESIGN.md, Gas](DESIGN.md#gas)).
- **ePBS (EIP-7732):** the builder's bid moves into the protocol. Bundle endpoints are
  builders' own, so which builders serve which network, and with which `eth_sendBundle`
  extensions, is being re-established through the migration. Pre-ePBS endpoints, such as
  Titan's on Hoodi, may not carry over.

Bruno targets Sepolia, which runs both today.
