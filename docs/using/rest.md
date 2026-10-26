---
description: "Bruno over REST: the relay's terms, a quote, a submission and its status, with real requests and answers on Sepolia; errors; the OpenAPI document and a playground"
---

# REST

Each chain is served under `/{chain}`, its chain id in decimal or its network's name:
`https://bruno.mailam.app/sepolia` and `https://bruno.mailam.app/11155111` are the same chain.
Quantities and data are hex strings, as in Ethereum's JSON-RPC, and names are camelCase. The
normative text is the [specification's interface](../SPEC.md#interface).

| Operation | Request | Answer |
|---|---|---|
| The relay's terms | `GET /{chain}/v1/config` | `payee`, `tokens`, `maxGas`, `maxCallBytes` |
| A quote | `GET /{chain}/v1/quote?gas=…&callBytes=…&tokens=…&authority=…` | `validUntilBlock`, `quotes: [{token, amount}]` |
| A submission | `POST /{chain}/v1/submissions` | `id`: 202 if new, 200 if taken already |
| Its status | `GET /{chain}/v1/submissions/{id}` | `status`, `stage`, `attempts`, and once included `transactionHash`, `blockNumber`, `confirmations` |

## Terms

Who to pay, in which tokens (ETH being ERC-7528's `0xEeee…EEeE`), and the most gas and calldata a
call may have:

```console
$ curl https://bruno.mailam.app/sepolia/v1/config
{"payee":"0xfBC0cF0d2C09C57222C871B1cbF9b153bfFA0049","tokens":["0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE","0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14","0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238"],"maxGas":"0xf4240","maxCallBytes":"0xa000"}
```

## Quote

What a call costs in each token: `gas` its own gas, `callBytes` its calldata's length, and
`authority` the account signing an EIP-7702 authorization, if it sends one. `tokens` repeats,
once a token. A quote is advisory: nothing is reserved, and it holds until `validUntilBlock`.

```console
$ curl 'https://bruno.mailam.app/sepolia/v1/quote?gas=0x186a0&callBytes=0x200&tokens=0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE&tokens=0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238'
{"validUntilBlock":"0xb71279","quotes":[{"token":"0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE","amount":"0x39825521640b6"},{"token":"0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238","amount":"0x2a2c50d"}]}
```

## Submission

The call, its gas, the payment the payee must gain during it, the last block it may land in, and
any EIP-7702 authorizations, in the type-4 transaction's JSON shape:

```json
{
  "call": {"to": "0x…the client's account", "data": "0x…its signed batch"},
  "authorizationList": [
    {"chainId": "0xaa36a7", "address": "0x…its delegate", "nonce": "0x0", "yParity": "0x1", "r": "0x…", "s": "0x…"}
  ],
  "gas": "0x186a0",
  "payment": {"token": "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE", "payee": "0xfBC0cF0d2C09C57222C871B1cbF9b153bfFA0049", "amount": "0x39825521640b6"},
  "maxBlockNumber": "0xb712a0"
}
```

The answer is its `id`, with `Location` its status URL: 202 for a new submission, 200 for one
taken already. The same submission sent again is the same submission. What the call must do to
protect the client, since anyone who sees it can run it: the
[client's obligations](../SPEC.md#client-obligations).

## Status

What's become of a submission. Its `stage` is `waiting` (taken, not yet sent), `sending`
(offered to builders, `attempts` times), `confirming` (included), `done` (its block finalized)
or `failed` (rejected, with a `reason`, or expired). Until it's done or failed, `Retry-After`
says when the stage could next change. From a live run on Sepolia:

```json
{"status": "included", "stage": "confirming", "attempts": "0x1", "transactionHash": "0x31687e431873c801d862f2cb0aecca1394263069b8ff27e0b894757736de8a2f", "blockNumber": "0xb70da7", "confirmations": "0x1"}
```

A finished submission is forgotten after a while; its status then answers 404.

## Errors

An error's body is `{"error": …}`, JSON-RPC's error object, its code saying why: the
[error codes](../SPEC.md#interface) and their HTTP statuses. A malformed parameter:

```console
$ curl -i 'https://bruno.mailam.app/sepolia/v1/quote?gas=0x01&callBytes=0x200&tokens=0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE'
HTTP/2 400
{"error":{"code":-32602,"message":"gas: a quantity, as hex","data":"gas"}}
```

A submission that fails its simulation carries its revert data, the call's own or the
runner's [errors](../SPEC.md#errors).

## OpenAPI, and trying it

The relay serves its OpenAPI document at
[`/openapi.json`](https://bruno.mailam.app/openapi.json), x402's operation included. To browse
it and send requests from the browser, open it in Swagger UI:
[petstore.swagger.io/?url=https://bruno.mailam.app/openapi.json](https://petstore.swagger.io/?url=https://bruno.mailam.app/openapi.json).
The relay answers any origin, so "Try it out" reaches it directly. Terms, quotes and statuses
are free to try; on Sepolia, `chain` is `sepolia`. A submission needs a signed call: the
[TypeScript client](typescript.md) makes one.
