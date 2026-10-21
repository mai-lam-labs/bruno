# Plan: the treasury and market pricing (v0.1.1)

The relay's float only drains: nothing moves revenue from the payee back to the float, so
the relay stops when the float is empty. v0.1.1 is the full operational package that fixes
this, and prices from the market rather than a fixed tip.

**The end-to-end proof:** deploy v0.1.1, put USDC and a little ETH on the payee, and wait.
The treasury converts, the float is topped up, and the relay serves, all visible on the
root page. Done first on anvil, then Sepolia, then Ethereum.

Discussion and figures: issue #3. This file is the working checklist: tick items as
they're done, and add notes as they come up.

## Decisions

| | Decided |
|---|---|
| Pricing | The full proposal (below), in place of a fixed 1 gwei tip × 1.1. |
| Tip | The market's, no floor and no cap: the median is what half of recent transactions paid, so it's enough to be included by definition; a tip of 0 is fine. `priority_fee` is removed. |
| Our fee | The larger of the tip and `min_fee_per_gas` (0.01 gwei), × gas; at least `min_fee` (a per-chain token and amount, 1 cent of USDC on Ethereum), priced through that token's pools. The floors protect our income, not inclusion. |
| Tip cap | None. The per-block check means a higher tip never costs us money: a submission whose amount doesn't cover the block waits. A cap would take us out of service under contention. |
| Payee key | Held by the Worker as a secret, `PAYEE_KEY` (low stakes). Payee and float are separate to keep their nonces apart and to allow more floats later. |
| Payee gas | The payee keeps a reserve, derived: one run's gas at its max fee × `funding_multiplier`. Swaps pay the payee within the run's bundle, so it only needs to cover a run before the swaps pay out. |
| Float target | The larger of `runway_days` × the measured spend and a derived minimum: `accounts` × `funding_multiplier` × `max_gas` × the fee ceiling (every account funded once for the largest transaction). ~$26 at 1.34 gwei base fee with typical gas, ~$147 with `max_gas`; start with `max_gas`, revisit on Sepolia. |
| Scheduling | Worker: an hourly cron trigger calls each chain's `Treasury` object, which runs when `interval_seconds` is up (an alarm would need something to wake it after a deploy). Python: a task per chain inside the server, given `--payee-key` / `BRUNO_PAYEE_KEY`; no separate `make treasury`, so the root can show the snapshot. |
| Conversion path | Swaps through Uniswap's router, sent as bundles through our builders. CoW Protocol was considered and isn't needed once the payee holds gas. |
| Treasury schedule | Every 3–6 hours, per chain. |
| Funding | `funding_multiplier` 6 in the defaults. The simulation showed funding rounds were the biggest hidden cost at 2. |
| Rollout | anvil → Sepolia (any prices: proves resilience) → Ethereum, as v0.1.1. |
| Deployed v0.1.0 | Stays as it is until v0.1.1. |

## Pricing

Every step's one `eth_feeHistory` read (already batched with the latest block) covers the
last 20 blocks and asks for the 50th-percentile tip of each.

- **Tip:** the larger of the median of the 20 blocks' medians and the median of the last 5
  (the 5 let it rise within a few blocks). No floor, no cap.
- **Our fee:** max(tip, `min_fee_per_gas`) × gas limit, at least `min_fee` in wei.
- **Quote:** (gas limit × (next base fee × 1.27 + 2 × tip) + our fee) × the token's margin.
  The 2 × tip is the allowance for the tip rising, as the 27% is for the base fee.
- **Taking a submission, and each block:** the amount must cover (gas limit × (that block's
  base fee + tip) + our fee) × the token's margin. If it doesn't, the submission waits.
- **Paid:** each transaction tips the market tip; funding transactions likewise.
- **Configuration:** the chain's `margin_multiplier` defaults to `"1"`; a token's
  `margin_multiplier` covers converting it (`"1.003"` for a 0.3% pool); `min_fee_per_gas`
  (wei) and `min_fee` (`{"token", "amount"}`, the amount in whole tokens by `decimals()`, the
  token one the chain accepts).

## The treasury

One per chain: a Durable Object class in the Worker (`worker/src/treasury.ts`), and
`bruno/treasury.py` with `make treasury` for the Python server. Each run:

1. **Read** the float, the concurrency accounts and the payee (ETH and each accepted token),
   in one batch.
2. **Measure** the spend since the last run: the last snapshot of the float and accounts,
   plus what the treasury sent since, less now.
3. **Target:** the float and accounts topped up to two days of spend. Nothing to do if they
   hold it.
4. **The payee's ETH** above its reserve goes to the float first: a plain transfer.
5. **Swap** a stablecoin: one transaction through Uniswap's router, along the token's
   configured pools to WETH, unwrapped, paying ETH to the float (or to the payee when its
   reserve is low). The minimum out comes from our pool price, less a set slippage. The
   first swap of a token approves the router first, in the same bundle.
6. **Cost rule:** go ahead only if the gas plus the pool fee is under 2% of the amount,
   unless the float would run dry before the next run.
7. **Send** as a bundle through our builders. The next run checks the receipt: nothing waits
   in between, so idle costs nothing.

**Visibility:** each chain's float, accounts and payee balances, and how many transactions
the float covers, in the root's JSON and on the page. They come from the treasury's last
snapshot, so viewing the page never reads the chain.

## Testing

- **anvil, as now,** for pricing, visibility and the treasury's logic.
- **The treasury's swaps on the local anvil, vendored** (the user's standard): Uniswap's
  SwapRouter02, position manager, v2 factory and a USDC/USDT v3 pool vendored at their
  mainnet addresses; the pools given real liquidity at setup through their own code.
- **Sepolia:** a live run, only when asked. Sepolia's USDC pools have arbitrary prices: the
  point is that nothing breaks.
- **No tests that count node requests** (the user's call).

## Checklist

### 1. Pricing
- [x] `latest()` (both relays): `eth_feeHistory` over 20 blocks with the 50th percentile, in the same batched request
- [x] The tip: the larger of the two medians, at least `priority_fee` (Python, then Worker)
- [x] The quote, the check when taking a submission, and the per-block check, at the new formula
- [x] Funding transactions tip the same
- [x] Profit ranking with the new cost and price
- [x] `margin_multiplier` default `"1"`; DEPLOYMENT.md's settings table updated
- [x] Golden vectors for the tip (from fee-history samples) and the quote; the Worker matches
- [x] Server tests pass at the new prices, on both relays
- [x] `config.default.json`: `funding_multiplier` 6, `priority_fee` as a floor (value to decide), token margins `"1.003"`
- [x] DESIGN.md (Price) and SPEC.md (if quotes change meaning) updated

### 2. Visibility
- [x] Treasury snapshot stored: float, accounts, payee balances, the time, and the last run's outcome
- [x] The root's JSON shows each chain's snapshot and how many transactions the float covers
- [x] The page shows the same (Python and Worker byte for byte, as now)
- [x] Test: the root carries the snapshot, on both relays

### 3. The treasury
- [x] Rework: `priority_fee` removed; `min_fee_per_gas` and `min_fee`; our fee as above (both relays, vectors, docs)
- [x] Settings: `treasury: {router, interval_seconds, runway_days, max_cost_multiplier, slippage_multiplier}`; the float's minimum and the payee's reserve derived
- [x] The router and its call encoding: `multicall(exactInput(path → WETH), unwrapWETH9(min, recipient))`, plus `approve`
- [x] A swap's path from the token's configured `price` hops (v2 and v3 pools)
- [x] `bruno/treasury.py`: read, measure, target, transfer, swap, cost rule, bundle; one run
- [x] The Python server runs it per chain given `--payee-key` / `BRUNO_PAYEE_KEY`; `make serve` passes `.secrets/payee.key`
- [x] The Worker: `treasury.ts`, the `Treasury` Durable Object class, run from an hourly cron trigger when due
- [x] Golden vectors for the router calldata, so the Worker's matches the Python's
- [x] `deploy.ts`: the cron schedule, and `PAYEE_KEY` as a secret (no second class or migration: see notes)
- [x] `make worker-secret` sets `PAYEE_KEY` from `.secrets/payee.key`
- [x] Real swaps on anvil (vendored router and pools, liquidity minted at setup): USDC along v3 with its approval; USDT along v3 then v2
- [x] Tests: the cost rule skips a small conversion; an empty float converts anyway; ETH above the reserve is forwarded
- [x] DESIGN.md (a Treasury section), DEPLOYMENT.md (settings, `PAYEE_KEY`, the proof), README

### 4. Rollout
- [x] `make check` passes, the swap tests included
- [x] Sepolia's config: Circle's USDC (`0x1c7D…7238`) through the 0.3% v3 USDC/WETH pool (`0x6Ce0…9b50`), margin `"1.003"`; `min_fee` a cent of USDC, as Ethereum
- [x] Anvil burn-in: a long randomised run on the fast chain, both relays, its invariants checked every block (its own make target, not `make check`)
  - Found and fixed: the Worker held a submission until its forget-only alarm (an hour) when one arrived after the queue emptied: in v0.1.0 too. Fix: a submission replaces an alarm that's only for forgetting, as the Python relay replaces its idle clock; a regression test on both relays.
  - Found and fixed (the harness): Litestar's test client runs the app in a thread and loop of its own, so a Python relay was driven from two loops at once; the tests now serve it with uvicorn in the test's loop.
  - Found and fixed: the treasury stopped at the first conversion it couldn't afford (a WETH unwrap after it would have refilled its ETH), and its reserve forgot a token's gas once the payee held none of it; it now passes over what it can't afford, and remembers each token's last measured gas. Regression tests for both.
  - Found and fixed: funding was priced at 2× the base fee, which a full-speed EIP-1559 rise outruns in ~6 blocks, before its 10-block timeout; it's now priced for its timeout's blocks (the treasury's bundles for their 5). The relay no longer sends funding the float can't cover at that max (a node would refuse it; anvil's pool admitted it and dropped it silently).
  - The burn-in's own fixes: no-capacity 503s are refusals, not failures; base-fee shocks are EIP-1559 surges (an eighth a block), not jumps; the funding timeout is 10 slots; the mock builder keeps public-mempool transactions that arrive mid-build (in the pool, or mined into a trial block) and sends them again.
  - Result, seed 1703039936, 120 blocks: no violations on either relay; honest submissions 93/97 (Python) and 94/97 (Worker) included.
- [x] The root's page: each chain's float and concurrency accounts, addresses (shortened) linked to the network's explorer, balances and nonces as last read: by every batch (the float read with the accounts, in the same request) and by the treasury; on the Worker, kept in the chain's object's storage and refreshed by the hourly cron when the treasury isn't due, so no object is kept awake for it. Numbers right-aligned in `<code>`. The page's order: Chains, Try it, Donate, Reference, Version, a rule, Accounts, Treasury (the user's asks, 2026-10-21)
- [x] Sepolia (live run: only when asked; ~10 minutes): wrap a little ETH to WETH, swap a little ETH for USDC, fund the payee; `make serve`: the first run approves, the next converts and forwards; then a submission paid in USDC. Keep every swap small (0.005 ETH or so each way) so the pool stays usable in both directions
- [ ] Bump `pyproject.toml` to 0.1.1, merge, tag `v0.1.1` (CI releases the image)
- [ ] Ethereum: `make worker-deploy worker-secret`, fund the payee with USDC and a little ETH, watch the float fill: the proof
- [ ] Before merging PR #4: `git rm PLAN-TREASURY-v011.md`

## Notes

- **Simulation** (`scratchpad/treasury_sim.py`, gitignored; worth keeping as a tool). A
  month on Ethereum with a $15 float:
  - at 1 gwei, the float can't keep up past ~20 submissions a day (7–43% refused);
  - at the median tip, ~0% are refused up to 400 a day, and conversions cost ~2% of the
    margin;
  - ETH's volatility barely matters (±$7 a month on a ±35% trend); the float is the
    exposure, not payments waiting to convert;
  - `funding_multiplier` 2 → 6 multiplied profit several times.

  The model is simple (fixed gas, every bundle lands): a guide to direction, not a forecast.
- **Ethereum fees** (2026-10-20): base fee 0.04–0.8 gwei within hours. The per-block median
  tip ranged 0–4.7 gwei across 20 blocks, ~0.05–0.1 typical. Nodes'
  `eth_maxPriorityFeePerGas` suggested ~0, so a floor is needed.
- **Durable Object billing:** alarms count as requests; duration is 128 MB × wall-clock
  while active, waiting on I/O included; idle isn't billed. A treasury running every 3–6
  hours is effectively free. An alarm should send and return, not wait for a receipt.
- **v0.1.0's priority fee** is 1 gwei on every chain, and stays until v0.1.1.
- **Rules:** keep Python and the Worker in step (CLAUDE.md); commit only when asked; live
  runs only when asked.

- **Pricing as built:** the median of an even count is the upper one; a block without a
  reward tips 0. Accounts are funded to the base fee risen for `quote_blocks` blocks + 2 ×
  the tip (the quote's tip allowance; our fee isn't spent). The chain margin was already
  `"1"` by default; the defaults drop their `"1.1"`. SPEC.md needed no change: a quote was
  already advisory. Then reworked: `priority_fee` removed (a leftover one in a
  configuration is ignored); Ethereum's defaults `min_fee_per_gas` 0.01 gwei and `min_fee` a
  cent of USDC, Sepolia's 0.01 gwei only. `min_fee`'s token decimals are read as the relay
  opens.

- **Sepolia's USDC pools** (2026-10-20): every fee tier prices ETH near $50,000. The 0.05%
  pool holds 4.3M USDC and 99 WETH, the 0.3% pool 3.9M USDC and 53 WETH, the 1% pool 54M USDC
  and 1,009 WETH. The 0.3% pool matches Ethereum's margin.
- **Mainnet, 2026-10-20 (later):** ETH $2,465; next base fee 1.34 gwei; the 20 blocks'
  median tips 0.1–1.0 gwei, median 0.29. 1 cent ≈ 0.023 gwei a gas at 175k gas.

- **The treasury as built** (2026-10-20):
  - **In the chain's own object, not a `Treasury` class:** the Worker's `ChainRelay` runs it,
    as the Python treasury holds its relay. No second class, no v2 migration, no test setup
    copied between objects. An hourly cron calls `/{chain}/__treasury` on each chain with a
    treasury; paths under `/{chain}/__` are refused from outside. The root reads each chain's
    snapshot from its object (`/{chain}/__snapshot`), never the chain.
  - **Everything goes through the payee:** WETH unwrapped and swaps paid to the payee, then
    one transfer to the float, all one bundle (for each of the next 5 blocks). The order is
    the configuration's (WETH before the stablecoins in the defaults).
  - **Chunked conversions** (found on a mainnet fork, since replaced by vendored swaps): at a 1.3 gwei base fee a ~$15 swap costs
    ~5.6% in gas, so the 2% rule refused it. A conversion worth making is now made at least
    large enough for its gas to be within the rule, up to the payee's balance; the rest stays
    with the payee for later runs.
  - **Running dry includes an empty float:** the float and accounts can't cover one
    transaction of `max_gas` at the fee ceiling, as well as the measured spend not lasting to
    the next run. On a first run nothing is measured, so an empty float needed this.
  - **The forward's gas is the fork's:** a transfer to a float not yet in the state costs
    183,600 more on Amsterdam; found by `make check`'s Amsterdam run.
  - **Gas measured with eth_estimateGas, + 25%** (the user's choice over eth_simulateV1, which
    publicnode alone answered, and per-fork fixed limits). Fixed limits failed twice: a
    two-leg swap ran out on Prague (267,272 of 270,000), and Amsterdam's state creation
    (EIP-8037) broke the approval. A swap is measured only once its token is approved, so
    every token is approved ahead, in a run that does only that (one that also forwarded left
    the payee unable to pay the next run's swap); the next run follows in 2 minutes (Python)
    or at the next cron (Worker). A conversion's amount is sized again if its final
    measurement is higher. The reserve counts the measured gas of the tokens the payee holds.
  - **Tests measure, never tune:** a probe run (the payee holding no ETH, so nothing can be
    sent) reads the target and the reserve; a test then puts the payee at its reserve. Dust is
    ten times its measured gas at the base fee. No per-fork constants.
  - **1rpc.io dropped** from the defaults (the user's call; it answered a POST with a 301).
  - **`interval_seconds` is an integer**, so the run-dry check is exact in both languages.
  - **No mainnet fork:** swaps are tested on the local anvil with vendored contracts, both
    relays. `make vendor RPC=… VENDOR="name…"` (bruno/tests/vendor.py, new: the target named
    a module that didn't exist) fetches a manifest entry's code.
  - **The Python snapshot is in memory:** a restart measures spend afresh.

- **Sepolia, 2026-10-21** (blocks 11960003–11960121, through the nflaig builder): the router
  approved for USDC, then two minutes later the payee's 0.005 WETH unwrapped and 0.0019 ETH
  forwarded; then 1,242.6 USDC swapped back through the 0.3% pool and 0.0264 ETH forwarded,
  the float 0.0108 → 0.0372, the payee kept at its reserve (~0.005). The 2% rule refused the
  swap until the USDC was worth ~0.023 ETH: on Sepolia's Amsterdam schedule the swap measures
  309k gas (Prague ~150k). A local config with `funding_multiplier` "12" set the float short
  of its target. My funding script's fixed 21,000 gas reverted a transfer to the not-yet-
  existing payee (Amsterdam: +183,600); it now measures gas too.

## Open questions

- The slippage allowed on a swap: `"1.005"`? The interval: 6 hours? (Both the defaults now.)
- Sepolia's tokens for the proof: which USDC, and which pool prices it?
