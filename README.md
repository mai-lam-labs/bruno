# Bruno: permissionless post-paid gas relays via Bundlers

A transaction's sender must hold its gas before it's sent. Bruno sends a client's call and
pays the gas, and the call pays Bruno back as it runs: cash on delivery, in ETH or any token,
from whatever the call can reach. What the account holds, what the call releases or earns,
another account's signed payment: nothing is needed in advance.

Or pay by **x402**: a client holding only USDC signs an EIP-3009 authorization of the price,
and Bruno spends it in the call's own transaction. Bruno is its own facilitator, so the payment
moves only if the call succeeds and lands. Any x402 client can pay it: Ethereum's USDC is one of
x402's default assets.

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
Each runner is a new address that self-destructs at the end of its transaction, so nothing
granted to it outlives the transaction, and nothing is left in state.

## x402

`POST /{chain}/v1/x402`, x402 v2 over HTTP, the `exact` scheme with EIP-3009. The request is
the call; there's no separate quote or submission step:

1. **Ask.** `POST` the call: `{"call": {"to", "data"}, "gas", "authorizationList"?}`. The
   answer is `402`, its `PAYMENT-REQUIRED` header the price in USDC, priced for that call, and
   `maxTimeoutSeconds`, how long a paid request may be held.
2. **Pay.** Send the same request with `PAYMENT-SIGNATURE`: a `transferWithAuthorization` of
   the amount to the payee, signed for the token's EIP-712 domain (the requirement's `extra`).
3. **Settled.** The request is held until the transaction is included, then answered `200`,
   `PAYMENT-RESPONSE` carrying that transaction. If it's rejected or expires first, `402`,
   and the authorization was never used. Meanwhile its `Location` status URL shows the stage:
   waiting, sending, confirming, done.

The runner makes the transfer and the call in one transaction: a call that fails, or never
lands, costs nothing. Any account can pay for any call: the call needn't pay, nor the payer be
its sender. An amount above the price is a tip, ranked by it when the queue is full.

With x402's own Python client (`x402[httpx,evm]`):

```python
from eth_account import Account
import httpx
from x402 import x402Client
from x402.http.clients.httpx import x402AsyncTransport
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.exact import register_exact_evm_client

payer = Account.from_key(KEY)  # holds USDC, no ETH needed
x402 = x402Client()
register_exact_evm_client(x402, EthAccountSigner(payer), "eip155:*")
# a call can cost more than the client's default $1 cap
x402.set_spend_controls({"allowed_assets": [{"network": "eip155:1", "asset": USDC}],
                         "max_amount_per_payment": False})
async with httpx.AsyncClient(transport=x402AsyncTransport(x402), timeout=150) as http:
    paid = await http.post("https://bruno.mailam.app/ethereum/v1/x402",
                           json={"call": {"to": TO, "data": DATA}, "gas": hex(100_000)})
```

For wallet developers:

- **Set the HTTP timeout to at least `maxTimeoutSeconds`** (120 seconds on Ethereum). httpx's
  default is 5 seconds.
- **Allow the token where it isn't a default.** x402's clients pay only in their default
  assets: Ethereum's USDC is one, Sepolia's isn't.
- **Expect odd prices on Sepolia.** Its USDC pools are heavily unbalanced: faucet USDC is
  plentiful and Sepolia ETH scarce, so they value ETH at around 45,000 USDC. Bruno prices by
  them, so a call costs tens of Sepolia USDC, more than the faucet usually gives at once.
  Swap a little Sepolia ETH for USDC on those same pools instead.
- **Signing again doesn't pay twice.** A second payment for the same call while the first is
  pending is held for the first, its authorization never used.
- **Discovery:** `/.well-known/x402` lists each chain's endpoint, and `/openapi.json`
  describes it with `x-payment-info`. The 402 carries the `bazaar` extension.

Conformance is judged by x402's own SDK in the tests: its reference client pays through both
relays, its models parse every answer, and its facilitator code catalogs the endpoint. Where
Bruno differs from x402's text, and why: DESIGN.md, x402.

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
  landed, and an underpaying one was dropped and cost nothing. The Cloudflare Worker port,
  under workerd and on its own alarms, landed a submission there in its second slot, and
  settled an x402 payment in about 40 seconds: a client holding only USDC, its request held
  until the transaction landed, the runner leaving nothing in state.

### Networks

A transaction's gas limit is computed from the fork's gas schedule, never estimated. Which
schedule is in force is measured from the chain, not from activation times: any network on a
known fork (Prague, Amsterdam) works, and an unknown fork stops the relay rather than misprice.
A network also needs the deployer (`make <net>-deployer-deploy`) and builders that take
bundles, which are the scarcer.

### Builders

`eth_sendBundle` is Flashbots' API, and each builder extends it its own way. The mechanism
needs one property: a reverting transaction isn't included. Every builder that takes bundles
gives that to a single-transaction bundle (standalone); fewer let one transaction of a bundle
be dropped while the rest land (`droppingTxHashes`). As documented on 2026-10-10, and
probed on 2026-10-20 with a bundle that can't land:

- **`droppingTxHashes`:** Titan, BuilderNet (since v1.3), Quasar, BTCS. rbuilder
  implements it, with `version` unset or v2 and later. Titan's, BuilderNet's and Quasar's
  bundle hashes change with the field; BTCS's hash covers only the transactions, so shows
  nothing either way.
- **Not listed:** the Flashbots relay, which documents only `revertingTxHashes`; Blink;
  beaverbuild, which documents none, though its bundle hash changes with the field. Bruno sends
  them standalone bundles.
- **Gone:** rsync's endpoint no longer resolves.
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
| [DESIGN.md](DESIGN.md) | How Bruno provides it, and why: the deployer and runner, bundling, gas bounds and fork schedules, accounts, the queue and batches, funding, the treasury, price, risks, testing |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Running the server, and its configuration |

## Development

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
the builder rule (DESIGN.md, Bundling) to anvil.

`make check-sepolia` sends standalone bundles to `https://builder-sepolia.nflaig.dev/rpc`, which
needs an `X-Flashbots-Signature`.

## License

Dual-licensed under either [MIT](LICENSE-MIT) or [Apache-2.0](LICENSE-APACHE), at your option.
