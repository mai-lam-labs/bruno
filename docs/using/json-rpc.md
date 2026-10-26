---
description: "Bruno over JSON-RPC: the postpaid_ methods at POST /{chain}, with real requests and answers on Sepolia; errors; rpc.discover, the OpenRPC document, a playground and an inspector"
---

# JSON-RPC

Each chain's endpoint is `POST /{chain}`, its chain id in decimal or its network's name:
`https://bruno.mailam.app/sepolia`. The methods are [REST's](rest.md) operations, each taking
the REST request as its one parameter and answering the REST response. The normative text is
the [specification's interface](../SPEC.md#interface).

| Method | REST | Parameter | Result |
|---|---|---|---|
| `postpaid_config` | `GET /v1/config` | | `payee`, `tokens`, `maxGas`, `maxCallBytes` |
| `postpaid_quote` | `GET /v1/quote` | `{gas, callBytes, tokens, authority?}` | `validUntilBlock`, `quotes: [{token, amount}]` |
| `postpaid_sendTransaction` | `POST /v1/submissions` | the submission: `{call, authorizationList?, gas, payment, maxBlockNumber}` | `id` |
| `postpaid_getStatus` | `GET /v1/submissions/{id}` | `id` | `status`, `stage`, `attempts`, `transactionHash`?, `blockNumber`?, `confirmations`?, `reason`?, `revertData`? |
| `rpc.discover` | | | The OpenRPC document, the chain's endpoint its server |

What each field means, and a submission's shape: [REST](rest.md).

## Examples

From Sepolia:

```console
$ curl https://bruno.mailam.app/sepolia -H 'content-type: application/json' \
    -d '{"jsonrpc": "2.0", "id": 1, "method": "postpaid_config", "params": []}'
{"jsonrpc":"2.0","id":1,"result":{"payee":"0xfBC0cF0d2C09C57222C871B1cbF9b153bfFA0049","tokens":["0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE","0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14","0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238"],"maxGas":"0xf4240","maxCallBytes":"0xa000"}}

$ curl https://bruno.mailam.app/sepolia -H 'content-type: application/json' \
    -d '{"jsonrpc": "2.0", "id": 2, "method": "postpaid_quote", "params": [{"gas": "0x186a0", "callBytes": "0x200", "tokens": ["0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"]}]}'
{"jsonrpc":"2.0","id":2,"result":{"validUntilBlock":"0xb7127a","quotes":[{"token":"0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE","amount":"0x39825521640b6"}]}}
```

## Errors

JSON-RPC's error object, with the codes the [specification](../SPEC.md#interface) lists, and the
same `data` as over REST:

```console
$ curl https://bruno.mailam.app/sepolia -H 'content-type: application/json' \
    -d '{"jsonrpc": "2.0", "id": 3, "method": "postpaid_quote", "params": [{"gas": "0x01", "callBytes": "0x200", "tokens": ["0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"]}]}'
{"jsonrpc":"2.0","id":3,"error":{"code":-32602,"message":"gas: a quantity, as hex","data":"gas"}}
```

## OpenRPC, and trying it

The methods are described by an OpenRPC 1.3.2 document, at
[`/openrpc.json`](https://bruno.mailam.app/openrpc.json) with every chain's endpoint as a
server, and answered by `rpc.discover` with the chain's own.

- **Browse it** in the OpenRPC Playground:
  [playground.open-rpc.org/?schemaUrl=https://bruno.mailam.app/openrpc.json](https://playground.open-rpc.org/?schemaUrl=https://bruno.mailam.app/openrpc.json).
- **Send requests** with the OpenRPC Inspector:
  [inspector.open-rpc.org/?url=https://bruno.mailam.app/sepolia](https://inspector.open-rpc.org/?url=https://bruno.mailam.app/sepolia).
  The relay answers any origin, so it's reached directly.

Terms, quotes and statuses are free to try. A submission needs a signed call: the
[TypeScript client](typescript.md) makes one.
