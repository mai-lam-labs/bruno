---
description: "The tip, our fee, the price a block is signed at, quotes allowing for rising fees, and token prices from pools"
---

# Price

**The tip is the market's:** the larger of the median of the last 20 blocks' median tips and
the median of the last 5, so it follows a rise within a few blocks. No floor: the median is
what half of recent transactions paid, enough to be included by definition, even when it's 0.
No cap: a higher tip never costs the relay, since a submission whose amount doesn't cover the
block waits, and a cap would take the relay out of service under contention.

**Our fee is the tip again:** the relay earns what the builder does, for each gas. A fixed
margin over cost earns little when fees are low and too much when they're high; the tip
tracks how contended blocks are, which is what the service is worth. Floors keep it from
nothing when the market tips little: `min_fee_per_gas`, and `min_fee` on a transaction,
priced in a token (a cent of USDC). They protect the income, not inclusion, so they're on
our fee, never the builder's tip.

**Signed at exactly the next block's base fee** plus the tip: a bundle targets one block,
whose base fee is known from the latest. The price is that fee × the gas limit, and our fee, in
the token, × the margins. The chain's margin defaults to none; a token's covers converting
it to ETH.

**A quote allows for rising fees:** the next base fee risen by an eighth a block, EIP-1559's
most, through the quote's blocks, and twice the tip. Accounts are funded to that ceiling. A
quote is advisory; each block decides.

**Token prices from pools,** each pool's spot price along the token's path to WETH. ETH and
WETH need none: a wei is a wei. A spot
price can be moved within a block, but it's read from the latest block's state: moving it
means leaving the pool off-market at a block's end, open to arbitrage. Deep pools, and a
cache of `price_ttl_seconds`.

**Margins are multipliers,** string `Decimal`s, never basis points.
