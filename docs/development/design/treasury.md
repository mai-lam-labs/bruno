---
description: "How the float refills itself from what the payee collects: runs, the runway, conversions through the pools, and the root's balances"
---

# Treasury

**The float has to refill itself.** Payments go to the payee, in whatever token the client
chose; the float pays gas in ETH. Without something moving revenue back, the relay stops once
the float is spent. A treasury per chain does it, given the payee's key: the payee and the
float stay separate accounts so each has one user of its nonce, and more floats can be added.

**A run every `interval_seconds`, nothing in between.** A run reads the float, its accounts
and the payee in one batch, measures what the float and accounts spent since the last run
(what they held then, plus what the last run forwarded if it landed, less what they hold now),
and sends what's needed as one bundle. It waits for nothing: the next run reads the forward's
receipt. Idle, it costs a read every few hours.

**The target is a runway, with a floor.** The float and accounts are topped up to
`runway_days` of their measured spend, and to at least enough to fund every account once for
the largest transaction at the fee ceiling: what the relay's first full batch can draw, before
any spend is measured. Both follow fees.

**Through the payee.** Its ETH above its reserve goes first; then its WETH, unwrapped; then
its other tokens, each swapped along the pools that price it, through Uniswap's SwapRouter02
in one call: each run of v2 or v3 pools a leg, its output kept in the router, then unwrapped
and paid to the payee. The payee forwards the lot to the float. The minimum out is the pools'
price less the token's margin (its pools' cut) and `slippage_multiplier`.

**Gas measured, not assumed.** Every transaction's gas is measured with `eth_estimateGas` as
it will be sent, with a quarter's headroom: forks price gas differently (Amsterdam's state
creation costs many times Prague's), so no fixed figure fits. Measuring also refuses a swap
that would revert before anything is sent. A swap can only be measured once the router may
spend its token, so each token is approved ahead of its first swap, in a run that does only
that; the next run follows soon after, not a whole interval later.

**The payee keeps a reserve:** one run's gas at its max fee, × `funding_multiplier`, the gas
measured for the tokens it holds. Swaps pay
the payee inside the run's bundle, so the reserve only has to carry the run until they do;
conversions refill it. A payee below what a conversion's gas needs stalls, and the snapshot
says so: it needs ETH from outside.

**Converting must be worth it.** A conversion goes ahead only if its gas and its pools' cut
come to at most its value × (`max_cost_multiplier` − 1), 2% by default; converted, it's made
at least that large, up to what the payee holds, so a worthwhile conversion is made once
rather than in pieces. The rule is waived when the float is running dry: it can't cover a
transaction, or its measured spend outlasts it before the next run. A single swap of a few
dollars on Ethereum costs several percent in gas; the rule converts larger amounts less often.

**One bundle, all or nothing,** to every builder, for each of the next five blocks: a swap's
slippage reverts the bundle, and nothing is spent. A payee's transactions are never in the
public mempool.

**Every balance on the page is as of one read.** The root shows each chain's float, its
concurrency accounts and the payee (each address linked to the network's explorer, its balance
and nonce), and what the payee holds of each token, all as of the block they were last read
at; and the treasury's last run: only its own figures (the target and reserve, how many
transactions of `max_gas` the float covered) and what it did, at its block. Viewing it reads
nothing from the chain. Every batch reads the float, the accounts and the payee with its tokens,
in the request it makes anyway, and each treasury run reads them too; the later read wins. An
idle relay reads nothing, and nothing it holds changes but by its own transactions or the
treasury's, so the figures stay true; what's sent from outside shows at the next read. On the
Worker, the hourly cron that wakes a chain's object for its treasury reads them again when the
treasury isn't due, so they're an hour old at most; the object is otherwise only woken to
answer.

**A base unit of each x402 token stays with the payee.** Converted, the payee's balance of a
token would return to zero, and the next x402 transfer to it would set the slot again: 107,920
gas on Amsterdam, on every such submission's bound. Keeping one unit keeps the slot set; the
relay prices the new slot only while the payee holds none, as each treasury run tells it.

**Exposure is the float, not the payments.** Revenue waits as stablecoins until it's needed;
what's held in ETH is the float and the payee's reserve, a few days of gas.
