---
description: "Working on Bruno: the make targets, the local chain and its fixtures, and the live tests"
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
the builder rule ([DESIGN.md, Bundling](DESIGN.md#bundling)) to anvil.

`make check-sepolia` sends standalone bundles to `https://builder-sepolia.nflaig.dev/rpc`, which
needs an `X-Flashbots-Signature`.
