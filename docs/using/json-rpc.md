---
description: "Using Bruno over JSON-RPC: the postpaid_ methods at POST /{chain}, with real requests and responses from Sepolia; errors; rpc.discover, the OpenRPC document, a playground and an inspector"
---

# JSON-RPC

Each chain has a JSON-RPC endpoint at `POST /{chain}`, where `{chain}` is either the chain ID
in decimal or the network's name: for example, `https://bruno.mailam.app/sepolia`. The methods
are the same operations as [REST](rest.md). Each one takes the REST request as its only
parameter and returns the REST response. The
[specification's interface](../SPEC.md#interface) is the normative reference.

| Method | REST equivalent | Parameter | Result |
|---|---|---|---|
| `postpaid_config` | `GET /v1/config` | none | `payee`, `tokens`, `maxGas`, `maxCallBytes` |
| `postpaid_quote` | `GET /v1/quote` | `{gas, callBytes, tokens, authority?, payer?, deployment?, verifyGas?}` | `validUntilBlock`, `quotes: [{token, amount}]` |
| `postpaid_sendTransaction` | `POST /v1/submissions` | the submission: `{call, authorizationList?, gas, payment, deployment?, maxBlockNumber}` | `id` |
| `postpaid_getStatus` | `GET /v1/submissions/{id}` | `id` | `status`, `stage`, `attempts`, `transactionHash`?, `blockNumber`?, `confirmations`?, `reason`?, `revertData`? |
| `rpc.discover` | none | none | The OpenRPC document, with this chain's endpoint as its server |

The [REST page](rest.md) explains each field and the shape of a submission, including
[signed transfers](rest.md#paying-with-a-signed-transfer) and
[smart wallets](rest.md#smart-wallets). The one difference is in a quote: REST takes a
deployment as three parameters, `account`, `factory` and `factoryData`, and JSON-RPC takes it as
one object, `deployment: {account, factory, factoryData}`, as a submission does.

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
    "tokens": [
      "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE",
      "0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14",
      "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238"
    ],
    "maxGas": "0xf4240",
    "maxCallBytes": "0xa000"
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
      "callBytes": "0x200",
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
    ]
  }
}
```

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
      "callBytes": "0x200",
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
    "data": "gas"
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

Getting terms, quotes and statuses is free. A submission needs a signed call, which the
[TypeScript client](typescript.md) can make for you.
