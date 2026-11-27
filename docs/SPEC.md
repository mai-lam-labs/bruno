---
description: "The draft standard for post-paid gas relays, as a client sees it: submissions, sponsorships, semantics, the client's obligations, errors, the interface, security"
---

# Post-paid gas relays: specification

Draft. `ERCNNNN` stands for the standard's number once assigned, and `postpaid_` is a
placeholder namespace. Bruno is a reference implementation ([design](https://github.com/mai-lam-labs/bruno/blob/main/docs/development/design/index.md)).

A transaction's sender must hold its gas before it's sent. A post-paid **relay** sends a
**client**'s call in a transaction whose gas the relay pays, and the call settles with the
relay's **payee** as it runs: cash on delivery, in ETH or any token. The call can pay from
anything it can reach: what the account holds, what the call itself releases or earns, or
another account's signed payment. Nothing is needed in advance, and a call that doesn't pay
is never included.

A relay also sends an account's own signed transactions, a **sponsorship**: it sends the
account the ETH they need and is paid in the same bundle.

## Submission

- **`calls`:** a list of calls, each `{to, data, value?, gas?}`: its target, its calldata, and
  the gas it's given. The relay makes them in order, all or none, and may limit how many a
  submission carries. A call's `value`, the ETH it sends, is zero if given. None is made from
  the client's account: a client whose calls must be its account's own batches them through
  its account's delegate, as one call. A relay may bound a call's gas itself, where the call
  is one whose cost it knows (a token's EIP-3009 transfer a key signed, from an account
  without code): such a call is given the relay's bound, whatever `gas` it states, and needs
  to state none. Any other call without `gas` is invalid params, the call's `gas`
  (`call-no-gas`).
- **`authorizationList`:** EIP-7702 authorizations, optional, sent in the calls'
  transaction, or the first call's where each has its own.
- **`payment`:** an asset, the relay's payee, and an amount: what the payee must gain during
  the calls. The asset is a token's address, or ERC-7528's
  `0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE` for ETH.
- **`payment.transfer`:** optional, a token's EIP-3009 `transferWithAuthorization` of the
  amount from `from` to the payee, signed (see Signatures): `from`, `validAfter`,
  `validBefore`, `nonce`, `signature`. The relay makes it with the calls (see Semantics).
- **`deployment`:** optional, an account to deploy before the calls: `account`, `factory`,
  `factoryData` (ERC-4337's names). If `account` has no code, `factory` is called with
  `factoryData`.
- **`maxBlockNumber`:** the last block it may be included in.

## Sponsorship

An account's own transactions, signed, sent between two of the relay's as one bundle. The
relay's first transaction sends the account the ETH its transactions need. Its last calls the
payee's **forwarder**, which sends the payee what the account paid it in between.

- **`account`:** the account sponsored. Each transaction's sender, each authorization's signer
  and the call's target is this account.
- **`transactions`:** its transactions, each signed, as its raw bytes, in the order they're
  sent: EIP-1559 or EIP-7702 transactions (types 2 and 4) for this chain, at consecutive
  nonces.
- **`authorizationList`:** EIP-7702 authorizations the account signed, optional, sent in the
  relay's first transaction.
- **`call`:** optional, a call into the account's code, `{to, data}`, made by the relay's
  first transaction after its authorizations.
- **`gas`:** the gas the account's code is given in the relay's first transaction. Required
  with a `call` or an `authorizationList`, and for an account that has code already, which
  runs on receiving ETH.
- **`payment`:** an asset, the payee's forwarder, and an amount. The account pays the
  forwarder at least the amount before the relay's last transaction: by one of its
  transactions, or by its code in the relay's first. The relay's last transaction has the
  forwarder send it on, and fails unless the payee gains the amount of ETH, or the amount of a
  token less one base unit.
- **`maxBlockNumber`:** the last block it may be included in.

A sponsorship has a transaction, a call, or both.

**The forwarder** is a contract with the payee's address in its code, one for each payee:
`postpaid_config` gives its address. It holds what it's sent, ETH or any token. Called by
anyone, it sends what it holds to the payee, and to no one else, and reverts unless the payee
gained the amount asked for. It keeps one base unit of a token it has held, so the payee may
gain one base unit less than the amount. A relay takes no sponsorship on a chain where its
forwarder isn't deployed (`no-forwarder`, unavailable): a call to an address without code
succeeds and checks nothing.

## Semantics

**The relay guarantees, of a submission:** nothing is included unless the payee's balance of
the asset rises by at least the amount, and each call is given at least its `gas`, or the
relay's bound where the relay bounds it. The calls
are made in the order given: in one transaction, or in a transaction each, in a bundle that
lands whole. A deployment runs before the calls, in their transaction. A payment's transfer a
key signed is made after the calls, so a call may bring the payer what it pays with: in the
calls' transaction, or in one of its own after them, in the same bundle. One a contract
signed is made before the calls, in their transaction. If any of them fails, nothing is
included. A submission that isn't included costs the client nothing.

**The relay doesn't guarantee:**
- that the calls, or the authorizations, run at all, or that they run in one transaction;
- which block, before `maxBlockNumber`, it runs in;
- the sender of a transaction, or a call's `msg.sender`;
- whether the calls are made in one transaction or in a transaction each;
- whether a payment's transfer is made in the calls' transaction or in one of its own;
- how many times it attempts it.

**A sponsorship** is sent as one bundle, for a builder to include whole, in order, or not at
all: the relay's first transaction, the account's, the relay's last. The relay's first sends
the account each of its transactions' gas limit at its max fee, and its value, whatever the
account holds. ETH a transaction sends the forwarder isn't sent: an account paying in ETH
holds the amount already, or is brought it by an earlier transaction. What the account doesn't
spend stays with it. A sponsorship is quoted and taken only if the relay's two transactions'
gas and all the account is sent, together, cost no more than `maxGas` does at the most a
block may ask by the deadline. The relay's last transaction fails unless the payee's balance rises in
it by at least the amount of ETH, or by at least the amount of a token less one base unit,
which the forwarder keeps. If any transaction in the bundle fails, nothing is included, and
the sponsorship costs the account nothing. Each of its transactions'
max fee is at least the most a block's base fee can be by `maxBlockNumber`. One sponsorship of
an account is pending at a time. A builder keeps a bundle whole, not the chain: if only some
of its transactions land, the relay sends nothing more of it, and it's `rejected`, its reason
`bundle-broken` (see Security considerations).

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
`maxBlockNumber`), or `rejected`, its `reason` one of `outbid` (from a full queue),
`simulation-failed` (no longer simulating), `reverted` (on-chain) and `bundle-broken` (some of
its transactions landed without the rest). The same submission sent again is the same
submission. A sponsorship's lifecycle is a submission's.

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

Why a submission fails, as `revertData`: a call's own revert data (or a deployment's, or a
payment transfer's), or one of:

| Error | Selector | |
|---|---|---|
| `ERCNNNN_Underpaid()` | `0xdf9459c7` | The payee's balance didn't rise by the amount. |
| `ERCNNNN_BalanceUnreadable()` | `0x693e471f` | The payee's balance of the token couldn't be read. |
| `ERCNNNN_GasExhausted()` | `0x472c00d6` | A call couldn't be given its gas. |

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
| `postpaid_config` | `GET /v1/config` | | `payee`, `forwarder`, `tokens`, `maxGas`, `maxCallDataLength` |
| `postpaid_quote` | `POST /v1/quote` | `tokens`, `calls: [{to?, data?, value?, callDataLength?, gas?}]`, `authorizationList`?, `authority`?, `payer`?, `deployment`?, `verifyGas`? | `validUntilBlock`, `quotes: [{token, amount, breakdown, typedData?}]`, `calls: [{gas}]` |
| `postpaid_sendTransaction` | `POST /v1/submissions` | `calls: [{to, data, value?, gas}]`, `authorizationList`?, `payment: {token, payee, amount, transfer?}`, `deployment`?, `maxBlockNumber` | `id` |
| `postpaid_quoteSponsorship` | `POST /v1/sponsorships/quote` | `tokens`, `account`, `transactions`? (each signed, or `{to?, data?, value?, gas?}`), `authorizationList`?, `call: {to, data}`?, `gas`?, `maxBlockNumber`? | `validUntilBlock`, `maxBlockNumber`, `forwarder`, `gas`?, `transactions`, `quotes: [{token, amount, payment?}]` |
| `postpaid_sponsor` | `POST /v1/sponsorships` | `account`, `transactions`?, `authorizationList`?, `call: {to, data}`?, `gas`?, `payment: {token, payee, amount}`, `maxBlockNumber` | `id` |
| `postpaid_getStatus` | `GET /v1/submissions/{id}` | `id` | `status`, `stage`, `attempts`, `transactionHashes`?, `blockNumber`?, `confirmations`?, `reason`?, `revertData`? |
| `eth_estimateUserOperationGas` | `POST /v1/user-operations/gas` | `userOperation`, `entryPoint` | `preVerificationGas`, `verificationGasLimit`, `callGasLimit`, `paymasterVerificationGasLimit`?, `paymasterPostOpGasLimit`? |
| `postpaid_quoteUserOperation` | `POST /v1/user-operations/quote` | `userOperation`, `entryPoint` | `validUntilBlock`, `quotes: [{token, amount}]` |
| `eth_sendUserOperation` | `POST /v1/user-operations` | `userOperation`, `entryPoint` | its hash (REST: `userOpHash`) |
| `eth_getUserOperationReceipt` | `GET /v1/user-operations/{hash}` | `userOpHash` | ERC-7769's receipt, or null |
| `pimlico_getUserOperationGasPrice`, `zd_getUserOperationGasPrice` | | | `{slow, standard, fast}`, each `{maxFeePerGas, maxPriorityFeePerGas}` |
| `eth_supportedEntryPoints` | | | `[EntryPoint v0.8, EntryPoint v0.7]` |
| `eth_chainId` | | | the chain's id |

- **Only these:** a relay answers the methods above and `rpc.discover`, and refuses any other
  as method not found, reading nothing: it's no gateway to its nodes.
- **User operations pay one of two ways,** in the EntryPoint's `handleOps` of the op, the
  relay's payee its beneficiary. With its `maxFeePerGas` above zero, by its fees, as at any
  bundler: the relay sends `handleOps` itself, and the EntryPoint pays the payee its gas at its
  price, in ETH, whether or not the op's calls succeed. With it zero, by its own calls, sent as
  a call that pays for itself: they pay the payee in a token the relay takes, and the account
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
  `maxCallDataLength` are invalid params, refused reading nothing; limits over `maxGas` are invalid
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
  by its fees, the ETH its gas costs at its price in the next block (run at a base fee of 0 and
  at the node's, the least it's charged, checked again for each block it's sent for); by its
  calls, what they pay.
  Refused as an estimate is, and without its paymaster's limits if it has one (invalid params,
  naming the limit); invalid params if the account refuses its signature (`signature`), or its
  paymaster its paymaster data's (`paymasterData`), or if, paying by its calls, it pays nothing in a token the relay takes
  (`callData`); then checked as any submission, so underpaid says what's required. The answer is the op's hash as its EntryPoint
  computes it; the same op sent again is taken once.
- **`eth_getUserOperationReceipt`:** null until the op's transaction is included (and for an op
  the relay didn't take, or has forgotten); then ERC-7769's receipt, read from that transaction:
  its `UserOperationEvent`'s fields, the logs its execution emitted, the revert data if it
  failed, and the transaction's receipt.
- **`maxGas`:** the most gas a submission's transactions may use, everything in them: the
  relay's own, a deployment, a transfer and its signature check, and the calls. A quote or a
  submission that would need more is invalid params, `calls`. It bounds each call's gas too,
  and the calls' together, as `maxCallDataLength` bounds each call's data and all of it: one
  call over a limit is invalid params, that call's `gas` or the call, and calls over it only
  together, `calls`.
- **`postpaid_quote`:** a submission's fields less its payment and deadline. Each of `calls`
  is the call whole (`to` and `data`), or only its data's length (`callDataLength`) with
  `gas`; both, and they must agree (invalid params, the call's `callDataLength`); neither,
  invalid params, the call. A field of a call is named by its place, as `calls[0].gas` is.
  `gas` is the call's own gas. One call may leave it out, and the relay estimates it: the
  call made as the submission's transaction will make it, after its authorizations and
  deployment and the calls before it, the others each at their gas, the gas it needs and a
  tenth more. Where the calls are each sent in a transaction of their own, it's estimated
  alone, as the chain stands. A second call without `gas` is invalid params, that call's `gas`
  (`call-no-gas`). A call the relay bounds is given its bound and isn't run, so it's quoted
  even where it would revert as the chain stands, and counts as stating its gas. The answer's `calls` gives each
  call's `gas`, in order, for the submission to give it. A call that reverts when estimated
  is simulation failed.
  `authorizationList` the authorizations as signed, or `authority` the account that will sign
  one, not both (invalid params, `authority`); with `authority` alone, a call is estimated
  with the account's code as it is, so only from an account delegated already (otherwise
  invalid params, the call's `gas`). `payer` is the
  account a signed transfer pays from (its `from`): with it, the payment is that transfer, and
  only then is `verifyGas` taken; without it, the calls pay for themselves. With `deployment`, the
  factory call is priced: as made if its account has no code, and, for a transfer, as the call
  that prepares an account with code (ERC-6492). A contract payer's (one with code, or the
  account its `deployment` deploys) signature check is priced at `verifyGas` if given, else at
  the relay's figure for the deployment's factory, else at its largest. Each token's quote is
  broken down, for people to read: `breakdown: {setup, call, payment, relay}`, each `{gas,
  amount}`, the account's setup (its deployment and authorizations), the call, the payment's
  transfer and check, and the relay's own, the rest, so they sum to `amount`. With `payer`, a
  token taking EIP-3009 comes with `typedData`, the transfer to sign as EIP-712's JSON (as
  `eth_signTypedData_v4` takes it, its numbers decimal strings): from the payer, to the payee,
  of `amount`, valid for the quote's blocks and the longest deadline, its nonce fresh.
- **`postpaid_quoteSponsorship`:** a sponsorship's fields less its payment, each of its
  `transactions` signed already, as a sponsorship's, or still to be: an object, `{to?, data?,
  value?, gas?}`, with no `to` for a contract creation. The answer fills each of those in to
  sign (`transactions`, null in a signed one's place): `type` (EIP-1559's), `chainId`, `nonce`
  (the account's next, after its authorizations), `to`, `data`, `value`, `gas` (the gas given,
  or what it uses simulated and a tenth more: a measurement of one run, which can be under
  the limit a transaction needs, so a client states the gas of any but a simple call),
  `maxFeePerGas` (the most a block's base fee can
  be by the deadline) and `maxPriorityFeePerGas`. Each token's quote has `amount`, what the
  sponsorship is charged, and, if it has transactions, `payment`: the transaction paying the
  forwarder that amount, filled in to sign after them (a token's `transfer`, or ETH sent
  it). With no transactions, the account's code pays, and no payment is offered. `gas` is what
  the account's code is given in the relay's first transaction, measured the same way unless
  given,
  answered whenever that transaction runs the account's code: with a `call` or an
  `authorizationList`, or code there already. `maxBlockNumber` is the deadline quoted for: the
  one given, or the furthest the relay takes; a nearer one costs less, the account being sent
  ETH for the fees a block may ask by then. The relay's first transaction, the account's and
  the payment are simulated together, once for each token, and a failure is simulation failed. Signed as answered, and submitted with the answer's
  `forwarder` as its payment's payee, its `gas` and its `maxBlockNumber`, it's a sponsorship
  the relay takes unless the price has risen since.
- **`postpaid_sponsor`:** a sponsorship, taken as a submission is: planned and priced as it
  was submitted, never compared with a quote, so underpaid says what's required. Invalid
  params, naming the field: a transaction that isn't one of the two types, strictly encoded
  (`transaction-undecodable`), signed for another chain (`transaction-chain`), or at a max fee
  under what a block may ask by the deadline (`transaction-fee`, with `needs`); anything that
  isn't the account's (`sponsored-not-accounts`); a nonce that doesn't follow the transaction
  before it (`sponsored-nonce`); neither a transaction nor a call (`sponsored-nothing`); a
  `call`, an `authorizationList` or an account with code, without `gas` (`sponsored-no-gas`);
  transactions the account can't pay for with what it holds and is sent, an ETH payment it
  doesn't hold among them (`sponsored-unfunded`); transactions whose gas, max fees and value
  take what the account is sent, with the relay's own gas, over what a sponsorship may cost
  (`sponsored-over`, with `most` and `needs`, in wei). A `payment.payee` that
  isn't the relay's forwarder is unsupported. While another sponsorship of the account is
  pending it's unavailable, to try again (`sponsored-in-flight`). Its `id` is
  `keccak256(abi.encode(account, authorizations, call's data, gas, transactions, token, payee,
  amount, maxBlockNumber))`, the transactions as `bytes[]`, and its status is
  `postpaid_getStatus`'s, at REST's `Location`.
- **Requests are read strictly:** a field a request doesn't have is invalid params,
  `malformed`, naming it (`calls[0].nonce`, `payment.transfer.salt`). So are a quote, a
  submission, a sponsorship's quote and a sponsorship, with the calls, payment, transfer,
  deployment and transactions to sign within them: a sponsorship's call has no `value` to
  give, and a transaction to sign no sender, nonce or fees. x402's body is read the same, one with such a field being a body
  that isn't a call. A user operation, an authorization and an x402 payment are read as their
  own standards have them, other fields ignored.
- **`token`, `tokens`:** assets, ETH being ERC-7528's address.
- **`payment.payee`** must be the relay's: its payee for a submission, its forwarder for a
  sponsorship.
- **`id`:** `keccak256(abi.encode(calls, authorizations, token, payee, amount,
  maxBlockNumber))`, the calls as `(to, data, value, gas)[]`, a call stating no gas as zero,
  the authorizations as `(chainId, address, nonce, yParity, r, s)[]`; a
  payment transfer in place of `maxBlockNumber`, as `(from, to, value, validAfter,
  validBefore, nonce, signature)`; a `deployment` appended as `(account, factory,
  factoryData)`. REST answers 202 for a new submission, 200 for one already taken.
- **`authorizationList`** is the type-4 transaction's JSON shape. An authorization's nonce is
  at most 2^53 − 1, so every client reads it exactly as a number.
- **`stage`:** `waiting` (taken, not yet sent), `sending` (offered to builders, `attempts`
  times), `confirming` (included; `confirmations` counts its block and each since), `done`
  (its block finalized), `failed` (`status` rejected or expired). REST answers `Retry-After`
  until it's done or failed: when the stage could next change.
- **`transactionHashes`:** each of the submission's transactions that landed, in the order
  they were sent. An included submission's are all of them. Most have one; calls sent each in
  a transaction of their own have each call's, in order, and then the transfer's;
  a sponsorship has the relay's first, the account's own, and the relay's last.

The JSON-RPC methods are described by an OpenRPC 1.3.2 document, answered by
`rpc.discover` with the chain's endpoint as its server.

A REST error's body is `{"error": <the JSON-RPC error object>}`:

| Code | Meaning | `data` | HTTP |
|---|---|---|---|
| -32602 | Invalid params | `{reason, field, most?, needs?}` | 400 |
| -32010 | Underpaid at the next block's price | `{reason, token, required}` | 422 |
| -32011 | Simulation failed | `{reason, revertData}` | 422 |
| -32012 | Unsupported token or payee | `{reason, category}` | 422 |
| -32013 | No capacity | `{reason, token, required}`: the amount that would be taken | 503 |
| -32014 | Unknown submission | `{reason}` | 404 |
| -32015 | Unavailable: the chain can't be served now, or a node failed the request | `{reason, category}` | 503 |
| -32601 | Not found: no such path, or the chain isn't served; a method the path doesn't take is 405, with `Allow` | `{reason}`, and `chain` for a chain not served, as the path names it | 404 |
| -32603 | Internal error, not to be retried; a chain whose configuration is its operator's mistake says what and where | `{reason, field}` for a configuration | 500 |

An error is told apart by its code and its data's `reason`, the refusal it is, one of the
OpenAPI document's `Refusal` schema. What doesn't decode as the request's types is `malformed`,
and a failed simulation's `reason` is its revert's. The message isn't part of the interface: it
says what failed, for people and models alike. `field` is the
request's field at fault, as a path into its parameter (`calls[0].to`, `authorizationList[0]`), named
as the interface's Request column names it, the same over REST and JSON-RPC: a user operation's
fields within it (`maxFeePerGas`), a parameter wrong as a whole by its name (`submission`, `id`),
and a REST body that isn't an object `body`. A field over a limit has `most`, the most it may be
(for a deadline, the latest block it may name), and `needs` where what it needs isn't what was
given (a call's gas that fits, in a transaction that doesn't). A JSON-RPC request that isn't one
is an Invalid Request (-32600) with the same `data`: `field` its `jsonrpc`, `method`, `id` or
`params`, or `request`; a body that isn't JSON is a Parse error (-32700), with no `data`. A
chain that isn't served is refused before its request is read.

Unsupported and Unavailable say who can act on them in `category`: `retry`, the client trying
again later; `request`, the client asking for something else; `operator`, only the relay's
operator.

### x402

A relay may take payment by x402 (v2, over HTTP, the `exact` scheme on EVM with EIP-3009), at
`POST /{chain}/v1/x402`, REST only. The request's body is the calls: `calls: [{to, data, value?,
gas?}]`, `authorizationList`?, and, as for a quote, `payer`?, `deployment`?, `verifyGas`?. Without
one call's `gas`, it's estimated as for a quote, for the challenge and again when the payment is taken. Its
payment
is always a transfer: `payer` names the account it will be from before it's signed, so a
contract's check is priced; without it, the account `deployment` deploys is taken to be it.

1. **Without `PAYMENT-SIGNATURE`:** 402, `PAYMENT-REQUIRED` and the same in the body. Each
   requirement is a token the relay takes this way: `amount` the call's quote, `payTo` the
   payee, `maxTimeoutSeconds` the longest a paid request is held, `extra` the token's EIP-712 domain. A
   body that isn't a call is priced as a reference call, the description says which.
2. **With `PAYMENT-SIGNATURE`:** a `transferWithAuthorization` of `value` to the payee, valid
   from the next block. It's the submission's payment: the payee must gain at least `value`
   during the call, and the authorization is spent with the call, as a submission's
   transfer is, or not at all. The deadline is the last block expected before `validBefore`, at
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
| A `PAYMENT-SIGNATURE` that isn't a payment payload | 400 | `PAYMENT-RESPONSE` `invalid_payload`, no `payer`; the body the error object |
| A token not taken over x402, or suspended | 402 | `PAYMENT-REQUIRED`; `PAYMENT-RESPONSE` `invalid_payment_requirements` |
| Any other refusal (a call that reverts at the challenge, its fields invalid, the chain unavailable) | as over REST | the error object alone, no `PAYMENT-RESPONSE` |
| A chain that takes no x402 payments | 404 | the error object |

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

**A transfer to sign is the relay's word.** A quote's `typedData` is what the relay asks the
payer to sign. A client checks it's the payment before signing: its `domain` the token and
chain meant, its `to` the payee `postpaid_config` names, its `from` the payer, its `value`
within what the client will pay. Signed unchecked, a relay could have it pay someone else.

**A sponsorship is kept whole by a builder.** A submission's payment is checked in its own
transaction, so the chain enforces it. A sponsorship is several transactions, and only the
builder that includes them keeps them together. If one includes part of it, the account's
transactions that landed stay landed, and the relay doesn't send the rest. A submission's
calls are in one transaction unless each is to a target the relay calls directly, when they're
a bundle too. Calls that must not be left half done whatever a builder does belong in one
call: the account's own batch.

**A signed transaction doesn't expire.** `maxBlockNumber` is when the relay stops sending a
sponsorship, not when its transactions stop being valid. Whoever holds one, the relay or a
builder it reached, can include it for as long as its nonce is unused. The account withdraws
one by using its nonce.

**What a quote gives to sign is the relay's word.** A client checks a sponsorship's
transactions before signing them: each is the call it gave, on its chain, at consecutive
nonces, and the payment is a transfer of an amount it will pay, in the asset it chose, to the
quote's forwarder, and nothing else.

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
