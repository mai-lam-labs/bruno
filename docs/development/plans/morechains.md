---
description: "Plan: Bruno on chains beyond Ethereum and Sepolia, and which flows each kind of chain can support"
---

# Plan: more chains

Bruno on chains beyond Ethereum and Sepolia. Two kinds, by what the chain offers:

- **Chains with the bundler guarantee** (a transaction that would revert is never included):
  every flow, post-paid and atomic x402 included. Type-4 isn't required; without it a client
  can't delegate in the same transaction, and everything else stands.
- **Every other chain:** only what doesn't need the guarantee: `upfront` x402
  (`x402-async.md`), paid before anything is sent.

## Which chains have the guarantee (research, 2026-10-23)

| Chain | Mechanism | Holds? |
|---|---|---|
| **Unichain** | `eth_sendBundle` on the sequencer's public endpoint (`mainnet.unichain.org`), no auth. op-rbuilder: a bundle's transaction not in `revertingTxHashes` is skipped, never committed, "dropped from the pool" once its range passes. One transaction per bundle; range at most head + 10 blocks (~10 s at 1 s blocks). | **Yes** until 2026-10-29, and exclusive: one sequencer. Then unknown: Optimism takes over the operation (below). |
| **Linea** | `eth_sendBundle` at the sequencer (`BundleConstraintTransactionSelector`: a failed transaction not listed drops the bundle); one `blockNumber`, not a range. | **Yes, gated:** Infura's private beta only; the public RPC lacks the method. |
| **BNB Chain** | Flashbots-style builders (48club Puissant, BlockRazor, NodeReal, BlockSec). | **As on Ethereum:** many builders and a public mempool, not exclusive. Send only to builders that drop reverts. |
| Base, OP Mainnet, Ink, Soneium, Mode, Zora, World Chain, Arbitrum, Scroll, zkSync, Polygon PoS and zkEVM, Blast, Mantle, Taiko, MegaETH, Monad, and others | No bundle API, or one that includes reverts. Base runs op-rbuilder with revert protection off. | **No:** `upfront` only. |

Caveats to settle before relying on Unichain:

- **The operator is changing.** Unichain moves to OP Enterprise: Optimism runs its
  sequencers, RPC, monitoring and incident response
  ([Uniswap, 2026-10-09](https://blog.uniswap.org/unichain-is-upgrading-to-op-enterprise)).
  Testnet moved on 2026-10-13 and mainnet moves on 2026-10-29 (no time given). Chain ID,
  contracts and balances stay; the announcement says nothing about bundles, the builder or
  revert protection. Sepolia refusing `eth_sendBundle` fits this, as it was already under
  Optimism when we first probed it (2026-10-23; the same on 2026-10-28, while mainnet still
  accepts the method). OP Mainnet, which Optimism already runs, has no bundle API. Flashbots
  referred our questions to the OP team (2026-10-27). Until Optimism says otherwise, Unichain
  may join the `upfront`-only list after 2026-10-29. Probe `mainnet.unichain.org` again
  after the switch-over.

- **The bundle docs are live, mainnet only** (corrected 2026-10-23: the page moved, not gone):
  developers.uniswap.org/docs/unichain/technical-information/advanced-txn (source
  Uniswap/docs `content/unichain/technical-information/advanced-txn.mdx`, last changed
  2026-07-17). "Use the Unichain mainnet endpoint for bundle submission":
  `https://mainnet.unichain.org`. One transaction; block or timestamp range, not both; a
  10-block default lifetime; revert protection when `revertingTxHashes` is omitted; the
  `bundleHash` is the transaction hash; a dropped bundle's receipt query answers `-32602`
  "the transaction was dropped from the pool" (Bruno's receipt polling must read that as
  dropped, not as a failing node).
- **No testnet bundles anywhere:** probed 2026-10-23, Unichain Sepolia's public RPC (proxyd
  over op-reth) answers "rpc method is not whitelisted"; publicnode and dRPC "method not
  found"; Alchemy and Ankr need Unichain enabled on our keys (and forward only what the
  sequencer exposes). Testing the guarantee: ask in public (the Flashbots Collective's thread,
  not email); or Flashbots'
  builder-playground (a local OP stack with op-rbuilder, revert protection on), the L2 twin of
  our mock builder; or mainnet itself, where a dropped bundle costs nothing and an included
  one cents.
- **Gossip and fallback:** a bundle's transaction enters the builder's pool as local; if that
  pool gossips to the sequencer's default execution client, rollup-boost's fallback block (when
  the builder lags or is down) could include it reverting. Deployment configuration, not
  visible: ask.
- **Never sent any other way:** the guarantee holds only while the signed transaction goes to
  that endpoint alone (L1 forced inclusion covers deposits signed on L1, not Bruno's).
- **Flashblocks:** a preconfirmation isn't inclusion; Bruno waits for the block.

And on Ethereum itself: a May 2026 Flashbots forum report had a BuilderNet bundle transaction
included reverting despite default semantics (the thread now 404s; no rbuilder issue found;
rbuilder's source drops unlisted reverts). Watch it.

## What each new chain needs

- **A sender per chain kind** (`builders.py`): Ethereum's builder fan-out; a sequencer's
  `eth_sendBundle` with its limits (Unichain: one transaction, ≤ 10 blocks, resubmitted per
  range; Linea: one block number); a builder subset (BNB); plain `eth_sendRawTransaction`
  for `upfront`-only chains. Configured per chain, never inferred.
- **Pricing with the L1 fee.** On rollups a transaction also pays for its data on L1, which
  Ethereum's gas schedule doesn't cover. Per family: OP stack's `GasPriceOracle` (Ecotone /
  Fjord formula, Isthmus's operator fee), Linea's own, Arbitrum's. The fork is measured from
  the chain as now; an unknown family or fork stops the relay rather than misprice.
- **The fork's gas schedule.** OP-stack forks map onto Ethereum's (Isthmus on Prague); each
  family's mapping stated and checked by `make preflight`.
- **Block time.** Bruno settles per block. Unichain's 1 s blocks are 12× Ethereum's: the
  Worker's alarm cadence and CPU per alarm (Workers Paid) need measuring before deploying, and
  maybe settling every few blocks instead.
- **On-chain prerequisites, checked by preflight:** the deployer (CREATE2 factory present?),
  EIP-3009 USDC (native, not bridged), the price pools and the treasury's router, Calibur
  (where 7702 is active), the RPC nodes (`make rpc-check`).
- **A live network to test on.** Unichain Sepolia (chain 1301, `sepolia.unichain.org`, 1 s
  blocks) is up, but its public RPC refuses `eth_sendBundle` ("rpc method is not
  whitelisted"; probed 2026-10-23), while `mainnet.unichain.org` accepts it. Flashbots demoed
  revert protection on Unichain Sepolia in August 2025. To find: a testnet bundle endpoint
  (Infura's Unichain Sepolia RPC, through `config.local.json`; or ask Uniswap). Funds: Sepolia
  ETH over the OP-stack bridge; testnet USDC's source unchecked. Then a `<net>-verify` as for
  Sepolia. **Done 2026-10-23:** the float holds ~0.099 ETH there; `make
  unichain-sepolia-deploy` put our deployer and x402's Permit2 proxy (tx 0x2635…ca9e) at their
  canonical addresses; Calibur and Permit2 were already there.

## Architecture: a transaction's cost beyond gas (decided 2026-10-23)

On OP-stack chains a transaction also pays an L1 data fee and an operator fee, outside its gas,
from its sender's balance (and the node's pool requires the balance to cover them). Added where
cost is computed (some fifteen `gas × fee` sites), it would be an `if` everywhere. Instead:

- **One model, measured.** `bruno/datafee.py` (`worker/src/datafee.ts`): `DataFee`, an OP-stack
  chain's fee inputs as of a block (L1 base fee, blob base fee, their two scalars from
  `GasPriceOracle`; the operator fee's scalar and constant from `L1Block`), and its
  `ceiling(unsigned size, gas, blocks)`: Fjord's upper bound (FastLZ's practical bound on the
  size, the regression to Brotli) and Jovian's operator fee (`gas × scalar × 100 + constant`),
  the L1 fees risen an eighth a block for `blocks` blocks (L2 blocks, at least as many as L1's
  in the same time; blob fees rise less).
- **Measured, not assumed, and no fork flags.** The hourly measurement reads `GasPriceOracle`'s
  code: none, no data fee (Ethereum, Sepolia); code, then the inputs and the oracle's own
  `getL1FeeUpperBound` and `getOperatorFee` for sample sizes, in the same batch, and our
  formula must agree, else the chain is unavailable ("a data-fee formula Bruno doesn't know"),
  as an unknown gas schedule is. A fork that changes the formula stops the relay rather than
  misprice. Base, Unichain and both Sepolias were on Jovian on 2026-10-23.
- **Read with the block.** `Latest` gains `data_fee: DataFee | None`; on an OP-stack chain the
  inputs ride in `latest()`'s batch.
- **One cost.** Every `gas × fee` becomes a cost of gas, fee per gas and the transaction's
  unsigned size, plus the data fee's ceiling: quotes, charges, ranking, funding, the
  treasury, the deployer. `SignedTransaction` carries its data fee's ceiling (from its own
  size), so `max_cost`, and every balance check and top-up built on it, includes it.
- **Sizes without tuned constants.** A transaction's unsigned size is its RLP with the widest
  fields it can have where they aren't known yet (a quote): an overestimate, never a guess.
- **Exact drains** (`sign_drain`, the sweeps) leave the data fee's ceiling behind on an
  OP-stack chain: draining to exactly 0 needs the fee known exactly, which it isn't.
- **Tests:** Ethereum's paths unchanged (`make check`); the formula pinned by vectors
  (Python → Worker) and checked against the live oracles by preflight; an anvil test with
  `GasPriceOracle` and `L1Block` vendored at their predeploy addresses, as the other mainnet
  contracts are.

## Architecture: our own cadence, not the chain's (decided 2026-10-23)

Blocks come every 12 s on Ethereum, 2 s on Base, 1 s on Unichain, 0.2 s on Base Sepolia (mean
over 1,000 blocks, 2026-10-23). With RPC round trips of 0.4–0.8 s and timeouts of ~15 s,
stepping per block on a fast chain can't keep up, burns CPU, and invites races; and in the
Worker every step is an alarm (a request), a `setAlarm` and a few rows written. Per block at
0.2 s that's ~26M steps a month a busy chain: ~$4 of requests and ~$50 of rows written, and
the object never at rest. So:

- **A step every max(block time, 5 s).** Never sooner than 5 s, as a rule (the author's), on
  any chain: Ethereum stays per block (12 s); Base, Unichain and Base Sepolia step every 5 s.
  Only an event runs sooner: a submission taken still closes its batch at once.
- **A step covers the time to the next one.** It settles what's been included since the last
  step (one read of receipts), and sends once, valid until the next: a block range where the
  sender takes one (Unichain: 5 blocks, under its 10-block cap), a plain transaction for
  `upfront` (valid until mined), Ethereum's builders a bundle per block as now.
- **`slot_ms` replaces `slot_seconds`:** the slot, in milliseconds (default 12,000): the
  shortest time between two blocks, the protocol's schedule. Not the block time, the observed
  mean, which is never shorter (Ethereum: 12.06 s against a 12 s slot, its missed slots; an
  OP-stack sequencer fills every slot, so there they coincide). It converts between blocks and
  time where the bound wanted is the slot's: when the next block can come, the most blocks
  that fit before an authorization expires, block ranges. `make preflight` checks it against
  the chain's mean over 100 blocks (at least the slot, at most a quarter more). It never sets
  the step rate. Where a time should be the longest (x402's maxTimeoutSeconds, blocks x slot)
  the slot understates it by any missed slots: a margin, if it matters, not a second value.
  On a 0.2 s chain timestamps are whole seconds, five blocks to one: a next block predicted from
  the latest's timestamp is good to a second, which steps of 5 s don't notice.
- **The cost, checked against Cloudflare's pricing (2026-10-23):** duration is billed only
  while running, or idle and unable to hibernate; between alarms the object is idle and
  hibernateable, so unbilled. A chain busy 24/7 at a 5 s step: ~520k alarms a month (half the
  1M included requests), ~65k GB-s (of 400k), ~2.5M rows written (of 50M). Idle: nothing but
  the hourly treasury run. A held x402 request is billed while held (a request in progress).
- **A prerequisite for any fast chain in the Worker,** ahead of slice 2's L2 execution.

## Order

1. Ask the OP team in public whether Unichain keeps `eth_sendBundle` and revert protection
   under OP Enterprise, on mainnet and Sepolia, and about gossip/fallback (Flashbots referred
   our questions there); meanwhile builder-playground for a local op-rbuilder. If bundles
   go, Unichain moves to the `upfront` list.
2. OP-stack pricing and fork mapping (Unichain is OP stack, and so is most of the `upfront`
   list).
3. Unichain Sepolia, then Unichain, on the sequencer sender.
4. `upfront` chains as `x402-async.md` lands.
5. Linea if Infura grants access; BNB Chain if the builder subset holds up.

## Open questions

1. **BNB Chain:** worth a builder subset that isn't exclusive, as Ethereum's isn't?
2. **Settling per block on 1 s chains,** or every N blocks: a cost against latency.
3. **Which `upfront` chains first:** by the facilitators' networks (Base, Arbitrum, Polygon)
   or by demand?
