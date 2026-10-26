# Post-paid gas relays: specification

Draft. `ERCNNNN` stands for the standard's number once assigned, and `postpaid_` is a
placeholder namespace. Bruno is a reference implementation (DESIGN.md).

A transaction's sender must hold its gas before it's sent. A post-paid **relay** sends a
**client**'s call in a transaction whose gas the relay pays, and the call settles with the
relay's **payee** as it runs: cash on delivery, in ETH or any token. The call can pay from
anything it can reach: what the account holds, what the call itself releases or earns, or
another account's signed payment. Nothing is needed in advance, and a call that doesn't pay
is never included.

## Submission

- **`call`:** one target and its calldata, value 0. A client batches through its own
  account's delegate.
- **`authorizationList`:** EIP-7702 authorizations, optional, sent in the same transaction
  as the call.
- **`gas`:** the gas the call is given.
- **`payment`:** an asset, the relay's payee, and an amount: what the payee must gain during
  the call. The asset is a token's address, or ERC-7528's
  `0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE` for ETH.
- **`maxBlockNumber`:** the last block it may be included in.

## Semantics

**The relay guarantees:** nothing is included unless, during the call, the payee's balance of
the asset rises by at least the amount. A submission that isn't included costs the client
nothing.

**The relay doesn't guarantee:**
- that the call, or the authorizations, run at all, or that they run together;
- which block, before `maxBlockNumber`, it runs in;
- the sender of the transaction, or the call's `msg.sender`;
- how many times it attempts it.

**Price.** A quote is advisory: nothing is reserved. A relay includes a submission only in a
block whose price its amount covers; a submission that doesn't cover the current price waits,
and expires at its deadline.

**Lifecycle.** A submission taken is `pending` until it's `included`, `expired` (past
`maxBlockNumber`), or `rejected` (with a reason: outbid from a full queue, or no longer
simulating). The same submission sent again is the same submission.

## Client obligations

The relay runs whatever it's given; the call must protect the client by the client's own
signature:
- **authorize everything it does,** never relying on `msg.sender`;
- **check, before paying, that any delegation it relies on is in place:** a delegate whose
  signatures bind its implementation (Calibur's EIP-712 salt does), or a check of the
  account's code hash against `keccak256(0xef0100 ‖ delegate)`. A stale authorization is
  skipped (EIP-7702), and the call then runs against the account's current code;
- **order and batch** whatever it does;
- **carry its own replay protection and validity window:** a submission is public to the
  relay and to every builder it reaches, and anyone who sees it can run it.

## Errors

Why a submission fails, as `revertData`: the call's own revert data, or one of:

| Error | Selector | |
|---|---|---|
| `ERCNNNN_Underpaid()` | `0xdf9459c7` | The payee's balance didn't rise by the amount. |
| `ERCNNNN_BalanceUnreadable()` | `0x693e471f` | The payee's balance of the token couldn't be read. |
| `ERCNNNN_GasExhausted()` | `0x472c00d6` | The call couldn't be given its gas. |

## Interface

One base path a chain, `/{chain}`: its chain id in decimal, or its network's name
(`ethereum`, `sepolia`, `hoodi`), either reaching the same chain. Each operation is served
over REST and over JSON-RPC (`POST /{chain}`), with the same request and response objects:
Ethereum JSON-RPC's encoding (hex quantities and data), camelCase.

`GET /` lists the chains served, each with its JSON-RPC and REST URLs and its payee, and the
documents: OpenRPC at `/openrpc.json`, OpenAPI at `/openapi.json`. A request accepting
`text/html` gets the same as a page, with a QR code of each payee's address.
Any origin may call the interface (CORS).

| JSON-RPC | REST | Request | Response |
|---|---|---|---|
| `postpaid_config` | `GET /v1/config` | | `payee`, `tokens`, `maxGas`, `maxCallBytes` |
| `postpaid_quote` | `GET /v1/quote` | `gas`, `callBytes`, `tokens`, `authority`? | `validUntilBlock`, `quotes: [{token, amount}]` |
| `postpaid_sendTransaction` | `POST /v1/submissions` | `call: {to, data}`, `authorizationList`?, `gas`, `payment: {token, payee, amount}`, `maxBlockNumber` | `id` |
| `postpaid_getStatus` | `GET /v1/submissions/{id}` | `id` | `status`, `stage`, `attempts`, `transactionHash`?, `blockNumber`?, `confirmations`?, `reason`?, `revertData`? |

- **`postpaid_quote`:** `gas` is the call's own gas, `callBytes` its data's length,
  `authority` the account signing an authorization, if any. REST repeats `tokens` once a
  token.
- **`token`, `tokens`:** assets, ETH being ERC-7528's address.
- **`payment.payee`** must be the relay's.
- **`id`:** `keccak256(abi.encode(to, data, authorizations, gas, token, payee, amount,
  maxBlockNumber))`, the authorizations as `(chainId, address, nonce, yParity, r, s)[]`. REST
  answers 202 for a new submission, 200 for one already taken.
- **`authorizationList`** is the type-4 transaction's JSON shape.
- **`stage`:** `waiting` (taken, not yet sent), `sending` (offered to builders, `attempts`
  times), `confirming` (included; `confirmations` counts its block and each since), `done`
  (its block finalized), `failed` (`status` rejected or expired). REST answers `Retry-After`
  until it's done or failed: when the stage could next change.

The JSON-RPC methods are described by an OpenRPC 1.3.2 document, answered by
`rpc.discover` with the chain's endpoint as its server.

A REST error's body is `{"error": <the JSON-RPC error object>}`:

| Code | Meaning | `data` | HTTP |
|---|---|---|---|
| -32602 | Invalid params | the field | 400 |
| -32010 | Underpaid at the next block's price | `{token, required}` | 422 |
| -32011 | Simulation failed | `{revertData}` | 422 |
| -32012 | Unsupported token or payee | | 422 |
| -32013 | No capacity | `{token, required}`: the amount that would be taken | 503 |
| -32014 | Unknown submission | | 404 |
| -32015 | Unavailable: the chain can't be served now | | 503 |
| -32601 | Not found: no such path, or the chain isn't served | | 404 |

### x402

A relay may take payment by x402 (v2, over HTTP, the `exact` scheme on EVM with EIP-3009), at
`POST /{chain}/v1/x402`, REST only. The request's body is the call: `call: {to, data}`, `gas`,
`authorizationList`?.

1. **Without `PAYMENT-SIGNATURE`:** 402, `PAYMENT-REQUIRED` and the same in the body. Each
   requirement is a token the relay takes this way: `amount` the call's quote, `payTo` the
   payee, `maxTimeoutSeconds` the longest a paid request is held, `extra` the token's EIP-712 domain. A
   body that isn't a call is priced as a reference call, the description says which.
2. **With `PAYMENT-SIGNATURE`:** a `transferWithAuthorization` of `value` to the payee, valid
   from the next block. It's the submission's payment: the payee must gain at least `value`
   during the call, and the authorization is spent in the call's own transaction, before the
   call, or not at all. The deadline is the last block expected before `validBefore`, at
   most `maxTimeoutSeconds` ahead. The request is held until the transaction is included, or
   the submission is rejected or expires.

| Answer | Status | |
|---|---|---|
| Settled | 200 | `PAYMENT-RESPONSE`: `success`, `transaction` the included one, `payer`, its `status` extension's `info.url`. Body: `id`, `statusUrl`; `Location` the same URL |
| Rejected or expired, held or sent again | 402 | `PAYMENT-REQUIRED`; `PAYMENT-RESPONSE` `invalid_transaction_state`; the authorization unused |
| `value` other than `accepted.amount` (an `accepted.amount` above the quote is a tip, and taken) | 402 | `PAYMENT-REQUIRED`; `PAYMENT-RESPONSE` `invalid_exact_evm_payload_authorization_value_mismatch` |
| Underpaid, or outbid from a full queue | 402 | `PAYMENT-REQUIRED` at what's needed; `PAYMENT-RESPONSE` `invalid_exact_evm_payload_authorization_value_mismatch` |
| Simulation failed | 402 | `PAYMENT-RESPONSE` `invalid_exact_evm_payload_signature` (the signature isn't the payer's: a key's recovered, for a payer without code; a contract's as its ERC-1271 `isValidSignature` answers), `insufficient_funds`, or else `invalid_transaction_state`; the body the error object, with `revertData` |
| `assetTransferMethod` other than `eip3009`, or `paymentFlow` other than `authorization` | 402 | `PAYMENT-REQUIRED`; `PAYMENT-RESPONSE` `invalid_payment_requirements` |
| Window, payee, scheme, network, version or token wrong | 402 | `PAYMENT-REQUIRED`; `PAYMENT-RESPONSE` with x402's code for it |
| A `PAYMENT-SIGNATURE` that isn't a payment payload | 400 | the error object |

A payer is a key or a contract: its `signature` a key's 65 bytes, made through
`transferWithAuthorization`'s `(v, r, s)` overload (a `v` of 0 or 1 read as 27 or 28), or a
contract's, any other length up to 4,096 bytes, made through its `bytes` overload and checked by
the payer's ERC-1271 `isValidSignature`. A contract payer's transfer is priced as it runs: a
payment that covers a key's but not its own is answered 402 at what it needs.

A taken payment's `id` is the submission's, its authorization `(from, to, value, validAfter,
validBefore, nonce, signature)` encoded in place of `maxBlockNumber`. Its status is
`postpaid_getStatus`'s, at `Location` while the request is held. The same payment sent again,
or another payment for the same call by the same payer while one is pending, is held for that
one and answered as it is; the other authorization is never used. Discovery:
`/.well-known/x402` lists each chain's endpoint.

## Security considerations

**A copy pays the same payee.** Anyone who runs a submission, relay or not, makes the same
call and pays the same payee from the client's signed call, at their own gas. The client
loses nothing it didn't sign; its replay protection decides how many times the call
succeeds.

**`msg.sender` is no one in particular.** It may differ between attempts and between relays.
Anything granted to it is granted to an address the client doesn't control.

**Paying before checking.** A call that pays and then does nothing still satisfies the relay.
The client's call must make its payment conditional on what it pays for: its delegation in
place, its batch succeeding.

**Visibility.** A submission is visible to the relay and the builders it reaches before it's
included, as a transaction in a public mempool is.

**An x402 authorization doesn't bind the call.** Anyone who sees it can attach it to another
call, or spend it directly: the money still goes to the payee, but the client's call may not
run. It's visible to the relay and the builders it reaches.

**The relay can decline.** It may never include a submission; the client can send the same
call to several relays, and its replay protection makes at most one succeed.

## Open questions

- The standard's number, and so its error names; the JSON-RPC namespace.
- Several authorizations a submission (type 4 allows them), or at most one.
- JSON-RPC batch requests.
- A prepaid mode, the payment taken before the call.
- x402's other ways to pay: Permit2, ERC-7710; x402 over MCP.
