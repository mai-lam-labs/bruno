---
description: "Using Bruno over REST: get the relay's terms, quote a call, submit it and follow its status, with real requests and responses from Sepolia; errors; the OpenAPI document and a playground"
---

# REST

Each chain has its own base path, `/{chain}`, where `{chain}` is either the chain ID in decimal
or the network's name. For example, `https://bruno.mailam.app/sepolia` and
`https://bruno.mailam.app/11155111` are the same chain. Quantities and byte strings are hex
strings, as in Ethereum's JSON-RPC, and field names are camelCase. The
[specification's interface](../SPEC.md#interface) is the normative reference.

| Operation | Request | Response |
|---|---|---|
| Get the relay's terms | `GET /{chain}/v1/config` | `payee`, `tokens`, `maxGas`, `maxCallBytes` |
| Get a quote | `GET /{chain}/v1/quote?gas=…&callBytes=…&tokens=…&authority=…` | `validUntilBlock`, `quotes: [{token, amount}]` |
| Submit a call | `POST /{chain}/v1/submissions` | `id`, with status 202 if it's new or 200 if it was already submitted |
| Get a submission's status | `GET /{chain}/v1/submissions/{id}` | `status`, `stage`, `attempts`, and once it's included, `transactionHash`, `blockNumber` and `confirmations` |

## Terms

The terms say who to pay, which tokens are accepted, and the most gas and calldata a call may
use. ETH appears as ERC-7528's address, `0xEeee…EEeE`.

```console
$ curl -s https://bruno.mailam.app/sepolia/v1/config | jq
{
  "payee": "0xfBC0cF0d2C09C57222C871B1cbF9b153bfFA0049",
  "tokens": [
    "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE",
    "0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14",
    "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238"
  ],
  "maxGas": "0xf4240",
  "maxCallBytes": "0xa000"
}
```

## Quote

A quote tells you what a call costs in each token you ask about. The parameters are:

- `gas`: the gas the call itself needs.
- `callBytes`: the length of the call's calldata.
- `tokens`: a token to quote in. Repeat the parameter for each token.
- `authority`: the account signing an EIP-7702 authorization, if the submission will include
  one.

A quote is only a guide. Nothing is reserved for you, and the price holds until
`validUntilBlock`.

```console
$ curl -s https://bruno.mailam.app/sepolia/v1/quote -G \
    -d gas=0x186a0 \
    -d callBytes=0x200 \
    -d tokens=0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE \
    -d tokens=0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238 | jq
{
  "validUntilBlock": "0xb71279",
  "quotes": [
    {
      "token": "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE",
      "amount": "0x39825521640b6"
    },
    {
      "token": "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238",
      "amount": "0x2a2c50d"
    }
  ]
}
```

## Submission

A submission contains the call, its gas, the payment the payee must receive during it, the
last block it may land in, and any EIP-7702 authorizations, in the same JSON shape as a type-4
transaction:

```json
{
  "call": {
    "to": "0x…the client's account",
    "data": "0x…its signed batch"
  },
  "authorizationList": [
    {
      "chainId": "0xaa36a7",
      "address": "0x…its delegate",
      "nonce": "0x0",
      "yParity": "0x1",
      "r": "0x…",
      "s": "0x…"
    }
  ],
  "gas": "0x186a0",
  "payment": {
    "token": "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE",
    "payee": "0xfBC0cF0d2C09C57222C871B1cbF9b153bfFA0049",
    "amount": "0x39825521640b6"
  },
  "maxBlockNumber": "0xb712a0"
}
```

The response contains the submission's `id`, and its `Location` header is the status URL. The
status code is 202 for a new submission and 200 if the same submission was already received;
sending it again doesn't create a second one.

Anyone who sees a submission can run it, so the call itself has to protect the client. What it
must check is set out in the [client's obligations](../SPEC.md#client-obligations).

## Status

The status shows what has happened to a submission. Its `stage` is one of:

- `waiting`: received, not yet sent.
- `sending`: offered to builders, `attempts` times so far.
- `confirming`: included in a block.
- `done`: its block is finalized.
- `failed`: rejected (with a `reason`) or expired.

Until the submission is done or has failed, the `Retry-After` header says when the stage could
next change. Here is a status from a live run on Sepolia:

```json
{
  "status": "included",
  "stage": "confirming",
  "attempts": "0x1",
  "transactionHash": "0x31687e431873c801d862f2cb0aecca1394263069b8ff27e0b894757736de8a2f",
  "blockNumber": "0xb70da7",
  "confirmations": "0x1"
}
```

Finished submissions are forgotten after a while, and their status then returns 404.

## Errors

An error response has the body `{"error": …}`, containing a JSON-RPC error object. Its code
says what went wrong; the [error codes](../SPEC.md#interface) and their HTTP statuses are
listed in the specification. For example, a malformed parameter gets status 400 and this body:

```console
$ curl -s https://bruno.mailam.app/sepolia/v1/quote -G \
    -d gas=0x01 \
    -d callBytes=0x200 \
    -d tokens=0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE | jq
{
  "error": {
    "code": -32602,
    "message": "gas: a quantity, as hex",
    "data": "gas"
  }
}
```

If a submission fails its simulation, the error includes the revert data: either the call's
own, or one of the runner's [errors](../SPEC.md#errors).

## Try it

The relay serves its OpenAPI document, including the x402 operation, at
[`/openapi.json`](https://bruno.mailam.app/openapi.json). To browse it and send requests from
your browser, open it in Swagger UI:
[petstore.swagger.io/?url=https://bruno.mailam.app/openapi.json](https://petstore.swagger.io/?url=https://bruno.mailam.app/openapi.json).
The relay accepts requests from any origin, so "Try it out" works directly. Getting terms,
quotes and statuses is free; on Sepolia, set `chain` to `sepolia`. A submission needs a signed
call, which the [TypeScript client](typescript.md) can make for you.
