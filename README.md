# Bruno: permissionless post-paid gas relays via Bundlers

A transaction's sender must hold its gas before it's sent. Bruno sends a client's call and
pays the gas, and the call pays Bruno back as it runs: cash on delivery, in ETH or any token,
from whatever the call can reach. What the account holds, what the call releases or earns,
another account's signed payment: nothing is needed in advance.

The mechanism rests on builders: Bruno sends only through `eth_sendBundle` (or equivalent),
so that a builder drops a reverting transaction instead of including it. A
transaction that doesn't pay therefore never lands, and costs Bruno nothing. A builder that
includes it reverted makes Bruno pay its gas, unpaid; the client is protected by the payment
check either way.

1. **Submit.** The client sends one call (typically to its own account, running a batch it
   signed through its 7702 delegate), its EIP-7702 authorization if needed, and a payment:
   ETH or a token, and an amount.
2. **Wrap.** Bruno generates a runner: initcode that reads the payee's balance, makes the
   call, and reverts unless the payee (Bruno's payment account) gained at least the amount.
3. **Send.** One of Bruno's concurrency accounts sends the runner to Bruno's deployer, which
   CREATE2s it at an address bound to that account and block. The client's authorization
   rides in the same type-4 transaction. The float funds the accounts' gas.
4. **Bundle.** The transaction goes to builders in `droppingTxHashes`, or alone in its
   bundle.

The client's signatures fix what runs and what it pays; Bruno can only include it or not.
Each runner is a new address that keeps no code, so nothing granted to it outlives its
transaction.

## Client contract

Nothing is included unless, during the call, the payee gains at least the amount; a
submission that isn't included costs the client nothing. Everything else is the client's to
enforce by its own signature: what the call does and in what order, any delegation it relies
on, and its replay protection, since anyone who sees a submission can run it. In full:
SPEC.md.

## Status

A proof of concept, written in October 2026.

- **Locally:** every path runs on anvil at Prague and at Amsterdam, against contracts fetched
  from mainnet, through a mock builder that applies rbuilder's commit rule.
- **Live:** on Sepolia, through an ePBS builder, in standalone bundles: a paying submission
  landed, and an underpaying one was dropped and cost nothing.

### Networks

A transaction's gas limit is computed from the fork's gas schedule, never estimated, so each
network needs its forks' schedules and activation times: Ethereum (Prague), Sepolia and Hoodi
(Amsterdam). A network also needs builders that take bundles, which is the scarcer of the two.

### Builders

`eth_sendBundle` is Flashbots' API, and each builder extends it its own way. The mechanism
needs one property: a reverting transaction isn't included. Every builder that takes bundles
gives that to a single-transaction bundle (standalone); fewer let one transaction of a bundle
be dropped while the rest land (`droppingTxHashes`). As documented on 2026-10-10:

- **`droppingTxHashes`:** Titan, BuilderNet (since v1.3), Quasar, BTCS. rbuilder
  implements it, with `version` unset or v2 and later.
- **Not listed:** the Flashbots relay, which documents only `revertingTxHashes`; Blink.
- **`revertingTxHashes`** is never used: a listed transaction may be included reverted, its
  gas unpaid.
- **Elsewhere:** bloXroute takes bundles as `blxr_submit_bundle`, through its paid API.
- **Testnets:** few builders. On Hoodi, Titan takes `droppingTxHashes` but built about one
  block in 23. On Sepolia, the ePBS builder `builder-sepolia.nflaig.dev` takes bundles,
  without `droppingTxHashes`.

### Prior art: sponsored bundles

**Sponsored bundles**, pioneered by builder0x69 and offered by
[Titan](https://docs.titanbuilder.xyz/api/eth_sendbundle#sponsored-bundles) since 2023 and by
[Quasar](https://docs.quasar.win/api/eth_sendbundle#sponsored-bundles). A bundle transaction
that would fail with `LackOfFundForGasLimit` gets the builder's ETH transfer prepended,
covering its gas and value. The bundle is included only if executing it increases the
builder's balance by more than the transfer. A
[proposal](https://collective.flashbots.net/t/proposal-builder-funded-gas-sponsorship-for-bundles/5820)
(July 2026) brings the same to BuilderNet and rbuilder.

This is the same post-paid condition, enforced by the builder that fronts the gas, with the
builder's own ETH balance as the payee. Bruno generalises it:

- **Payment:** ETH or any token, to any payee, with no transfer to a particular builder's fee
  recipient. A call paying in a token needs no swap to ETH.
- **Who can offer it:** the runner checks the payment on-chain, so a relay that isn't a
  builder can make the same guarantee through any builder that drops reverting transactions.
- **One submission for every builder:** its payee doesn't depend on which builder includes
  it.
- **The client signs one call,** not a transaction: the relay sets fees, sender and timing.

A builder already sponsoring bundles has the parts to serve the interface (SPEC.md) natively. It
simulates, fronts gas from its own account and checks repayment. It would need the runner's
payment check in place of its own balance check, or that check made directly against the
block's state.

### Glamsterdam and ePBS

Sepolia activated Amsterdam on 2026-10-06 (1791294816), Hoodi activates it on 2026-10-26
(1793036568), and Ethereum follows. Two changes matter here:

- **Gas (EIP-2780, 7976, 8037, 8038):** creating an account now costs 183,600 gas, so the
  runner's CREATE2 dominates a submission's overhead: about 230,000 gas on Amsterdam, against
  about 70,000 on Prague (DESIGN.md, Gas).
- **ePBS (EIP-7732):** the builder's bid moves into the protocol. Bundle endpoints are
  builders' own, so which builders serve which network, and with which `eth_sendBundle`
  extensions, is being re-established through the migration. Pre-ePBS endpoints, such as
  Titan's on Hoodi, may not carry over.

Bruno targets Sepolia, which runs both today.

## Documents

| | |
|---|---|
| [SPEC.md](SPEC.md) | The standard, as a client sees it: submissions, semantics, the client's obligations, errors, the interface, security considerations |
| [DESIGN.md](DESIGN.md) | How Bruno provides it, and why: the deployer and runner, bundling, gas bounds and fork schedules, accounts, the queue and batches, funding, price, risks, testing |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Running the server, and its configuration |

## Development

```
make install          toolchain, project-local
make check            lint, format, ty, pyrefly; tests on Prague and Amsterdam anvils
make check-sepolia    live test on Sepolia (spends Sepolia ETH)
make serve            the relay: CONFIG=, PORT=, FLOAT_KEY= (DEPLOYMENT.md)
make <net>-wallet     create or show .secrets/<net>/wallet.key
make <net>-deploy     deploy the deployer and Calibur
make <net>-sweep      drain the live-test accounts into the wallet
```

anvil: nightly `6c21ffe`, port 8546, `--order fifo --disable-pool-balance-checks`.
`tests/localchain.py` installs the deployer and the fixtures in `tests/fixtures/evm/`
(Calibur, Permit2, the x402 proxy, USDC, UNI, WETH9, USDT). `tests/mockbuilder.py` applies
the builder rule (DESIGN.md, Bundling) to anvil.

`make check-sepolia` sends standalone bundles to `https://builder-sepolia.nflaig.dev/rpc`, which
needs an `X-Flashbots-Signature`.
