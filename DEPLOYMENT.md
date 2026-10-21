# Deploying Bruno

## Running

```
python -m bruno.server --config CONFIG [--host 127.0.0.1] [--port 8402] [--float-key PATH] [--payee ADDRESS] [--payee-key PATH]
make serve [CONFIG=…] [PORT=8402] [FLOAT_KEY=…] [PAYEE=…] [PAYEE_KEY=…]
```

Each option may be an environment variable instead; an option given wins:

| Option | Variable |
|---|---|
| `--config` | `BRUNO_CONFIG` |
| `--host` | `BRUNO_HOST` |
| `--port` | `BRUNO_PORT` |
| `--float-key` | `BRUNO_FLOAT_KEY` |
| `--payee` | `BRUNO_PAYEE` |
| `--payee-key` | `BRUNO_PAYEE_KEY` |

`config.default.json` serves Ethereum and Sepolia: public nodes, every builder Bruno sends to,
and ETH, WETH and the USD stablecoins with deep Uniswap pools. It names no payee and no float:
give both (`--payee`, `--float-key`).

`make serve` runs with nothing given: `config.local.json` (gitignored) if there is one, else
`config.default.json`; the float `.secrets/float.key` and the payee in `.secrets/payee.address`
(from `.secrets/payee.key`), each created the first time, only addresses printed; and the
treasuries, with `.secrets/payee.key` (unless `PAYEE` names another payee; `PAYEE_KEY=` runs
none). Fund the float's address to send.

One process serves every chain in the configuration, each under `/{chain}`: its chain id, or
its network's name.
`--float-key` overrides every chain's `float_key`: one configuration, several nodes, each with
its own float. `--payee` is the payee of every chain that doesn't name its own, in place of the
configuration's. `--payee-key` runs the treasury (DESIGN.md, Treasury) of each chain that
configures one and whose payee the key is.

Nothing is read on start: the server starts whatever the chains are doing. Each chain is
measured when it's first needed, and again once `measure_seconds` old (DESIGN.md, Measured,
not assumed): its nodes' chain id, the fork in force, the deployer's code, each token's
`balanceOf` gas, decimals and symbol, each x402 token's `transferWithAuthorization` signed for
its `eip3009` domain, and the payee's balance of it. A token that fails is suspended; a chain
whose nodes are another chain, whose fork no schedule matches, or whose deployer is missing is
unavailable (503); the root lists either. Submissions are kept in memory: a restart loses
them, and a status URL given out before it answers 404 after.

**Before deploying,** `make preflight` checks every chain against its live nodes, read-only:
the same measurements, every token priced, the treasury's router and WETH, every builder
answering. `make worker-deploy` runs it first and deploys nothing if a chain isn't ready.
`make <net>-deployer-deploy` deploys the runner's deployer where it's missing (`KEY=` the key
paying). **After deploying,** `make <net>-verify` sends real payments through the deployed
URL: one a unit short, which must be refused, one that must land, and one over x402 where it's
taken. The client is derived from the float's key and funded by the payee; what's spent is
gas.

| Path | |
|---|---|
| `/` | The chains served and the documents: JSON, or a page with a QR code of the payee for donations; each chain's float and concurrency accounts, its treasury's last run, and what's wrong with it as last measured |
| `/{chain}/v1/…` | REST (SPEC.md, Interface) |
| `POST /{chain}` | JSON-RPC |
| `POST /{chain}/v1/x402` | A call paid over x402, on a chain with an `eip3009` token (DESIGN.md, x402) |
| `/.well-known/x402` | Each chain's x402 endpoint, for discovery |
| `/openapi.json` | OpenAPI, the x402 operation with its `x-payment-info` |
| `/openrpc.json` | OpenRPC; also `rpc.discover` |

**Behind a reverse proxy,** the server must see the scheme and host clients used: x402's
`resource.url` and its status URLs are built from them, and discovery wants `https://`. The
proxy passes `X-Forwarded-Proto` and `Host`; uvicorn trusts forwarded headers only from the
addresses in `FORWARDED_ALLOW_IPS` (by default `127.0.0.1`).

### Docker

`ghcr.io/mai-lam-labs/bruno`, built and pushed by CI from `main` (`:main`, `:sha-…`) and from
version tags (`:1.2.3`). It runs the server on port 8402, on every interface
(`BRUNO_HOST=0.0.0.0`), with `config.default.json` built in at `/etc/bruno/config.json`
(`BRUNO_CONFIG`). The float's key is mounted, never built in; options after the image name, or
variables, set the rest:

```
docker run -p 8402:8402 -v ./float.key:/float.key:ro \
  -e BRUNO_FLOAT_KEY=/float.key -e BRUNO_PAYEE=0x… ghcr.io/mai-lam-labs/bruno
```

A configuration of its own is mounted in the default's place, or anywhere `BRUNO_CONFIG`
names.

A bundler's `identity_key`, if a configuration names one, is a path inside the container:
mount it too.

### Cloudflare

The Worker (`worker/`), deployed through Cloudflare's API with an account API token: no
Wrangler. `.secrets/cloudflare/` holds `account_id` and `api_token` (Workers Scripts: Edit,
and for the domain, Workers Routes: Edit and Zone: Read on the domain's zone).

```
make worker-deploy    create the Worker, or replace its code and bindings; CONFIG checked first
make worker-secret    FLOAT_KEY and PAYEE_KEY from their key files, never shown: addresses printed
make worker-domain    WORKER_DOMAIN routed to the Worker, its DNS record and certificate made
```

| Variable | Default | |
|---|---|---|
| `WORKER_NAME` | `bruno` | The Worker's name in the account. |
| `WORKER_DOMAIN` | `bruno.mailam.app` | Its hostname, in whichever of the account's zones holds it. |
| `WORKER_CONFIG` | `config.default.json` | The configuration, bound as `CONFIG`. |
| `WORKER_PAYEE` | `.secrets/payee.address` | The default payee, bound as `PAYEE`. |
| `WORKER_FLOAT_KEY` | `.secrets/float.key` | The float's key file, set as the `FLOAT_KEY` secret. |
| `WORKER_PAYEE_KEY` | `.secrets/payee.key`, unless `WORKER_PAYEE` is given | The payee's key file, set as the `PAYEE_KEY` secret: the treasuries. Empty, none is set. |
| `CLOUDFLARE` | `.secrets/cloudflare` | The directory holding `account_id` and `api_token`. |

The first deploy creates the relay's SQLite-backed Durable Object class; later ones keep it,
and keep the secrets. Each deploy sets the hourly cron trigger that runs the treasuries. The compatibility date is `workerd.capnp`'s, the runtime the Worker is
tested on. The tool reaches only `api.cloudflare.com`, and reads only the files it's given.

**First deploy:** the three in order, then `curl https://$WORKER_DOMAIN/`. Until `FLOAT_KEY` is
set, a chain answers 404 (`FLOAT_KEY: no private key bound`); the domain's certificate takes a
minute or so. **Afterwards:** `make worker-deploy` alone, for new code or configuration: every
chain's object restarts on the next request and reads it. A chain's first request after that
is slower, its nodes checked and its tokens measured.

What the Worker reads:

| Binding | Kind | |
|---|---|---|
| `RELAY` | Durable Object namespace | `ChainRelay`, one object a chain: its queue, batch and funding in SQLite. |
| `CONFIG` | text | The configuration, as above. `float_key` is ignored; a bundler's `identity_key` names a secret binding holding its key. |
| `PAYEE` | text | The payee of every chain that doesn't name its own, in place of `CONFIG`'s. |
| `FLOAT_KEY` | secret | The float's key, as 0x-prefixed hex. |
| `PAYEE_KEY` | secret | The payee's key, as `FLOAT_KEY`, if given: each chain with a `treasury` whose payee it is runs one. |
| `TEST_HOOKS` | — | Never bound in a deployment: only the local `workerd.capnp` sets it. |

A chain the configuration doesn't name is refused before any object is made. An object with
nothing queued holds no alarm and is evicted: idle, a deployment costs only its requests.

## Accounts

- **Float:** its key is the only secret. It funds the concurrency accounts, and they derive
  from it (BIP-32 with the float's key as seed, `m/44'/60'/0'/0/i`). It sends one funding
  transaction at a time, so its nonce has one user. Keep it holding enough ETH for the
  top-ups.
- **Concurrency accounts:** `accounts` of them, indices 0 to `accounts − 1`. They hold ETH
  between batches; `make <net>-sweep` drains them back to the float.
- **Payee:** receives the payments. Given its key, the treasury converts what it collects
  into ETH for the float, keeping a reserve of ETH for its own gas (DESIGN.md, Treasury).
  Fund it with a little ETH to start.
- **Builder identities:** the key signing a builder's `X-Flashbots-Signature`: the float's,
  unless a builder names its own. It needn't hold funds.

Keys are files holding the private key as hex, given by path, never inline.

## How a chain runs

1. **Taking a submission:** its token and payee, `gas` against `max_gas`, the call's length
   against `max_call_bytes`, its authorizations' chain ids, a `maxBlockNumber` at most
   `max_deadline_blocks` ahead, `amount` against the next block's price, then `eth_call` of
   its transaction.
2. **The queue:** at most `max_pending` submissions, none holding an account, ranked by
   profit: the amount in wei less the transaction's gas limit × the next block's fee. A full
   queue takes a submission only by evicting its least profitable ("outbid"); otherwise
   -32013 says what amount would be taken.
3. **Every `batch_seconds` while there's work, a batch** (the first at once): the top `accounts` of the queue that still cover the
   price and simulate (one that doesn't is rejected) are mapped onto accounts, each to the
   lowest-numbered free account covering its transaction at the fee ceiling, else the
   lowest-numbered free account, topped up to `funding_multiplier` × that cost. The top-ups
   are one funding transaction, not waited for: its accounts join once it lands. While one
   is in flight, a batch leaves out what needs funding. The next batch replaces the mapping.
4. **Each block, while there's work:** settles what was sent and the funding; expires what's past its deadline;
   for each of the batch's submissions on a ready account, simulates it again, checks this
   block's price and the account's balance, signs, and sends every builder its bundles.

The clock runs only while a submission is pending or funding is in flight: idle, the relay
reads nothing from its nodes (DESIGN.md, Clock and reads).

**Tip:** the larger of the median of the last 20 blocks' median tips and the median of the
last 5 (eth_feeHistory's 50th percentile). Every transaction, funding included, offers it.
**Our fee:** the gas limit × the larger of the tip and `min_fee_per_gas`, and at least
`min_fee`. **Price:** the gas limit × (the next block's base fee + the tip), + our fee, in the
token at the pools' price, × `margin_multiplier` × the token's `margin_multiplier`, rounded
up. **Quote:** the same, with the next base fee risen by ⅛ a block (EIP-1559's most) for
`quote_blocks` − 1 blocks and twice the tip, for their rising. **Fee ceiling**, for top-ups:
the next base fee risen for `quote_blocks` blocks, + 2 × the tip.

## Configuration

JSON, by chain id. Multipliers are string `Decimal`s, at least `"1"`: `"1.1"` is 10% over.

```json
{"chains": {"11155111": {
  "rpc": ["https://…", "https://…"],
  "payee": "0x…",
  "weth": "0x…",
  "bundlers": [
    {"url": "https://…", "mode": "standalone", "identity_key": ".secrets/…/identity.key"},
    {"url": "https://…", "mode": "dropping"}
  ],
  "tokens": [
    {"address": "0x…USDC", "margin_multiplier": "1.003",
     "price": [{"kind": "uniswap-v3", "pool": "0x…USDC/WETH"}]},
    {"address": "0x…UNI", "margin_multiplier": "1.02",
     "price": [{"kind": "uniswap-v3", "pool": "0x…UNI/USDC"},
               {"kind": "uniswap-v3", "pool": "0x…USDC/WETH"}]},
    {"address": "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"}
  ],
  "accounts": 4,
  "max_pending": 16,
  "max_deadline_blocks": 50,
  "max_gas": 1000000,
  "max_call_bytes": 40960,
  "min_fee_per_gas": 10000000,
  "min_fee": {"token": "0x…USDC", "amount": "0.01"},
  "price_ttl_seconds": 60,
  "quote_blocks": 3,
  "batch_seconds": 12,
  "funding_multiplier": "6",
  "funding_timeout_seconds": 120,
  "float_key": ".secrets/…/wallet.key"
}}}
```

### Chain

| Field | Default | |
|---|---|---|
| `rpc` | required | Nodes, in order; the next is tried only when one fails over HTTP. |
| `payee` | the configuration's | Receives the payments. Without one, the top-level `payee` (`{"payee": …, "chains": …}`), or `--payee`'s in its place. |
| `weth` | required | The chain's wrapped ether, where every price path ends. |
| `float_key` | none | Path to the float's key; `--float-key` overrides it. One of the two is required. |

### Builders: `bundlers[]`

| Field | Default | |
|---|---|---|
| `url` | required | The builder's `eth_sendBundle` endpoint. |
| `mode` | required | `dropping` (one bundle a block, every submission in `droppingTxHashes`) or `standalone` (a bundle a submission). |
| `identity_key` | the float's | Path to the key signing `X-Flashbots-Signature`. |

Every bundle goes to every builder. Neither mode carries funding: accounts are funded by the
batch.

### Tokens: `tokens[]`

| Field | Default | |
|---|---|---|
| `address` | required | An asset accepted in payment: a token, or ERC-7528's `0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE` for ETH. |
| `price` | required, none for ETH or WETH | The pools from the token to `weth`, in order: `{"kind": "uniswap-v2" \| "uniswap-v3", "pool"}`. Each pool's `token0()` and `token1()` say which side is which. |
| `margin_multiplier` | `"1"` | The token's margin, on top of the chain's: what converting it costs, `"1.003"` through a 0.3% pool. |
| `eip3009` | none | `{"name", "version"}`: the token's EIP-712 domain, its `name()` and `version()`. The token is then taken over x402 too, paid by `transferWithAuthorization`. Ethereum's USDC: `"USD Coin"`, `"2"`; Sepolia's: `"USDC"`, `"2"`. |

A pool's spot price is read (v2 `getReserves`, v3 `slot0`); a spot price can be moved within
a block, so choose deep pools.

### Pricing

| Field | Default | |
|---|---|---|
| `margin_multiplier` | `"1"` | The chain's margin, over cost and our fee. |
| `min_fee_per_gas` | `0` | The least our fee is, in wei a gas: what we earn when the market tips little. |
| `min_fee` | none | The least our fee is on a transaction: `{"token", "amount"}`, the amount in whole tokens (by the token's `decimals()`), the token one the chain accepts, priced through its pools. `"0.01"` of USDC is a cent. |
| `price_ttl_seconds` | `60` | How long a token's price is reused before the pools are read again. Concurrent requests for a price being read share the one read. |
| `quote_blocks` | `1` | How many blocks of base-fee rise a quote, and a top-up, allow for. |

### Limits

| Field | Default | |
|---|---|---|
| `max_gas` | required | The most gas a client's call may be given. |
| `max_call_bytes` | required | The longest call data. |
| `max_deadline_blocks` | required | The furthest ahead a `maxBlockNumber` may be. |
| `max_authorizations` | `1` | The most EIP-7702 authorizations a submission may carry: each costs reads to plan. |

### Capacity

| Field | Default | |
|---|---|---|
| `accounts` | required | Concurrency accounts: a batch's size, so transactions in flight a block. |
| `max_pending` | required | The queue's size. |
| `retention_seconds` | `3600` | How long a finished submission's status is kept before it's forgotten. |
| `measure_seconds` | `3600` | How old the chain's measurements may get before they're taken again: the longest a fork or token change goes unnoticed. |
| `batch_seconds` | `12` | How often a batch is made. |
| `slot_seconds` | `12` | The chain's block time: the next block is a slot after the latest. |

Each block re-simulates up to `accounts` submissions; each batch plans and simulates up to
`max_pending`. Each is one batched request a kind of read, whatever the number.

### Funding

| Field | Default | |
|---|---|---|
| `funding_multiplier` | `"1"` | A top-up covers this many times its transaction's cost at the fee ceiling. Above 1, an account covers its next transactions without a funding round. |
| `funding_timeout_seconds` | `120` | How long a funding transaction is waited for before it's given up on. |

The accounts hold at most `accounts` × `funding_multiplier` × the largest transaction's cost
at the fee ceiling. The lowest-numbered accounts are used first, so those that stay funded
follow the actual concurrency.

### Treasury: `treasury`

Without it, the chain has no treasury. It runs only given the payee's key.

| Field | Default | |
|---|---|---|
| `router` | required | Uniswap's SwapRouter02 on the chain: `0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45` on Ethereum, `0x3bFA4769FB09eefC5a80d6E87c3B9C650f7Ae48E` on Sepolia. |
| `interval_seconds` | `21600` | How often it runs. The Worker checks hourly. |
| `runway_days` | `"2"` | The float and accounts are topped up to this many days of their measured spend, and to at least every account funded once for the largest transaction. |
| `max_cost_multiplier` | `"1.02"` | A conversion goes ahead only if it delivers at least its value / this, unless the float is running dry. |
| `slippage_multiplier` | `"1.005"` | A swap takes at least the pools' price, less the token's margin, / this. |

The payee's reserve is derived: one run's gas at its max fee, × `funding_multiplier`. Each
chain's last run shows on the root.

**Starting one:** deploy with the payee's key (`make serve`, or `make worker-deploy
worker-secret`), then send the payee a little ETH and some of a token the chain accepts. The
first run (at once in the Python server; within the hour on the Worker) approves the router
for each token, and does nothing else; the next (two minutes later; the next hour) converts
what's needed and tops the float up. The root shows each run.
