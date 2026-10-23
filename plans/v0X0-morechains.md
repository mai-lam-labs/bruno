# Plan: more chains

Bruno on chains beyond Ethereum and Sepolia. Two kinds, by what the chain offers:

- **Chains with the bundler guarantee** (a transaction that would revert is never included):
  every flow, post-paid and atomic x402 included. Type-4 isn't required; without it a client
  can't delegate in the same transaction, and everything else stands.
- **Every other chain:** only what doesn't need the guarantee: `upfront` x402
  (`v0X0-x402-async.md`), paid before anything is sent.

## Which chains have the guarantee (research, 2026-10-23)

| Chain | Mechanism | Holds? |
|---|---|---|
| **Unichain** | `eth_sendBundle` on the sequencer's public endpoint (`mainnet.unichain.org`), no auth. op-rbuilder: a bundle's transaction not in `revertingTxHashes` is skipped, never committed, "dropped from the pool" once its range passes. One transaction per bundle; range at most head + 10 blocks (~10 s at 1 s blocks). | **Yes**, and exclusive: one sequencer. |
| **Linea** | `eth_sendBundle` at the sequencer (`BundleConstraintTransactionSelector`: a failed transaction not listed drops the bundle); one `blockNumber`, not a range. | **Yes, gated:** Infura's private beta only; the public RPC lacks the method. |
| **BNB Chain** | Flashbots-style builders (48club Puissant, BlockRazor, NodeReal, BlockSec). | **As on Ethereum:** many builders and a public mempool, not exclusive. Send only to builders that drop reverts. |
| Base, OP Mainnet, Ink, Soneium, Mode, Zora, World Chain, Arbitrum, Scroll, zkSync, Polygon PoS and zkEVM, Blast, Mantle, Taiko, MegaETH, Monad, and others | No bundle API, or one that includes reverts. Base runs op-rbuilder with revert protection off. | **No:** `upfront` only. |

Caveats to settle before relying on Unichain:

- **Its bundle docs page is gone** while the endpoint answers: ask Uniswap/Unichain whether it
  stays.
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

## Order

1. Ask Uniswap/Unichain about the bundle endpoint's future and its gossip/fallback.
2. OP-stack pricing and fork mapping (Unichain is OP stack, and so is most of the `upfront`
   list).
3. Unichain Sepolia, then Unichain, on the sequencer sender.
4. `upfront` chains as `v0X0-x402-async.md` lands.
5. Linea if Infura grants access; BNB Chain if the builder subset holds up.

## Open questions

1. **BNB Chain:** worth a builder subset that isn't exclusive, as Ethereum's isn't?
2. **Settling per block on 1 s chains,** or every N blocks: a cost against latency.
3. **Which `upfront` chains first:** by the facilitators' networks (Base, Arbitrum, Polygon)
   or by demand?
