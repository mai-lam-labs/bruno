---
description: "The draft standard for post-paid gas relays, as a client sees it: submissions, semantics, the client's obligations, errors, the interface, security"
---

# Post-paid gas relays: specification

Draft. `ERCNNNN` stands for the standard's number once assigned, and `postpaid_` is a
placeholder namespace. Bruno is a reference implementation ([design](development/design/index.md)).

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
- **`payment.transfer`:** optional, a token's EIP-3009 `transferWithAuthorization` of the
  amount from `from` to the payee, signed (see Signatures): `from`, `validAfter`,
  `validBefore`, `nonce`, `signature`. The relay makes it in the call's transaction, before the
  call.
- **`deployment`:** optional, an account to deploy before the call: `account`, `factory`,
  `factoryData` (ERC-4337's names). If `account` has no code, `factory` is called with
  `factoryData`.
- **`maxBlockNumber`:** the last block it may be included in.

## Semantics

**The relay guarantees:** nothing is included unless, during the call, the payee's balance of
the asset rises by at least the amount, and the call is given at least `gas`. A deployment and
a payment's transfer run before the call, in its transaction: if either fails, nothing is
included. A submission that isn't included costs the client nothing.

**The relay doesn't guarantee:**
- that the call, or the authorizations, run at all, or that they run together;
- which block, before `maxBlockNumber`, it runs in;
- the sender of the transaction, or the call's `msg.sender`;
- how many times it attempts it.

**Price.** A quote is advisory: nothing is reserved. A relay includes a submission only in a
block whose price its amount covers; a submission that doesn't cover the current price waits,
and expires at its deadline. A submission that runs code its client chooses before the call (a
deployment, or a contract's signature check) is priced by its transaction as it will run, once
signed: one short of that is underpaid, with the amount required.

**Signatures.** A payment transfer's `signature` (over x402 too) is a key's 65 bytes, made
through `transferWithAuthorization`'s `(v, r, s)` overload (a `v` of 0 or 1 read as 27 or
28); a contract's, any other length up to 4,096 bytes, made through its `bytes` overload and
checked by the payer's ERC-1271 `isValidSignature`; or ERC-6492-wrapped, ending in its magic
bytes. A wrapped signature deploys `from`: its `factory` is called with its `factoryCalldata`
before the transfer if `from` has no code, or if the transfer fails without it (ERC-6492's
prepare), and its inner signature is the one used. A submission deploys at most one account: a
wrapped signature's and `deployment` naming different ones are invalid params.

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

Why a submission fails, as `revertData`: the call's own revert data (or a deployment's, or a
payment transfer's), or one of:

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
| `postpaid_quote` | `GET /v1/quote` | `gas`, `callBytes`, `tokens`, `authority`?, `payer`?, `deployment`?, `verifyGas`? | `validUntilBlock`, `quotes: [{token, amount}]` |
| `postpaid_sendTransaction` | `POST /v1/submissions` | `call: {to, data}`, `authorizationList`?, `gas`, `payment: {token, payee, amount, transfer?}`, `deployment`?, `maxBlockNumber` | `id` |
| `postpaid_getStatus` | `GET /v1/submissions/{id}` | `id` | `status`, `stage`, `attempts`, `transactionHash`?, `blockNumber`?, `confirmations`?, `reason`?, `revertData`? |
| `eth_estimateUserOperationGas` | `POST /v1/user-operations/gas` | `userOperation`, `entryPoint` | `preVerificationGas`, `verificationGasLimit`, `callGasLimit` |
| `postpaid_quoteUserOperation` | `POST /v1/user-operations/quote` | `userOperation`, `entryPoint` | `validUntilBlock`, `quotes: [{token, amount}]` |
| `eth_sendUserOperation` | `POST /v1/user-operations` | `userOperation`, `entryPoint` | its hash (REST: `userOpHash`) |
| `eth_getUserOperationReceipt` | `GET /v1/user-operations/{hash}` | `userOpHash` | ERC-7769's receipt, or null |
| `pimlico_getUserOperationGasPrice`, `zd_getUserOperationGasPrice` | | | `{slow, standard, fast}`, each `{maxFeePerGas, maxPriorityFeePerGas}` |
| `eth_supportedEntryPoints` | | | `[EntryPoint v0.8, EntryPoint v0.7]` |
| `eth_chainId` | | | the chain's id |

- **Only these:** a relay answers the methods above and `rpc.discover`, and refuses any other
  as method not found, reading nothing: it's no gateway to its nodes.
- **User operations pay one of two ways,** sent as a call that pays for itself, the EntryPoint's
  `handleOps` of the op, the relay's payee its beneficiary. With its `maxFeePerGas` above zero,
  by its fees, as at any bundler: the EntryPoint pays the payee its gas at its price, in ETH.
  With it zero, by its own calls: they pay the payee in a token the relay takes, and the account
  needs no ETH.
- **Another's paymaster:** an op may name one (`paymaster`, `paymasterData`, and its limits),
  and then pays by its fees, from the paymaster's deposit: with a paymaster and a zero
  `maxFeePerGas` it's invalid params, `maxFeePerGas`; the paymaster's data without it, invalid
  params, `paymaster`. The relay has no paymaster of its own.
- **EntryPoints v0.8 and v0.7,** each op for the one its `entryPoint` names. An EIP-7702
  account's op (v0.8) has `factory` `0x7702`, and its first brings the account's authorization
  as `eip7702Auth` (`chainId`, `address`, `nonce`, `yParity`, `r`, `s`, as hex), which the
  relay's transaction carries. Simulated, the account is given that delegation; an estimate
  doesn't check the authorization's signature. Sent, it must be the account's, for the chain or
  any, else invalid params, `eip7702Auth`; so must a `0x7702` op bringing none come from an
  account delegated already.
- **`eth_estimateUserOperationGas`:** ERC-7769's. Its `signature` is the
  account's stub; its gas limits aren't read, but its paymaster's, if given, are kept and
  answered as given. The answer's limits are what the op uses and a tenth more. A paymaster's
  limits not given are measured from the op simulated with and without it, as upper bounds a
  tenth more: for its validation, the op's whole validation; for its postOp, all after
  validation. `preVerificationGas` is zero for an op paying by its calls. For one paying by its
  fees, it's the rest of the gas that, at the price its fees give at the least the base fee can
  fall to by the quote's last block, comes to a quote of its transaction: whatever fees it
  offers, what it's charged is at least the relay's price (gas it's charged, not gas its
  transaction uses), its transaction planned with a node's estimate of the op simulated as
  sized (simulation failed if that reverts, a paymaster refusing its measured limits say).
  Fees paying nothing a gas then are invalid params, `maxFeePerGas`. Another EntryPoint, `0x7702` for v0.7, or call
  or factory data longer than
  `maxCallBytes` are invalid params, refused reading nothing; limits over `maxGas` are invalid
  params, `gas`; an op whose validation reverts, or that can't pay its prefund, is simulation
  failed. REST's body is `{userOperation, entryPoint}`.
- **`postpaid_quoteUserOperation`:** what an op paying by its calls is to pay, in each token the
  relay takes and hasn't suspended, before it's signed: its payment a placeholder (any amount
  it can pay), which only its amount then replaces; its `signature` the account's stub, as for
  an estimate. Its transaction is planned as its send plans it, with the limits its estimate
  gives and a node's estimate of the op simulated as sized, its calldata priced as if its
  payment's amount word held no zero bytes; then priced as `postpaid_quote` prices a call, until
  `validUntilBlock`. Signed paying the quoted amount, the op is taken by `eth_sendUserOperation`
  unless the price has risen since. Refused as an estimate is, and an op paying by its fees
  (which its estimate prices) is invalid params, `maxFeePerGas`, refused reading nothing.
- **`pimlico_getUserOperationGasPrice`, `zd_getUserOperationGasPrice`:** the price an op paying
  by its fees is asked, as both its fees, at every speed: a gas as a quote charges it, its base
  fee risen and tip doubled by the quote's last block, and the relay's fee a gas. An EntryPoint
  takes a max fee equal to the priority fee as the price, whatever the base fee.
  Unsupported if the relay doesn't take ETH.
- **`eth_sendUserOperation`:** the op, signed, taken as a submission, its deadline
  `max_deadline_blocks` ahead. Its payment is found by running it through the EntryPoint's
  `handleOps` and reading the payee's balance in each token the relay takes before and after:
  by its fees, the ETH its gas costs at its price in the next block (the runner checks what it
  pays at any base fee, run at a base fee of 0 and at the node's); by its calls, what they pay.
  Refused as an estimate is, and without its paymaster's limits if it has one (invalid params,
  naming the limit); invalid params if the account refuses its signature (`signature`), or its
  paymaster its paymaster data's (`paymasterData`), or if, paying by its calls, it pays nothing in a token the relay takes
  (`callData`); then checked as any submission, so underpaid says what's required. The answer is the op's hash as its EntryPoint
  computes it; the same op sent again is taken once.
- **`eth_getUserOperationReceipt`:** null until the op's transaction is included (and for an op
  the relay didn't take, or has forgotten); then ERC-7769's receipt, read from that transaction:
  its `UserOperationEvent`'s fields, the logs its execution emitted, the revert data if it
  failed, and the transaction's receipt.
- **`maxGas`:** the most gas a submission's transaction may use, everything in it: the relay's
  own, a deployment, a transfer and its signature check, and the call. A quote or a submission
  whose transaction would need more is invalid params, `gas`.
- **`postpaid_quote`:** `gas` is the call's own gas, `callBytes` its data's length,
  `authority` the account signing an authorization, if any. REST repeats `tokens` once a
  token. `payer` is the account a signed transfer pays from (its `from`): with it, the
  payment is that transfer, and only then is `verifyGas` taken; without it, the call pays for
  itself. With `deployment`, the factory call is priced: as made if its account has no code,
  and, for a transfer, as the call that prepares an account with code (ERC-6492). A contract
  payer's (one with code, or the account its `deployment` deploys) signature check is priced
  at `verifyGas` if given, else at the relay's figure for the deployment's factory, else at
  its largest. REST takes `deployment` as `account`, `factory` and `factoryData`.
- **`token`, `tokens`:** assets, ETH being ERC-7528's address.
- **`payment.payee`** must be the relay's.
- **`id`:** `keccak256(abi.encode(to, data, authorizations, gas, token, payee, amount,
  maxBlockNumber))`, the authorizations as `(chainId, address, nonce, yParity, r, s)[]`; a
  payment transfer in place of `maxBlockNumber`, as `(from, to, value, validAfter,
  validBefore, nonce, signature)`; a `deployment` appended as `(account, factory,
  factoryData)`. REST answers 202 for a new submission, 200 for one already taken.
- **`authorizationList`** is the type-4 transaction's JSON shape. An authorization's nonce is
  at most 2^53 − 1, so every client reads it exactly as a number.
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
| -32015 | Unavailable: the chain can't be served now, or a node failed the request; try again | | 503 |
| -32601 | Not found: no such path, or the chain isn't served | | 404 |

### x402

A relay may take payment by x402 (v2, over HTTP, the `exact` scheme on EVM with EIP-3009), at
`POST /{chain}/v1/x402`, REST only. The request's body is the call: `call: {to, data}`, `gas`,
`authorizationList`?, and, as for a quote, `payer`?, `deployment`?, `verifyGas`?. Its payment
is always a transfer: `payer` names the account it will be from before it's signed, so a
contract's check is priced; without it, the account `deployment` deploys is taken to be it.

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

A payer is a key or a contract, its `signature` as Signatures says. A contract payer's
transaction is priced as it runs: a payment that covers the 402's but not its own is answered
402 at what it needs.

A taken payment's `id` is the submission's, its authorization as a payment transfer. Its status is
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

## Copyright

Copyright and related rights waived via [CC0](https://creativecommons.org/publicdomain/zero/1.0/).
