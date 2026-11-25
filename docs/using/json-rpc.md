---
description: "Using Bruno over JSON-RPC: the postpaid_ methods at POST /{chain}, with real requests and responses; a sponsorship quoted and submitted; errors; rpc.discover, the OpenRPC document, a playground and an inspector"
---

# JSON-RPC

Each chain has a JSON-RPC endpoint at `POST /{chain}`, where `{chain}` is either the chain ID
in decimal or the network's name: for example, `https://bruno.mailam.app/sepolia`. The methods
are the same operations as [REST](rest.md). Each one takes the REST request as its only
parameter and returns the REST response. The
[specification's interface](../SPEC.md#interface) is the normative reference.

| Method | REST equivalent | Parameter | Result |
|---|---|---|---|
| `postpaid_config` | `GET /v1/config` | none | `payee`, `forwarder`, `tokens`, `maxGas`, `maxCallDataLength` |
| `postpaid_quote` | `POST /v1/quote` | `{tokens, call?, callDataLength?, gas?, authorizationList?, authority?, payer?, deployment?, verifyGas?}`: the call whole, its gas estimated unless given, or its data's length and its gas | `validUntilBlock`, `quotes: [{token, amount, breakdown, typedData?}]`, `gas` |
| `postpaid_sendTransaction` | `POST /v1/submissions` | the submission: `{call, authorizationList?, gas, payment, deployment?, maxBlockNumber}` | `id` |
| `postpaid_quoteSponsorship` | `POST /v1/sponsorships/quote` | `{tokens, account, transactions?, authorizationList?, call?, gas?, maxBlockNumber?}`: an account's own transactions, each signed already or an object to be signed | `validUntilBlock`, `maxBlockNumber`, `forwarder`, `gas`?, `transactions`, `quotes: [{token, amount, payment?}]`: what to sign |
| `postpaid_sponsor` | `POST /v1/sponsorships` | the sponsorship: `{account, transactions?, authorizationList?, call?, gas?, payment, maxBlockNumber}` | `id` |
| `postpaid_getStatus` | `GET /v1/submissions/{id}` | `id`, a submission's or a sponsorship's | `status`, `stage`, `attempts`, `transactionHashes`?, `blockNumber`?, `confirmations`?, `reason`?, `revertData`? |
| `eth_estimateUserOperationGas` | `POST /v1/user-operations/gas` | `userOperation`, `entryPoint` (two parameters, as ERC-7769 has them) | `preVerificationGas`, `verificationGasLimit`, `callGasLimit` |
| `postpaid_quoteUserOperation` | `POST /v1/user-operations/quote` | `userOperation`, `entryPoint`, the op paying by its calls, its payment a placeholder | `validUntilBlock`, `quotes: [{token, amount}]`: what to pay before it's signed |
| `eth_sendUserOperation` | `POST /v1/user-operations` | `userOperation`, `entryPoint` | the op's hash |
| `eth_getUserOperationReceipt` | `GET /v1/user-operations/{hash}` | the op's hash | ERC-7769's receipt, or null until it's included |
| `pimlico_getUserOperationGasPrice`, `zd_getUserOperationGasPrice` | none | none | The price an op paying by its fees is asked, as both its fees: `{slow, standard, fast}` alike |
| `eth_supportedEntryPoints` | none | none | EntryPoint v0.8's and v0.7's addresses |
| `eth_chainId` | none | none | The chain's id |
| `rpc.discover` | none | none | The OpenRPC document, with this chain's endpoint as its server |

Any other method is refused as not found. The endpoint isn't a node: read the chain through
your own.

The [REST page](rest.md) explains each field and the shape of a submission, including
[signed transfers](rest.md#paying-with-a-signed-transfer) and
[smart wallets](rest.md#smart-wallets).

## Examples

From Sepolia:

```console
$ curl -s https://bruno.mailam.app/sepolia -H 'content-type: application/json' -d @- <<'EOF' | jq
{"jsonrpc": "2.0", "id": 1, "method": "postpaid_config", "params": []}
EOF
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "payee": "0xfBC0cF0d2C09C57222C871B1cbF9b153bfFA0049",
    "forwarder": "0x2Dc27cb0fbce57bD1122b2CAa61D05d9E6B23838",
    "tokens": [
      "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE",
      "0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14",
      "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238"
    ],
    "maxGas": "0x1e8480",
    "maxCallDataLength": "0xa000"
  }
}

$ curl -s https://bruno.mailam.app/sepolia -H 'content-type: application/json' -d @- <<'EOF' | jq
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "postpaid_quote",
  "params": [
    {
      "gas": "0x186a0",
      "callDataLength": "0x200",
      "tokens": ["0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"]
    }
  ]
}
EOF
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "validUntilBlock": "0xb7127a",
    "quotes": [
      {
        "token": "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE",
        "amount": "0x39825521640b6"
      }
    ],
    "gas": "0x186a0"
  }
}
```

## Sponsoring an account's transactions

`postpaid_quoteSponsorship` and `postpaid_sponsor` are for an account's own signed
transactions, where `postpaid_quote` and `postpaid_sendTransaction` are for a call the relay
makes. The [REST page](rest.md#sponsoring-an-accounts-own-transactions) explains the fields
and what to check before signing. The flow has three steps:

1. Quote. Give the account and each transaction it wants to send as an object, `{to, data,
   value}`. The answer fills each one in to sign, and adds the payment to sign after them.
2. Sign each transaction of the answer, and the payment of the token you pay in, without
   sending them.
3. Submit the signed transactions, paying the answer's `forwarder` the token's `amount`, by
   the answer's `maxBlockNumber`.

This example is from a local test chain (chain id 31337, a relay at `127.0.0.1:8402`), not
from Sepolia. The account holds 100 USDC and no ETH, and sends 2.5 USDC. The quote is 7.405571
USDC (`0x710003`):

```console
$ curl -s http://127.0.0.1:8402/31337 -H 'content-type: application/json' -d @- <<'EOF' | jq .result
{
  "jsonrpc": "2.0",
  "id": 4,
  "method": "postpaid_quoteSponsorship",
  "params": [
    {
      "tokens": ["0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"],
      "account": "0xa8159029b550e3599E5983d9e6342579FB369DEa",
      "transactions": [
        {
          "to": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
          "data": "0xa9059cbb00000000000000000000000020c805fd232fd4e9c0f639b6970cf6794cb0ecc400000000000000000000000000000000000000000000000000000000002625a0"
        }
      ]
    }
  ]
}
EOF
{
  "validUntilBlock": "0x17",
  "maxBlockNumber": "0x48",
  "forwarder": "0xdF1c30Ce0B72a3c0547338Fa76DdDB790a068c3A",
  "transactions": [
    {
      "type": "0x2",
      "chainId": "0x7a69",
      "nonce": "0x0",
      "data": "0xa9059cbb00000000000000000000000020c805fd232fd4e9c0f639b6970cf6794cb0ecc400000000000000000000000000000000000000000000000000000000002625a0",
      "value": "0x0",
      "gas": "0xec27",
      "maxFeePerGas": "0x497002e3a",
      "maxPriorityFeePerGas": "0x3b9aca00",
      "to": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
    }
  ],
  "quotes": [
    {
      "token": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
      "amount": "0x710003",
      "payment": {
        "type": "0x2",
        "chainId": "0x7a69",
        "nonce": "0x1",
        "data": "0xa9059cbb000000000000000000000000df1c30ce0b72a3c0547338fa76dddb790a068c3a0000000000000000000000000000000000000000000000000000000000710003",
        "value": "0x0",
        "gas": "0xed8d",
        "maxFeePerGas": "0x497002e3a",
        "maxPriorityFeePerGas": "0x3b9aca00",
        "to": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
      }
    }
  ]
}
```

The submission carries what was signed, as raw transactions in the same order, the payment
last:

```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "method": "postpaid_sponsor",
  "params": [
    {
      "account": "0xa8159029b550e3599E5983d9e6342579FB369DEa",
      "transactions": ["0x02f8…the transfer, signed", "0x02f8…the payment, signed"],
      "payment": {
        "token": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "payee": "0xdF1c30Ce0B72a3c0547338Fa76DdDB790a068c3A",
        "amount": "0x710003"
      },
      "maxBlockNumber": "0x48"
    }
  ]
}
```

Its result is `{"id": "0x…"}`, and `postpaid_getStatus` follows it by that id, as it does a
submission. [A whole script](sponsorship/index.md) does all three steps with the TypeScript client.

The OpenRPC document's own example for `postpaid_quoteSponsorship`, which the playground below
runs, only shows a quote's shape: it asks about `0x…dEaD`, an account nobody can sign for.

## Errors

Errors use JSON-RPC's error object, with the codes listed in the
[specification](../SPEC.md#interface), and the same `data` as over REST:

```console
$ curl -s https://bruno.mailam.app/sepolia -H 'content-type: application/json' -d @- <<'EOF' | jq
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "postpaid_quote",
  "params": [
    {
      "gas": "0x01",
      "callDataLength": "0x200",
      "tokens": ["0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"]
    }
  ]
}
EOF
{
  "jsonrpc": "2.0",
  "id": 3,
  "error": {
    "code": -32602,
    "message": "gas: a quantity, as hex",
    "data": {
      "reason": "malformed",
      "field": "gas"
    }
  }
}
```

## Try it

The methods are described by an OpenRPC 1.3.2 document. It's served at
[`/openrpc.json`](https://bruno.mailam.app/openrpc.json), listing every chain's endpoint as a
server, and `rpc.discover` returns it with just that chain's endpoint.

- **Browse it and try each method** in the OpenRPC Playground:
  [playground.open-rpc.org/?url=https://bruno.mailam.app/sepolia](https://playground.open-rpc.org/?url=https://bruno.mailam.app/sepolia).
  Give the playground a chain's endpoint, not `/openrpc.json`. It reads the document from the
  endpoint with `rpc.discover`, and "Try It Now" sends to that endpoint. Given a `.json` file,
  it sends requests to its own site instead. "Try It Now" only opens the request in the
  inspector; press the inspector's play button to send it.
- **Send requests** with the OpenRPC Inspector:
  [inspector.open-rpc.org/?url=https://bruno.mailam.app/sepolia](https://inspector.open-rpc.org/?url=https://bruno.mailam.app/sepolia).
  The relay accepts requests from any origin, so the inspector can call it directly.

Getting terms, quotes and statuses is free. A submission needs a signed call, and a
sponsorship signed transactions, which the [TypeScript client](typescript.md) can make for you.
