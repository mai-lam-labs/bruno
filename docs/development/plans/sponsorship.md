---
description: "Atomic sponsorship, built in 0.6.1: what's left of it to build, and what was accepted as it is"
---

# Plan: atomic sponsorship, what's left

Sponsorship is built, on both relays and in the TypeScript client. What it is and what a client
sends are in the [specification](../../SPEC.md#sponsorship), why it's built this way is on its
[design page](../design/sponsorship.md), and a whole script is in the
[guide](../../using/sponsorship.md). This page lists what isn't built.

## Not built

- A quote's deadline can't be given as a count of blocks. Its `maxBlockNumber` is a block
  number, so a client that doesn't know the head gets the furthest deadline, which costs the
  most.
- A transaction to sign can't pin its own nonce or fees. A quote fills in both, and a client
  that wants its own signs the transaction first and gives it signed.
- A quote takes only authorizations signed already. It doesn't say which nonce an unsigned one
  is to be signed at.
- A sponsorship's quote has no breakdown of its price, as a call's has.
- No test shows a sponsorship sitting out a block whose fees its payment doesn't cover.
- Sponsorship isn't ready for an OP-stack chain. The account isn't sent what its own
  transactions pay beyond gas there (the L1 data fee), and the cap counts the relay's own such
  fees on one side only. No OP-stack chain is served.
- The client isn't published as a release: `@mailam/bruno` and `@mailam/bruno-agentkit` stay on
  `-dev` versions.

## Accepted as it is

- A quote offers an ETH payment to an account that holds no ETH. The quote can't know that an
  earlier transaction of the sponsorship won't bring it. A submission the account can't pay
  for is refused, `sponsored-unfunded`, and the guide says what's needed.
- Nothing sends on what the forwarder holds after a broken bundle. Anyone may call the
  forwarder, and the next sponsorship paid in that asset sends it on. The root shows what's
  there.
