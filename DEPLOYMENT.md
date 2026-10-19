# Deploying Bruno

## Running

```
python -m bruno.server --config CONFIG [--host 127.0.0.1] [--port 8402] [--float-key PATH]
make serve CONFIG=config.local.json PORT=8402 FLOAT_KEY=.secrets/…/wallet.key
```

One process serves every chain in the configuration, each under `/{chainId}`.
`--float-key` overrides every chain's `float_key`: one configuration, several nodes, each with
its own float.

On start, for each chain: the nodes are checked to be that chain, and each token's
`balanceOf` gas is measured against the payee. Submissions are kept in memory: a restart
loses them.

| Path | |
|---|---|
| `/{chainId}/v1/…` | REST (SPEC.md, Interface) |
| `POST /{chainId}` | JSON-RPC |
| `/schema` | OpenAPI |
| `/openrpc.json` | OpenRPC; also `rpc.discover` |

## Accounts

- **Float:** its key is the only secret. It funds the concurrency accounts, and they derive
  from it (BIP-32 with the float's key as seed, `m/44'/60'/0'/0/i`). It sends one funding
  transaction at a time, so its nonce has one user. Keep it holding enough ETH for the
  top-ups.
- **Concurrency accounts:** `accounts` of them, indices 0 to `accounts − 1`. They hold ETH
  between batches; `make <net>-sweep` drains them back to the float.
- **Payee:** receives the payments. Only its address is configured; converting what it
  collects into ETH for the float is another process's job.
- **Builder identities:** a key per builder that wants an `X-Flashbots-Signature`. It holds
  no funds.

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
3. **Every `batch_seconds`, a batch:** the top `accounts` of the queue that still cover the
   price and simulate (one that doesn't is rejected) are mapped onto accounts, each to the
   lowest-numbered free account covering its transaction at the fee ceiling, else the
   lowest-numbered free account, topped up to `funding_multiplier` × that cost. The top-ups
   are one funding transaction, not waited for: its accounts join once it lands. While one
   is in flight, a batch leaves out what needs funding. The next batch replaces the mapping.
4. **Each block:** settles what was sent and the funding; expires what's past its deadline;
   for each of the batch's submissions on a ready account, simulates it again, checks this
   block's price and the account's balance, signs, and sends every builder its bundles.

**Price:** the transaction's gas limit × (the next block's base fee + `priority_fee`), in the
token at the pools' price, × `margin_multiplier` × the token's `margin_multiplier`, rounded
up. **Fee ceiling**, for quotes and top-ups: the next base fee risen by ⅛ a block (EIP-1559's
most) for `quote_blocks` blocks (a quote, `quote_blocks` − 1), + `priority_fee`.

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
    {"address": "0x…USDC", "margin_multiplier": "1.005",
     "price": [{"kind": "uniswap-v3", "pool": "0x…USDC/WETH"}]},
    {"address": "0x…UNI", "margin_multiplier": "1.02",
     "price": [{"kind": "uniswap-v3", "pool": "0x…UNI/USDC"},
               {"kind": "uniswap-v3", "pool": "0x…USDC/WETH"}]},
    {"address": "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"}
  ],
  "accounts": 4,
  "max_pending": 16,
  "max_deadline_blocks": 50,
  "priority_fee": 1000000000,
  "max_gas": 1000000,
  "max_call_bytes": 40960,
  "margin_multiplier": "1.1",
  "price_ttl_seconds": 12,
  "quote_blocks": 5,
  "batch_seconds": 12,
  "funding_multiplier": "2",
  "funding_timeout_seconds": 120,
  "float_key": ".secrets/…/wallet.key"
}}}
```

### Chain

| Field | Default | |
|---|---|---|
| `rpc` | required | Nodes, in order; the next is tried only when one fails over HTTP. |
| `payee` | required | Receives the payments. |
| `weth` | required | The chain's wrapped ether, where every price path ends. |
| `float_key` | none | Path to the float's key; `--float-key` overrides it. One of the two is required. |

### Builders: `bundlers[]`

| Field | Default | |
|---|---|---|
| `url` | required | The builder's `eth_sendBundle` endpoint. |
| `mode` | required | `dropping` (one bundle a block, every submission in `droppingTxHashes`) or `standalone` (a bundle a submission). |
| `identity_key` | none | Path to the key signing `X-Flashbots-Signature`, for a builder that wants one. |

Every bundle goes to every builder. Neither mode carries funding: accounts are funded by the
batch.

### Tokens: `tokens[]`

| Field | Default | |
|---|---|---|
| `address` | required | An asset accepted in payment: a token, or ERC-7528's `0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE` for ETH. |
| `price` | required, none for ETH | The pools from the token to `weth`, in order: `{"kind": "uniswap-v2" \| "uniswap-v3", "pool"}`. Each pool's `token0()` and `token1()` say which side is which. |
| `margin_multiplier` | `"1"` | The token's margin, on top of the chain's. |

A pool's spot price is read (v2 `getReserves`, v3 `slot0`); a spot price can be moved within
a block, so choose deep pools.

### Pricing

| Field | Default | |
|---|---|---|
| `margin_multiplier` | `"1"` | The chain's margin over cost. |
| `priority_fee` | required | Wei a gas, offered by every transaction. |
| `price_ttl_seconds` | `0` | How long a token's price is reused before the pools are read again. |
| `quote_blocks` | `1` | How many blocks of base-fee rise a quote, and a top-up, allow for. |

### Limits

| Field | Default | |
|---|---|---|
| `max_gas` | required | The most gas a client's call may be given. |
| `max_call_bytes` | required | The longest call data. |
| `max_deadline_blocks` | required | The furthest ahead a `maxBlockNumber` may be. |

### Capacity

| Field | Default | |
|---|---|---|
| `accounts` | required | Concurrency accounts: a batch's size, so transactions in flight a block. |
| `max_pending` | required | The queue's size. |
| `batch_seconds` | `12` | How often a batch is made. |

Each block re-simulates up to `accounts` submissions (one `eth_call` each); each batch plans
up to `max_pending`.

### Funding

| Field | Default | |
|---|---|---|
| `funding_multiplier` | `"1"` | A top-up covers this many times its transaction's cost at the fee ceiling. Above 1, an account covers its next transactions without a funding round. |
| `funding_timeout_seconds` | `120` | How long a funding transaction is waited for before it's given up on. |

The accounts hold at most `accounts` × `funding_multiplier` × the largest transaction's cost
at the fee ceiling. The lowest-numbered accounts are used first, so those that stay funded
follow the actual concurrency.
