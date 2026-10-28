---
description: "Working on Bruno: the make targets, the local chain and its fixtures, how it's tested, the two implementations, and the live tests"
---

# Development

```
make install          toolchain, project-local
make check            lint, format, ty, pyrefly; tests on Prague and Amsterdam anvils; the Worker's
make worker-check     the Worker (worker/): Deno's format, lint, types; tests against make vectors
make check-sepolia    live test on Sepolia (spends Sepolia ETH)
make burn-in          minutes of seeded random load on the local chain, both relays (BURNIN_SEED=)
make serve            the relay, its float and payee keys made in .secrets/ the first time (DEPLOYMENT.md)
make worker-deploy    the Worker on Cloudflare; then worker-secret, worker-domain (DEPLOYMENT.md)
make <net>-wallet     create or show .secrets/<net>/wallet.key
make preflight        every configured chain checked against its live nodes, read-only
make rpc-check        every configured node checked on its own: chain, lag, batches, rate limit
make <net>-verify     a deployed relay verified with real payments (spends gas)
make <net>-deployer-deploy  deploy the runner's deployer where it's missing (spends ETH)
make <net>-deploy     deploy the deployer and Calibur on a testnet
make <net>-sweep      drain the live-test accounts into the wallet
```

anvil: nightly `6c21ffe`, port 8546, `--order fifo --disable-pool-balance-checks`.
`tests/localchain.py` installs the deployer and the fixtures in `tests/fixtures/evm/`
(Calibur, Permit2, the x402 proxy, USDC, UNI, WETH9, USDT, and Uniswap's pools, position
manager, v2 factory and SwapRouter02), and mints the pools' liquidity. `tests/mockbuilder.py` applies
the builder rule ([design, bundling](design/bundling.md)) to anvil.

`make check-sepolia` sends standalone bundles to `https://builder-sepolia.nflaig.dev/rpc`, which
needs an `X-Flashbots-Signature`.

## How it's tested

**Against real code, not models of it.** Contracts are fetched from mainnet (Calibur, USDC,
USDT, UNI, WETH9, Permit2, Uniswap's v2 and v3 pools, its position manager and SwapRouter02)
and installed on anvil at their mainnet addresses. Runners are generated and executed
on-chain, and gas is asserted exactly against receipts, on a Prague and an Amsterdam anvil.
The pools hold real liquidity, minted through their own code, so the treasury's swaps run
through Uniswap's router as they would on Ethereum.

**A mock builder** applies rbuilder's commit rule to anvil, bundle by bundle, so both bundling
modes are tested end to end. The live test sends standalone bundles to an ePBS builder on
Sepolia. The Python relay is served by uvicorn in the test's own event loop, as in production.

**A burn-in** (`make burn-in`, minutes, not part of `make check`) leaves each relay to its own
clock on the fast chain for a hundred-odd blocks under a seeded random load: clients paying in
USDC, USDT or WETH, honest or underpaying, spending their tokens after submitting, deadlines a
block away, submitting twice, base-fee shocks, and the treasury living on the revenue. Every
block, none of the relay's transactions may revert on-chain, nothing may answer with a server
error, and nothing may land past its deadline. At the end, nothing may be left pending, and
neither relay may have logged an error. Its seed replays the load.

**Releases are checked against the networks.** Before deploying, `make preflight` checks every
configured chain against its live nodes, read-only. After, `make <net>-verify` pays through the
deployed relay: a payment a unit short refused, one landed, one over x402. Its client's call is
its own `transferWithAuthorization` to the payee, so it needs no ETH and no delegation. Both
exist because Ethereum was once served for two releases without the deployer: every check had
run on a local chain that installs it.

## Two implementations

The Python relay (`bruno/`) is the reference, and the Cloudflare Worker (`worker/`) is its
port, module for module.

**Python writes golden vectors** that the port must reproduce exactly: runner initcode (as its
keccak256), every gas figure, signed transactions, funding, bundles, RLP at its boundaries and
QR codes (`make vectors`). It also writes the documents and page the Worker serves: OpenRPC,
OpenAPI, and the root page, whose QR codes Python makes with segno and the port's own encoder
reproduces, mask choice included.

**The server tests run against both.** The Worker runs under workerd, against anvil and the
mock builder served over HTTP. Its batches and blocks are driven through test hooks that only a
local build enables, or left to its own alarms on a fast chain: a block every two seconds,
built by the mock builder.

**A small supply chain.** Deno and workerd are single binaries pinned by sha256. The Worker's
only packages are `@noble/hashes`, `@noble/curves`, `@scure/bip32` and `@scure/base`: audited,
depending on nothing else, fetched from JSR and locked by integrity. There's no npm, no install
scripts and no Wrangler: the Workers runtime types the relay uses are declared in the port,
and the QR encoder is the port's own.
