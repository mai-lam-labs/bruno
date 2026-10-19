# NPD spec: pay-in-tokens gas relay

**Status:** draft for implementation, 2026-10-10 (Saturday). Target release Monday 2026-10-12.
**Audience:** the coding agent building it. Background research is in
`MEV_GASLESS_COMPOSABLES.md`, `GASLESS.md` and `RESEARCH_QUEUE.md`, but this
document is meant to stand alone.

## 1. What it is

A permissionless service that submits other people's transactions and pays
their gas in ETH. The transaction itself repays the service in an accepted
ERC-20 token. A client with no ETH sends one or more calls (typically a
7702-delegated account's signed batch), plus its 7702 authorization if it
needs one. The service wraps them in one transaction from its own account,
multicall-style:

```
type-4 tx from a service account → CREATE2 factory (salt ‖ initcode)
  authorizationList: the client's 7702 authorization (optional)
  the factory runs the service-generated initcode at a fresh address:
  1. runs the client's calls, in order (whatever they are)
  2. asserts the payment: revert unless the payee received at least the
     agreed amount of the agreed token during step 1
  3. returns empty runtime code, so nothing is left at the runner's address
```

The runner is a fresh address every time. No long-lived contract ever acts
as `msg.sender` for user calls, so there's no persistent address an attacker
could reuse permissions against.

The service gets that transaction into a block through builder bundles. If
the client's calls didn't pay, the assertions revert the transaction, the
builder drops it, and the service has paid nothing.

- **No accounts, no API keys, no allowlist.** Anyone whose call pays is served.
- **No trust in the client.** Payment is enforced on-chain by the runner's
  initcode, not by off-chain checks.
- **No trust in the service beyond liveness.** The client's signed payload
  fixes what runs and what it pays; the service can only include it or not.

### Why it exists

A fresh account holding only tokens (WETH, WBTC, USDT…) can't send its first
transaction, because every transaction's sender must hold ETH for gas.
- **Existing paid-inclusion services don't help.** mev-boost bundles and
  ETHGas preconfirmations still require the sender to hold ETH, and a
  preconfirmation can't be combined with a bundle.
- **ERC-4337 paymasters only partly help.** They only work permissionlessly
  for permit tokens (Circle's paymaster, USDC only), because a paymaster is
  paid before the UserOp runs.
- **This service** fronts the gas and is repaid at the end of the same
  transaction, from whatever token the account holds.

## 2. Scope for Monday

**In scope (v1):**
- Ethereum mainnet, plus Sepolia for integration testing.
- Accepted tokens: a wide set, any token the operator's pricing oracles cover
  (listed per chain in `/v1/config`).
- Inclusion through mev-boost builder bundles (`eth_sendBundle`).
- The flow:
  - submissions through the HTTP API;
  - the CREATE2 runner;
  - float → concurrency-account fan-out inside bundles;
  - per-block bundle building with user transactions marked droppable;
  - status tracking.
- Two payment modes (Section 9): **in-call** (the client's calls pay, and
  the runner asserts it) and **x402 prepaid** (the runner pulls an x402
  payment first, then the client's calls may fail).
- Pricing from the operator's existing oracle and accounting stack
  (Section 7).

**Out of scope (later):**
- **L2s.** They have no public builder-bundle market; they need a different
  inclusion path.
- **Treasury conversion** of collected tokens back to ETH. Secondary; the
  focus is whether the core mechanic works.
- **An MCP interface.**

## 3. Roles and keys

| Thing | What it is | Key |
|---|---|---|
| **Float** | The service instance's own hot account: one per instance, the same address on every chain. It is also the payee: clients' token payments go to it. The operator keeps it topped up with ETH on each supported chain, and it funds the concurrency accounts. | The instance's root key |
| **Concurrency accounts** | Sub-accounts derived from the float's key (`m/44'/60'/0'/0/i`) as the per-block load needs them, up to a configured cap. Each sends at most one user transaction per block. | Derived from the float's key |
| **CREATE2 factory** | The Arachnid deterministic deployer `0x4e59b44847b379578588920cA78FbF26c0B4956C` (Section 5). Existing, ownerless; it's only ever the deployer, never the caller of user code. | None |
| **Runner** | Initcode generated per transaction and run at a fresh CREATE2 address. It runs the client's calls and the payment assertion, then leaves no code. | None |
| **Client** | Anyone. Signs their own 7702 authorization and payload. | Theirs |

The service never holds a client's keys.

## 4. Client-facing flow

1. **Config.** `GET /v1/config` returns the chains, accepted tokens, payee
   (the float), the factory address, pricing parameters and limits.
2. **Quote.** `POST /v1/quote` with the chain, token and gas limit returns the
   required amount and the block until which the quote holds.
3. **Build the calls.** The client builds one or more calls that, between
   them, transfer at least the quoted amount of the token to the payee. The
   runner is `msg.sender` for each call, and it's a fresh address that holds
   no rights. So each call must be authorised by the client's own signature,
   not by the caller. The typical shape is a
   7702-delegated EOA running a signed batch:
   - **Calibur** (`0x000000005c84F8Fd50b21CAC312528A64437030e`): one call
     to the client's EOA, `execute(SignedBatchedCall, wrappedSignature)`,
     with `executor = 0` (anyone may run it; the payment goes to the float
     whoever submits) and the batch
     `[approve…, venue call…, token.transfer(payee, amount)]`.
   - The client also signs a 7702 authorization for its EOA if it isn't
     delegated yet.
   - Other shapes work too: an EIP-3009 `transferWithAuthorization` to the
     payee, a Permit2 signature transfer, a venue call that pays a fee to a
     named address. The service doesn't care what the calls are. Its
     assertions only check what the payee received.
4. **Submit.** `POST /v1/submit` with the calls, an optional authorization
   list, a gas limit, the payment token and amount, and a deadline block.
5. **Track.** `GET /v1/status/{id}` until it reports `included`, `rejected`
   or `expired`.

## 5. The runner (CREATE2, one-shot)

**Why not a long-lived contract.** A deployed contract that runs user calls
gives every user the same `msg.sender`. Anything that ever grants that
address a right becomes usable by the next caller: a token approval, a
Calibur batch naming it as executor, or a protocol allowlist. So the service
deploys a fresh, codeless runner for every transaction instead.

**Mechanism.** The service emits initcode for each submission and deploys it
through the Arachnid deterministic deployer,
`0x4e59b44847b379578588920cA78FbF26c0B4956C`, with calldata
`salt ‖ initcode`. The initcode is generated bytecode, not a compiled
contract, and does the following in order:

1. **Record balances.** For each payment `(token, payee, minAmount)`, read
   the payee's balance into memory: `BALANCE(payee)` for native ETH, or
   `STATICCALL token.balanceOf(payee)` for a token. Check that the call
   succeeded and returned 32 bytes, otherwise revert.
2. **Run the client's calls,** in order. Each call's data is embedded after
   the code and copied to memory with `CODECOPY`. Then
   `CALL(gas, target, 0, …)`: all remaining gas, value 0. If a call fails,
   copy its return data and revert with it.
3. **Assert payment.** Read each balance again. Revert unless
   `after ≥ before` and `after − before ≥ minAmount`.
4. **Leave nothing.** `RETURN(0, 0)`, so the runner's address ends with no
   code.

**Properties that follow:**
- **Fresh address per transaction:**
  `keccak256(0xff ‖ factory ‖ salt ‖ keccak256(initcode))[12:]`. Afterwards
  it has nonce 1 and no code, so CREATE2 can't deploy there again and it can
  never execute anything. Rights granted to it during the call are dead.
- **Codeless while it runs.** During construction the address has no code,
  so callbacks to it behave as they would against an EOA.
- **A short payment reverts everything.** The deployer reverts when CREATE2
  returns zero (its code is `… f5 80 15 15 60 39 57 81 82 fd …`, identical
  on Ethereum, Sepolia, Hoodi, Arbitrum and Base, read 2026-10-10). The
  builder then drops the transaction.
- **Net payment.** The balance change is measured across all the client's
  calls, so paying and then pulling the money back fails.
- **Salt:** unique per submission, for example `keccak256(submissionId)`.
  A dropped attempt never creates the address, so the same salt and
  initcode can be resent in the next block.
- **Size:** initcode is capped at 49,152 bytes (EIP-3860), which bounds a
  submission's total call data.
- **Gas overhead:** CREATE2's 32,000, initcode hashing and word costs, and
  the balance reads. There's no code-deposit cost. Measure it and add it to
  the client's gas limit.
- **Executor:** clients sign Calibur batches with `executor = 0`. The
  runner's address depends on the initcode, which contains the batch, so a
  batch can't name it.

**Tests to write** (on the local anvil chain, Section 11.1):
- payment above, at and below `minAmount`, for a token and for native ETH;
- a client call that reverts (the transaction must revert with its data);
- paying and then pulling back (must fail);
- several calls where the payment comes from a later one;
- the runner's address afterwards: no code, and resending the same salt and
  initcode fails;
- a fresh 7702-delegated Calibur EOA holding only USDC whose signed batch
  approves Permit2 for WETH and pays the float, in one type-4 transaction to
  the deployer.

## 6. Execution engine

### 6.1 The outer transaction

For each accepted submission, the service builds a transaction from a
concurrency account:
- **Type and target:** a type-4 (EIP-7702) transaction if the submission
  carries an `authorizationList`, otherwise type-2. In both cases `to` is the
  CREATE2 factory.
- **Data:** `salt ‖ initcode`, where the initcode (Section 5) embeds the
  client's calls as submitted and the payment `(token, float, amount)` from
  the submission's terms.
- **Gas:** `gasLimit` is the client's gas limit plus the runner's overhead
  (CREATE2 and the assertions; measure it). The priority fee and max fee come from
  Section 7.
- **Signing:** the concurrency account's key, re-signed every block with
  fresh fees.

The client's `authorizationList` entries are passed through untouched. The
service doesn't need to understand them.

### 6.2 The per-block bundle

Each block (target block = head + 1), build one bundle per chain:

```
tx0:   float → Disperse/Multicall: send ETH to every concurrency account used below,
       topping each up to (gasLimit × maxFeePerGas) for its transaction plus a small buffer
tx1…k: concurrency account i → CREATE2 factory (one user submission each)
```

- **Builder parameters:** submit with `eth_sendBundle`:
  `{ txs, blockNumber, droppingTxHashes: [tx1…txk] }`.
  - **Droppable, not revertible.** User transactions go in `droppingTxHashes`,
    so one that would revert or is invalid is left out and the rest of the
    bundle still lands.
  - **Never use `revertingTxHashes`** for user transactions. A reverted
    transaction that gets included costs gas and pays nothing.
  - **The funding transaction isn't droppable.** If `tx0` fails, the bundle
    fails.
- **No replacement needed.** A bundle targets one block. Keep the queue on
  our side, and each block send a fresh bundle for `head + 1` with the
  remaining submissions and fresh fees. A bundle that misses its block simply
  expires. (`replacementUuid` exists at most builders below if it's ever
  wanted.)
- **Builders:** send each bundle to every builder below that documents
  `droppingTxHashes` (verified from their docs, 2026-10-10):

  | Builder | Endpoint | `droppingTxHashes` | Auth |
  |---|---|---|---|
  | Titan | `https://rpc.titanbuilder.xyz` | yes: "allowed to be discarded, but may not revert" | not documented |
  | BuilderNet | `https://rpc.buildernet.org` | yes, since v1.3: "allowed to be removed from the bundle (but not allowed to revert)" | `X-Flashbots-Signature` (any key; an identity, not an API key) |
  | Quasar | see docs.quasar.win | yes: "allowed to be discarded, but may not revert" | not documented |
  | BTCS Builder+ | `https://rpc.btcs.com` | yes: "silently dropped from the bundle (not reverted on-chain)"; bundle version unset or ≥ v2 | not documented |
  | Flashbots relay | `https://relay.flashbots.net` | **not listed**, only `revertingTxHashes` | `X-Flashbots-Signature` |
  | Blink | `ethauction.blinklabs.xyz/v1/{API_KEY}` | **not listed** | key in URL |
  | rsync | docs site unreachable on 2026-10-10 | search snippet says yes; unverified | — |

  Before mainnet, confirm the behaviour live on one builder: a bundle whose
  droppable transaction would revert should land without it.
  - **Don't confuse the two lists.** Titan describes `revertingTxHashes` as
    "allowed to revert or be discarded", which could include the transaction
    reverted. Never use it for user transactions.
  - **Builders without drop support** (Flashbots relay, Blink): send them
    nothing, or one bundle per user transaction from a pre-funded account.
- **Float nonce:** the float is the serialisation point, with one funding
  transaction per bundle per block. If the bundle doesn't land, the float's
  nonce isn't consumed. Reconcile nonces from chain state every block.
- **Skip funding** for accounts that already hold enough (`tx0` covers only
  the shortfall). If no account needs funding, leave `tx0` out.

### 6.3 Concurrency accounts and nonces

- Assign each submission to a free concurrency account, deriving the next
  index when none is free (up to the cap). An account carries at most one
  in-flight user transaction (one nonce) at a time, so the number in use
  equals the number of submissions pending for the next block.
- When the bundle lands, read the receipts:
  - included user transactions are marked `included` and their accounts
    freed with nonce + 1;
  - dropped user transactions keep their account and nonce and go back into
    the next block's bundle.
- If the bundle didn't land, everything carries over to the next block.
- At startup and every block, reconcile each account's nonce and balance
  from chain (`eth_getTransactionCount` at `latest`). Chain state wins.
- Sweep ETH back from concurrency accounts to the float when idle (optional
  in v1).

### 6.4 Simulation (filter, not guarantee)


Before adding a submission to a bundle, simulate the full bundle at the
target block. Use `eth_callBundle` on a builder or relay that offers it, or
`eth_simulateV1` on your node, with the authorization list applied.
- Reject the submission up front if it reverts (`CallFailed` or
  `Underpaid`) or uses more gas than its limit.
- Keep simulating each block while it's pending. Drop it from the bundle if
  it starts failing, and mark it `rejected` after M consecutive failures or
  once its deadline passes.

The runner's assertion is what protects funds. Simulation only keeps bundles clean and
spares the service from relying on builders' drop behaviour.

## 7. Pricing

Pricing comes from the operator's existing stack: oracles for the major
pairs, a model of expected inclusion time against the payment currency and
the gas cost, and the operator's accounting. The spec fixes only what the
mechanism needs:
- **A quote names one amount in one token,** and the client signs that
  amount into its calls (in-call mode) or its x402 authorization (prepaid
  mode). The runner checks that exact amount, so it can't change after
  signing.
- **Price on the gas limit, not the gas used.** The service's worst case,
  gas limit × fee, is then covered by a fixed, signed amount.
- **Acceptance per block:** a submission goes into a block's bundle only if
  its amount covers that block's price. Otherwise it waits until the price
  falls or its deadline passes, then expires. The service never includes a
  submission at a loss.

## 8. Interfaces

The same four operations (config, quote, submit, status) are exposed through
the operator's stack over REST, JSON-RPC and MCP. Documentation and schemas
are generated from it. This section defines the REST shapes (8.0) and the
JSON-RPC methods (8.1). The MCP tools mirror the JSON-RPC methods one for one.

### 8.0 REST

JSON over HTTPS, no authentication. Rate-limit per IP. All amounts are
decimal strings in base units; addresses are checksummed hex.

#### `GET /v1/config`

```json
{
  "chains": [{
    "chainId": 1,
    "factory": "0x4e59b44847b379578588920cA78FbF26c0B4956C",
    "payee": "0x…",
    "maxGasLimit": 1000000,
    "tokens": [
      { "symbol": "WETH", "address": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "decimals": 18, "price": "fixed:1" },
      { "symbol": "USDC", "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "decimals": 6, "price": "chainlink:0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419" }
    ],
    "pricing": { "runnerOverheadGas": 60000, "quoteValidityBlocks": 25 }
  }]
}
```

#### `POST /v1/quote`

Request:

```json
{ "chainId": 1, "token": "0xA0b8…eB48", "gasLimit": 250000 }
```

Response:

```json
{ "chainId": 1, "token": "0xA0b8…eB48", "amount": "1234567", "payee": "0x…", "factory": "0x4e59…956C",
  "gasLimitTotal": 280000, "validUntilBlock": 21000123 }
```

Quotes are advisory: there's no quote id and nothing is reserved. The
acceptance rule in Section 7 decides.

#### `POST /v1/submit`

Request:

```json
{
  "chainId": 1,
  "calls": [
    { "target": "0xUserEOA", "data": "0x…" }
  ],
  "authorizationList": [
    { "chainId": "0x1", "address": "0x000000005c84F8Fd50b21CAC312528A64437030e", "nonce": "0x0", "yParity": "0x1", "r": "0x…", "s": "0x…" }
  ],
  "gasLimit": 250000,
  "payment": { "token": "0xA0b8…eB48", "amount": "1234567" },
  "deadlineBlock": 21000150
}
```

For x402 prepaid mode (Section 9.2), leave out `payment`. The server answers
`402` with the x402 challenge, and the client retries with
`PAYMENT-SIGNATURE`.

**Validation, in order** (each failure answers 400 with a code):
1. The chain and token are supported.
2. `gasLimit ≤ maxGasLimit`.
3. `amount ≥` the current price (otherwise `UNDERPAID`; the client can
   still resubmit with more).
4. The deadline is in the future and within a maximum window (for example
   300 blocks).
5. Simulation passes (otherwise `SIMULATION_FAILED`, with the revert data).
6. There's capacity: a free concurrency account, and the float balance is
   above its floor (otherwise 503 `NO_CAPACITY`).

Response: `202 { "id": "<uuid>", "status": "pending" }`.

#### `GET /v1/status/{id}`

```json
{ "id": "…", "status": "pending|included|rejected|expired",
  "reason": "SIMULATION_FAILED|UNDERPAID_AT_BLOCK|DEADLINE|…",
  "txHash": "0x…", "blockNumber": 21000124, "attempts": 3 }
```

### 8.1 JSON-RPC

Served next to the standard `eth_*` methods on the operator's endpoint.
Public, with no bearer token.

**Conventions:**
- **Namespace `postpaid_`.** A neutral name other implementers can adopt.
- **Ethereum JSON-RPC encoding:** hex quantities, checksummed addresses.
- **`calls`** use EIP-5792's `{ to, data }` shape. `value` is always 0, so
  it's left out.
- **`authorizationList`** uses the type-4 transaction JSON shape from
  `eth_sendTransaction`.

#### `postpaid_config`

```json
{"jsonrpc":"2.0","id":1,"method":"postpaid_config","params":[]}
```
```json
{"jsonrpc":"2.0","id":1,"result":{
  "chainId": "0x1",
  "payee": "0xFloat…",
  "deployer": "0x4e59b44847b379578588920cA78FbF26c0B4956C",
  "modes": ["inCall", "x402"],
  "tokens": ["0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "0xdAC17F958D2ee523a2206206994597C13D831ec7", "…"],
  "maxGas": "0xf4240",
  "maxCallBytes": "0xa000",
  "overheadGas": "0xea60"
}}
```

#### `postpaid_quote`

```json
{"jsonrpc":"2.0","id":2,"method":"postpaid_quote","params":[{
  "chainId": "0x1",
  "gas": "0x3d090",
  "tokens": ["0xA0b8…eB48", "0xdAC1…1ec7"],
  "mode": "inCall"
}]}
```
```json
{"jsonrpc":"2.0","id":2,"result":{
  "validUntilBlock": "0x18f2a3c",
  "quotes": [
    { "token": "0xA0b8…eB48", "amount": "0x12d687" },
    { "token": "0xdAC1…1ec7", "amount": "0x12e1a0" }
  ]
}}
```

With `"mode": "x402"`, each quote also carries a standard x402
`PaymentRequirements` object (`scheme`, `network`, `asset`, `amount`,
`payTo`, `extra.assetTransferMethod`), so existing x402 client code can sign
it unchanged.

#### `postpaid_sendTransaction`

In-call payment:

```json
{"jsonrpc":"2.0","id":3,"method":"postpaid_sendTransaction","params":[{
  "chainId": "0x1",
  "calls": [
    { "to": "0xUserEOA", "data": "0x<Calibur execute(signedBatch, sig)>" }
  ],
  "authorizationList": [
    { "chainId": "0x1", "address": "0x000000005c84F8Fd50b21CAC312528A64437030e", "nonce": "0x0", "yParity": "0x1", "r": "0x…", "s": "0x…" }
  ],
  "gas": "0x3d090",
  "maxBlockNumber": "0x18f2a50",
  "payment": { "type": "inCall", "token": "0xA0b8…eB48", "amount": "0x12d687" }
}]}
```

x402 prepaid carries the x402 `PaymentPayload` unchanged:

```json
"payment": { "type": "x402", "payload": { "x402Version": 2, "scheme": "exact", "network": "eip155:1",
             "payload": { "signature": "0x…", "authorization": { "from": "0x…", "to": "0xFloat…", "value": "1234567", "validAfter": "0", "validBefore": "…", "nonce": "0x…" } } } }
```

Result:

```json
{"jsonrpc":"2.0","id":3,"result":{
  "id": "0x<keccak of the canonical submission>",
  "runner": "0x<CREATE2 address the calls will run from>",
  "status": "pending"
}}
```

- **`id`** is a hash of the canonical submission, so resending the same
  submission is idempotent, like a bundle hash.
- **`runner`** is the CREATE2 address the calls will execute from (Section
  5), known before inclusion.

#### `postpaid_getStatus`

```json
{"jsonrpc":"2.0","id":4,"method":"postpaid_getStatus","params":["0x<id>"]}
```
```json
{"jsonrpc":"2.0","id":4,"result":{
  "status": "included",
  "transactionHash": "0x…",
  "blockNumber": "0x18f2a41",
  "attempts": "0x3"
}}
```

The status is one of `pending`, `included`, `expired` (deadline passed) or
`rejected` (with a `reason`). A per-block drop is internal; the client keeps
seeing `pending`.

#### Errors

| Code | Meaning | `data` |
|---|---|---|
| `-32602` | Invalid params | the field |
| `-32402` | Payment required (x402 mode, no payload) | x402 `PaymentRequirements`, as in an HTTP 402 |
| `-32010` | Underpaid at the current price | `{ "required": "0x…", "token": "0x…" }` |
| `-32011` | Simulation failed | `{ "callIndex": "0x0", "revertData": "0x…" }` |
| `-32012` | Unsupported token or chain | |
| `-32013` | No capacity | `{ "retryAfterBlocks": "0x2" }` |

**Optional, later:**
- `postpaid_cancel(id)`: best effort. It stops resubmission, but can't
  recall a bundle a builder already holds.
- `postpaid_estimate`: `eth_estimateGas` for a full submission without
  payment, returning the gas and the quotes together.

## 9. Payment modes

The runner's initcode (Section 5) has two variants, chosen per submission.

### 9.1 In-call payment (Section 5 as written)

The client's own calls pay the float. The runner records balances, runs the
calls (any failure reverts), asserts the float received at least the price,
and leaves no code. If the calls don't pay, the transaction reverts and is
dropped.

### 9.2 x402 prepaid

The client pays with an x402 payment signature, and the runner pulls it
before running anything. The service doesn't care whether the client's calls
then succeed: once the pull has landed, the service is paid.

**HTTP flow** (standard x402 `exact` scheme on EVM):
1. `POST /v1/submit` without payment terms returns `402 Payment Required`
   with a `PAYMENT-REQUIRED` challenge. It contains `scheme: "exact"`,
   `network: "eip155:<chainId>"`, `asset`, `amount` (the price from
   Section 7), `payTo` (the float), `maxTimeoutSeconds`, and
   `extra.assetTransferMethod`, which is `eip3009` or `permit2`.
2. The client signs and retries with `PAYMENT-SIGNATURE`. The server
   validates it and answers `202 { id }`. The payment settles on-chain only
   when the transaction lands; report it through `GET /v1/status/{id}`.

**Runner initcode, prepaid variant:**
1. **Pull the payment.** If it fails, revert, so the transaction is dropped.
   - **`eip3009`:**
     `token.transferWithAuthorization(from, float, value, validAfter, validBefore, nonce, signature)`.
     Anyone may submit it, so the runner can.
   - **`permit2`:** `x402ExactPermit2Proxy.settle(permit, owner, witness, signature)`
     on the canonical proxy, `0x402085c248EeA27D92E8b30b2C58ed07f9E20001`.
     The client signs a Permit2 `permitWitnessTransferFrom` with
     `spender = proxy` and `witness = { to: float, validAfter }`. `settle` has
     no caller restriction and transfers only to `witness.to`. This needs a
     prior Permit2 approval from the client; alternatively
     `settleWithPermit` takes an EIP-2612 permit in the same call.
     - The proxy is needed because the runner can't be the Permit2 spender:
       its address depends on its initcode, which contains the signature.
     - Deployed on Ethereum, Sepolia, Base and Base Sepolia, but **not on
       Hoodi** (read 2026-10-10). Anyone can deploy it there at the same
       address:
       - **How:** the mainnet deployment (tx
         `0x0017383c2a7ff3fd8c4eb5c107619c63048aeed55084b957db18c32288180e9a`)
         was a plain call to the Arachnid deployer with
         `salt = 0x…3000000007263b0e` and initcode = the proxy's bytecode plus
         the Permit2 address as its only constructor argument. There's no
         owner and no initialiser.
       - **Checked:** replaying that transaction's input to
         `0x4e59b44847b379578588920cA78FbF26c0B4956C` on Hoodi simulates to
         `0x402085c248EeA27D92E8b30b2C58ed07f9E20001` (eth_call), with an
         estimate of 722,776 gas.
       - **Permit2 on Hoodi** is the canonical contract: its bytecode differs
         from mainnet's only in the immutables for the chain ID and its
         domain separator.
2. **Run the client's calls,** each `CALL(gas − reserve, target, 0, …)`. The
   result is ignored, so a failed call doesn't revert the transaction. Hold
   back a small gas reserve so the runner can always finish after a call
   runs out of gas.
3. **Leave nothing.** `RETURN(0, 0)`.

There's no balance assertion. The pull either moves the signed amount to the
float or reverts.

**Properties:**
- **Paid or not included.** If the pull fails (a bad signature, an expired
  authorization, too little balance), the transaction reverts and is dropped
  from the bundle. Otherwise it lands, whatever the client's calls do.
- **Single use.** The EIP-3009 nonce or Permit2 nonce is consumed on
  landing. A submission expires at the authorization's `validBefore` or
  `deadline`, or at its own deadline block, whichever comes first.
- **Pricing as in Section 7,** on the gas limit, plus the pull's gas.
- **Tokens.** `eip3009` covers tokens with `transferWithAuthorization`
  (USDC, USDT0 and others). `permit2` covers any token the client has
  approved to Permit2, which for a fresh account means one with an EIP-2612
  permit, through `settleWithPermit`.

## 10. Risks and mitigations

| Risk | Effect | Mitigation |
|---|---|---|
| In-call mode: the user's call succeeds without paying | Service pays the gas, unpaid | The runner's initcode reverts, the factory reverts, and the transaction is dropped from the bundle |
| x402 prepaid mode: the client's calls fail after the pull | The client has paid for a failed run | By design: payment buys inclusion, not success. Simulate and report the expected outcome before accepting |
| Someone reuses rights granted to the service's calling address | Theft through the service | No long-lived caller exists: each runner is a fresh CREATE2 address that ends with no code and can't be redeployed |
| A builder ignores `droppingTxHashes` and includes a reverting transaction | Service pays the gas, unpaid | Only use builders verified to honour drops; simulate every block; never set `revertingTxHashes` |
| A builder without drop support | One bad transaction voids the whole bundle for that block | Simulation filters; retry next block; or send such builders one bundle per submission |
| State changes between simulation and the block (the user front-runs and moves the tokens) | The user's transaction reverts | Dropped; the submission is retried or expires |
| Base fee spikes after the quote | The payment no longer covers the gas | Wait until it does, or expire; never include at a loss |
| Simulation spam (DoS) | CPU and RPC cost | Per-IP rate limit; a cap on pending submissions per `target`; a minimum payment; reject large gas limits |
| Float drained or empty | No capacity | Alert at a floor; `NO_CAPACITY` (503) |
| Hot key compromise | Loss of the float's ETH and the tokens it has collected | The float is hot by design: keep its balances bounded by sweeping surplus to the operator; alert on unexpected outflows |
| Price source stale | Mispriced | Stop quoting and accepting a token while its price feed is stale |
| An authorization with the wrong nonce | The transaction is invalid | The simulation catches it; return the authority's current nonce in the error |

## 11. Testing plan

There are four tiers:
1. **CI, every commit:** each component works on its own, and the
   components compose.
2. **CI, scheduled:** the composition against a real builder.
3. **Public testnets, occasionally:** the hypothesis holds on a live
   network.
4. **Mainnet, a few pre-release checks.**

### 11.1 CI on a local anvil (every commit)

A fully local chain, with no fork: `anvil --hardfork prague`. The harness
deploys every dependency at the start of each run:
- **The CREATE2 deployer** at `0x4e59b44847b379578588920cA78FbF26c0B4956C`:
  anvil provides it by default. Check, and deploy it if missing.
- **Permit2** at its canonical address, by replaying its CREATE2 deployment
  through the deployer. The canonical address matters because the x402 proxy
  hardcodes it.
- **The x402 Permit2 proxy** at `0x402085c248EeA27D92E8b30b2C58ed07f9E20001`,
  by replaying its mainnet deployment input (Section 9.2).
- **A 7702 delegate:** Calibur built from `source/calibur`, or
  `Simple7702Account`. Its address only has to match what the test clients
  sign their authorizations to.
- **Test tokens covering each payment path:**
  - one with EIP-3009 (`transferWithAuthorization`);
  - one with EIP-2612 but no EIP-3009;
  - one with neither (WETH-like);
  - one that returns no bool from `transfer` (USDT-like);
  - native ETH.

**Components, each on its own:**
- **Runner initcode, in-call mode:**
  - payment above, at and below the minimum, for a token and for native ETH;
  - a reverting call (the whole transaction reverts with its data);
  - paying and then pulling back (fails);
  - payment from a later call;
  - the address afterwards has no code, and a re-deploy fails.
- **Runner initcode, prepaid mode:**
  - an EIP-3009 pull, and a Permit2 pull through the proxy, including
    `settleWithPermit`;
  - a failing pull (reverts);
  - a client call that reverts after a successful pull (lands, and the float
    is paid);
  - a call that burns all its gas (the runner still finishes).
- **7702:** a fresh EOA's authorization to Calibur applied in a type-4
  transaction to the deployer. A stale authorization (wrong nonce) means the
  calls hit a codeless account, and in-call mode reverts at the assertion.
- **Pricing and acceptance:** a submission is held while its amount is
  below the block's price, and expires at its deadline.
- **Nonces and accounts:** sub-account derivation, assignment, and
  reconciliation against chain state after included, dropped and missed
  blocks.

**Composition, through a mock builder.** anvil has no `eth_sendBundle`, so
the CI harness implements one. It applies rbuilder's rule, which we've read in
`commit_bundle_no_rollback` (`crates/rbuilder/src/building/order_commit.rs`,
`source/rbuilder` at `6037fa7`).
- **Per transaction,** in bundle order:
  - `evm_snapshot`, send it, mine;
  - if it reverted or was invalid and it's in `droppingTxHashes`,
    `evm_revert` to the snapshot (dropped: no state, no gas);
  - if it isn't droppable, revert to the bundle's starting snapshot (the
    whole bundle fails).
- **Then the full flow:** API → queue → per-block bundle (funding plus N
  user transactions, mixing payers and non-payers, stale nonces and stale
  authorizations) → mock builder → receipts → status. Check that:
  - only paying transactions land;
  - the float's ETH falls by exactly the gas of the transactions that
    landed;
  - its token balance rises by exactly their payments;
  - every account's nonce reconciles.

The mock builder is only as good as its match with rbuilder. Keep it to
that rule, cite the source line, and let the next tier catch any difference.

### 11.2 CI against a real builder (scheduled)

rbuilder can't run on anvil: it reads state from a Reth database (or an IPC
state provider that serves Reth-specific calls), and needs a consensus
client and a relay. Run a containerised devnet:
- Reth (dev chain, Prague), a consensus client and validator, a local
  mev-boost relay, and rbuilder;
- Flashbots' builder-playground describes this setup in rbuilder's README.
  It's unmaintained, and `decker` is its successor.

Send the same composition scenarios as 11.1 to rbuilder's `eth_sendBundle`,
and compare the landed blocks with the mock builder's results. Run nightly
or before each release. It's too heavy for every commit.

### 11.3 Public testnet integration (occasionally)

Same as mainnet: send every bundle to every testnet endpoint that takes
bundles, then wait for inclusion. Endpoints (2026-10-10):
- **Sepolia:** the Flashbots relay, `relay-sepolia.flashbots.net`. It
  probably ignores `droppingTxHashes` (same bundle hash with or without it),
  so expect a bundle with a failing transaction to be voided rather than
  trimmed.
- **Hoodi:** Titan, `rpc-hoodi.titanbuilder.xyz`. It parses
  `droppingTxHashes`. Few Hoodi blocks come through builders: over ~2,000
  slots, Flashbots' Hoodi relay delivered 100 blocks, none by Titan.
- **Contracts:** Sepolia has Calibur, the x402 Permit2 proxy and Circle's
  test USDC (`0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238`, from Circle's
  faucet). Hoodi lacks Calibur and the proxy; the proxy can be deployed
  there (Section 9.2), and `Simple7702Account` stands in for Calibur.

### 11.4 Mainnet pre-release checks

A small float, a low cap on concurrency accounts, and a handful of real
submissions in both payment modes, including one deliberate non-payer
(it must be dropped and cost nothing). Then raise the limits.

## 12. Implementation notes

- **The runner's initcode is generated by the service** (Section 5), and is
  tested on the local anvil chain (Section 11.1).
- **Chain state is the source of truth** for nonces and balances. Local
  state is a cache.
- **Record every bundle:** the target block, the transaction hashes, which
  transactions were dropped, and whether it landed.

## 13. Open questions

None block the build. Inclusion on public testnets, as on mainnet, depends
on whichever builders win blocks. The local rbuilder devnet is the
deterministic test of the bundle mechanics.
