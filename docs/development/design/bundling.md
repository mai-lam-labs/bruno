---
description: "How submissions reach builders: standalone and dropping bundles, every builder every transaction, and the builder rule relied on"
---

# Bundling

A submission's transaction is sent only through `eth_sendBundle`, in one of two modes per
builder:

- **Standalone:** each transaction is its own bundle. A reverting transaction fails its
  bundle and isn't included. Any builder that takes bundles.
- **Dropping:** one bundle a block, every transaction listed in `droppingTxHashes`. A
  reverting transaction is dropped and the rest land. A builder that honours
  `droppingTxHashes`.

A submission of several transactions (a call to a listed target, then the transfer that pays
for it) is a bundle of its own in either mode, with nothing listed in `droppingTxHashes`: it
lands whole or not at all, which is what makes its payment certain.

**Every builder gets every transaction,** in its own mode. Each comes from its own account at
that account's next nonce, so it lands at most once, whichever builder wins the block; the next
block's are signed afresh. Bundles are signed (`X-Flashbots-Signature`) by the float, unless a
builder has an identity key of its own.

`version` is unset and `revertingTxHashes` never used. The rule relied on (rbuilder
`order_commit.rs` @ `6037fa7`, 698–732, 824–878): a reverting or invalid transaction in
`droppingTxHashes` is skipped; one listed nowhere fails the bundle; a failed bundle is rolled
back whole.

**A builder serving the interface itself** needn't use the deployer or runner: it can include
the call directly, from its own account, only where the block's state shows the payee paid.
That skips the runner's CREATE2, the bulk of the overhead.
