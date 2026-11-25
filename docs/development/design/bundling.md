---
description: "How submissions reach builders: standalone and dropping bundles, a bundle of its own for a submission of several transactions and what's done when a builder breaks one, every builder every transaction, and the builder rule relied on"
---

# Bundling

Everything is sent through `eth_sendBundle`. A submission of one transaction goes in one of
two modes per builder:

- **Standalone:** each transaction is its own bundle. A reverting transaction fails its
  bundle and isn't included. Any builder that takes bundles.
- **Dropping:** one bundle a block, every transaction listed in `droppingTxHashes`. A
  reverting transaction is dropped and the rest land. A builder that honours
  `droppingTxHashes`.

A submission of several transactions is a bundle of its own in either mode, with nothing
listed in `droppingTxHashes`, so a builder lands it whole or not at all. There are two kinds:

- **A call to a listed target, then the transfer that pays for it,** both from one concurrency
  account ([the runner](runner.md)).
- **A sponsorship:** the relay's first transaction, the sponsored account's own as it signed
  them, and the relay's last, which checks the payment ([sponsorship](sponsorship.md)). The
  relay's two are from one concurrency account; the ones between are from the sponsored
  account, at the nonces and fees it signed.

In both, the payment is certain only while the bundle is whole, and that's the builder's to
keep, not the chain's.

**A bundle a builder breaks is rejected, and nothing more of it is sent.** If some of such a
bundle's transactions land, each succeeding, and the rest don't, a builder landed part of
it. The submission is rejected with the reason `bundle-broken` and listed with the chain's
problems, with the ETH the landed transactions sent. The rest isn't sent afterwards, because
that would land in pieces what was to land whole.

**Every builder gets every transaction,** in its own mode. A submission's own transactions come
from its concurrency account, at that account's next nonces, so they land at most once,
whichever builder wins the block; the next block's are signed afresh. A sponsored account's
transactions are the same bytes every block, valid once at their nonces. Bundles are signed (`X-Flashbots-Signature`) by the float, unless a
builder has an identity key of its own.

`version` is unset and `revertingTxHashes` never used. The rule relied on (rbuilder
`order_commit.rs` @ `6037fa7`, 698–732, 824–878): a reverting or invalid transaction in
`droppingTxHashes` is skipped; one listed nowhere fails the bundle; a failed bundle is rolled
back whole.

**A builder serving the interface itself** needn't use the deployer or runner: it can include
the call directly, from its own account, only where the block's state shows the payee paid.
That skips the runner's CREATE2, the bulk of the overhead.
