# 0.5.4 code organisation (temporary, delete before merging)

Python/Worker parity and layout, carried from 0.5.3 (the drift review of 2026-11-01), as
preparation for 0.6 (MCP). Neither relay is the reference: each rename picks the better name of
the pair, and each split is made alike on both sides.

The naming review of 2026-11-01 read every module pair on both relays, then was triaged with
the user's rule: a name says what it holds, short where its module or type carries the rest; no
sweeping rename, but slices toward parity, stopping at diminishing returns. Each item is tagged
**parity** (the same thing named differently on the two sides) or **confusion** (one name for
two things where they meet, or a name that misleads read in isolation). What was reviewed and
left as it is, is at the end with its reason.

The splits (2) go first, `relay` as the test case: it's the file growing past its size. Then
work in slices: one item from A, or one module pair from B, on both relays together, tests run,
reviewed before the next. Line numbers are the review's, checked at the file before anything
changes. Python `name` (file:line) ↔ TS `name` (file:line) → proposed. Paths are under `bruno/`
and `worker/src/`.

## 1. Renames

### A. Words with two meanings

- [x] **confusion: settle.** (Done: `reconcile`, `reconcile_one`, `reconcile_funding` and their
  logs on both relays; DEPLOYMENT.md and the more-chains plan describe the step so.) The relay's per-block receipt reading, `_settle`, `_settle_one`,
  `_settle_funding` (relay.py:1061, 1086, 1100) ↔ `settle`, `settleOne`, `settleFunding`
  (relay.ts:1114, 1139, 1163), against x402's settlement (`x402.settled`) beside it. →
  "settle" for x402 only; the relay's `reconcile`, `reconcile_one`, `reconcile_funding`.
  plan.ts's `settled<T>` (plan.ts:420) is in B (plan).
- [x] **confusion: receipt_of.** (Done.) `chain.receipt_of` (chain.py:119 ↔ chain.ts:273), a
  transaction's; `userops.receipt_of` (userops.py:694 ↔ userops.ts:667), a UserOp's, aliased
  `userOperationReceiptOf` in bundler.ts:40 only. → userops' `user_operation_receipt_of`.
- [x] **parity: risen.** (Done.) Chain's base-fee bounds `risen`/`fallen` (chain.py:200, 206 ↔
  chain.ts:257, 265) took the bare name, so TS's DataFee bound is `risenDataFee`. → chain's
  `risen_base_fee`/`fallen_base_fee`; DataFee's `risen` on both.
- [x] **confusion: bundler.** (Done; the key is E.) The config's `Bundler` (config.py:135) ↔ `BundlerConfig`
  (config.ts:70) is a builder's `eth_sendBundle` endpoint (the runtime's `builders.Builder`),
  beside ERC-4337's `Bundler` (bundler.py:55, bundler.ts:52). → `BuilderConfig` on both. The
  `"bundlers"` key is E.
- [x] **confusion: margin.** (Done: `GAS_HEADROOM = 11/10`, a Fraction as the treasury's.) userops' `_MARGIN` ↔ `MARGIN` (userops.py:130 ↔ userops.ts:84) is
  gas-limit headroom; "margin" is the price markup (`margin_multiplier`) everywhere else. →
  `GAS_HEADROOM`, as the treasury's. The treasury's `_margin` is E.
- [x] **parity: float.** (Done.) `relay.float_account` (relay.py:352) ↔ `relay.float` (relay.ts:283),
  and the same in `accounts.derive`, `sign_funding` ↔ `signFunding`, maintenance. →
  `float_account` (`float` is a Python builtin).
- [x] **parity: encoded, ID_TYPE.** (Done: Python's `TRANSFER_ID_TYPE`, `DEPLOYMENT_ID_TYPE`; the
  Worker's free functions prefixed by owner, `transferEncoded`, `transferCalldata`,
  `transferEcdsa`, as `transferDeployment` and `deploymentEncoded` already were.) Python's `ID_TYPE` in both eip3009.py:120 and erc6492.py:41,
  aliased `DEPLOYMENT_ID_TYPE` in submission.py; TS's bare `encoded`, `calldata`, `ecdsa` from
  eip3009 (submission.ts:8, 98; plan.ts:21) beside `deploymentEncoded`. → by owner on both:
  `TRANSFER_ID_TYPE`, `DEPLOYMENT_ID_TYPE`; `transfer_encoded`, `deployment_encoded`.
- [x] **parity: measuring.** (Done.) Python's `measuring` is eip3009's, tokens' aliased
  `measuring_balance` (measurements.py:23, 29); TS's `measuring` is tokens', eip3009's aliased
  `measuringTransfer`/`measuredTransfer` (measurements.ts:14, 18). → the same aliases on both:
  `measuring_transfer`, `measured_transfer`, `measuring_balance`.
- [x] **confusion: pair.** (Done.) `pair_calls`/`pair_of` (prices.py:99, 104 ↔ prices.ts:87, 92) read any
  pool's two tokens, v2 or v3; a "pair" is a v2 pool. → `token_calls`/`tokens_of` (prices is
  about pools already).
- [x] **confusion: account.** (Done.) TS's `onBlock` calls the derived Signer `account` and the number
  `index` (relay.ts:992, 1009); `account` is the index everywhere else (`_Slot.account`). →
  `signer` for the Signer.

### B. Module pairs

Re-checked at the file 2026-11-02, after the relay split: each item below confirmed as stated,
its current place given; what didn't hold went to the end, with why.

#### relay, pricing
- [x] **parity:** `authority` (relay.py, pricing.py `quote_at`) ↔ `authorityAddress` (relay.ts
  `quote`, `x402Quote`; pricing.ts `quoteAt`) → `authority`.
- [x] **parity:** `Accepted(id, new)` (relay.py:101) ↔ inline `{id, isNew}` (relay.ts:211,
  operations.ts:138) → `Accepted` named in TS.

#### plan, submission
- [ ] **parity:** `_revert` (plan.py:405) ↔ `revertOf` (plan.ts:403) → `revert_of`.
- [ ] **parity, confusion:** plan.ts's `settled<T>` unwraps a PromiseSettledResult (plan.ts:86,
  batches.ts:483), beside x402.ts's exported `settled`; Python unwraps inline in `make_plan`
  (plan.py:71) → one name, not "settled".
- [ ] **confusion (low):** `empty(chain, authorities)` returns a count (plan.py:326,
  plan.ts:348), beside the field `Draft.empty` → `count_empty`.
- [ ] **parity:** `authorities` (submission.py:124, submission.ts:104), imported as
  `authorities_of`/`authoritiesOf` by both plans, bare by bundler and x402 → `authorities_of`
  at the source.

#### chain
- [x] **parity:** (Done.) Python's batches send through `chain.rpc("eth_sendRawTransaction")`
  (batches.py:235, 384) ↔ the Worker's `chain.sendRaw` (chain.ts:224) → `send_raw` on Python's
  Chain, used there. Its `send` (send and wait: tools, tests) stays.
- [ ] **parity:** `lasting_fees(…, priority_fee, …)` (chain.py:211) ↔ `lastingFees(…, priority,
  …)` (chain.ts:232) → `priority_fee`.

#### datafee, transactions, forks
- [ ] **parity:** `DataFee.fee(size, gas)` (datafee.py:89; called transactions.py:48, 109, 166)
  ↔ `beyondGas` (datafee.ts:73) → `beyond_gas`.
- [ ] **parity:** `_authorization` (transactions.py:216) ↔ `authorizationFields` (.ts:250);
  `_signed(…, beyond_gas)` ↔ `signed(…, beyond)` → one name each.
- [ ] **parity:** `GasSchedule.authorizations` (forks.py:93) ↔ `authorizationsGas`
  (forks.ts:83) → `authorizations_gas`.

#### jsonrpc, builders
- [ ] **parity:** `JsonRpcError.message` (jsonrpc.py:42) ↔ `nodeMessage` (.ts:34) →
  `node_message`; Python's keeps `method` too, as the Worker's (.ts:32).
- [ ] **parity:** `Answer.value()` (jsonrpc.py:71) ↔ `resultOf(answer)` (.ts:67) → `result_of`.
- [x] **parity, confusion:** (Done: Python's `BuilderError`, an HTTP status too, as the Worker's.) a builder's error answer raises `JsonRpcError("eth_sendBundle", …)`
  in Python (builders.py:62), "the node answered" ↔ `BuilderError` (builders.ts:24) →
  `BuilderError` in Python; `send_bundle` taking the `Builder`, as the Worker's.

#### maintenance, tracked, evm, config
- [x] **parity (low):** (Done.) `AccountState.exists` (maintenance.py:39) ↔ rebuilt inline
  (batches.ts:181) → `exists` in maintenance.ts.
- [ ] **confusion (low):** `Tracked.user_operation` holds the op's hash (tracked.py:64,
  tracked.ts:43) → `user_op_hash`; the Worker's stored JSON key stays `userOperation`
  (tracked.ts:88, 121, 181 map it explicitly), so storage doesn't change.
- [ ] **parity (medium):** `EncodedCall.data` is `HexStr` in Python (evm.py:26), bytes in TS →
  `bytes`; every Python site builds `HexStr("0x" + …)`.
- [ ] **parity:** `Token` (config.py:103) ↔ `TokenConfig` (config.ts:44) → `TokenConfig`.

#### userops
- [ ] **parity:** `FeePayment.at(base_fee)` (userops.py:162), what it pays ↔ `paidAt`
  (userops.ts:191) → `paid_at`.
- [ ] **parity (low):** `FeePayment.price`, `UserOperation.gas_price` (userops.py:154, 272) ↔
  `priceOf` (userops.ts:181), a price a gas → `gas_price`.
- [ ] **parity (low):** `GasEstimate.gas` (userops.py:318) ↔ `gasOf` (userops.ts:206) → one
  name.
- [ ] **confusion:** `gas_estimate()` returns an `eth_estimateGas` Call (userops.py:355,
  userops.ts:341), beside the type `GasEstimate` → `estimate_call`.

#### router
- [ ] **parity (low):** `_legs` (router.py:75) ↔ `legsOf` (router.ts:101) → `legs`.

#### server
- [x] **parity:** `X402Params` (wire.py:696) ↔ `X402Call` (wire.ts:107; its decoder is
  `x402Params`) → `X402Params`; `challenge`'s `params` (x402.py:140) ↔ `call` (x402.ts:146).
- [ ] **confusion (low):** `take` (x402.py:200, x402.ts:269) builds the paid Submission →
  `paid_submission`.
- [ ] **confusion:** two TS `transferOf`s, wire.ts:252 (TransferJson) and x402.ts:254
  (PaymentPayload) → x402's `paymentTransfer`.
- [x] **parity:** `status` (operations.py:146) ↔ `statusOf` (operations.ts:144) → `status_of`.
- [x] **parity:** `InvalidParamsError` (operations.py:77) ↔ `InvalidParams` (wire.ts:64) →
  `InvalidParamsError`.
- [ ] **confusion:** Python's `wire.data`, `wire.quantity` (wire.py:93, 89) encode; the Worker's
  private `data` (wire.ts:433) decodes → Python `hex_data`, `hex_quantity`; TS `dataOf`.
- [ ] **parity (low):** `UserOperationReceiptJson` (wire.py:420), a response among `*Result`s;
  `UserOperationParams.operation()` (wire.py:362) ↔ `UserOperationJson.user_operation()`, one
  conversion; `SendParams` (wire.py:521), decoded as "submission" everywhere else.
- [x] **parity:** `IndexResult` (wire.py:186) ↔ `Index` (front.ts:109) → `IndexResult`.
- [ ] **parity:** `ChainShown.status`, `snapshot` (front.py:57-58) ↔ `status`, `treasury`
  (front.ts:100-101), and `accounts` → `accounts_status`, `treasury`, `concurrency_accounts`,
  the ChainEntry fields each becomes.
- [ ] **parity:** `account_rows` (front.py:161) ↔ `accountsOf` (front.ts:262) →
  `account_rows`.
- [ ] **confusion (low):** TS `openapi(origin, x402)` (front.ts:142): `x402` a minimum fee,
  `undefined` and `null` meaning different things → `minFee`, the no-x402 case explicit.
- [x] **parity, a small fix:** (Done, a test seen failing first.) `MAX_BODY_BYTES` (app.py:135) ↔ `MAX_BODY_CHARS` (app.ts:46): the
  Worker checks `content-length` in bytes, then `text.length` in UTF-16 units, so a body sent
  without one can be up to three times the limit in bytes → bytes on both.
- [ ] **parity (low):** TS `MethodNotFound` (rpc.ts:135) → `MethodNotFoundError`; TS's `STATUS`
  regex (app.ts:61) → `SUBMISSION`; `_user_operation` (rpc.py:150) ↔ `userOperationOf`
  (rpc.ts:80).

### C. Structural

- [x] **Unit types.** (Done for the relay's clock, config durations and where block time meets the clock; teeswap's model, below. Chain time and tooling deferred.) Time is a bare number on both sides, its unit only in names (`_seconds`,
  `_ms`), and the two relays differ: `Measurements.at`, `Finished.finished_at` are monotonic
  seconds in Python and `Date.now()` ms in TS. → `Seconds` and `Millis`, neither passable as
  the other without converting:
  - Python: a value class, not a `float` subclass (decided 2026-11-03): a subclass can't
    refuse `Seconds + Millis` or `Seconds < Millis` (Liskov: ty rejects narrowing
    `float.__add__`; overloads only fall back to `float`). `Seconds` holds its `value`, defines
    the operators used, and mixing units is a type error and a runtime TypeError; `.value`
    where time leaves our code.
  - TS: branded `Seconds` and `Millis` (as `Address` is, evm.ts), with `toMillis` and
    `toSeconds`; `Date.now()` kept, typed once where it's read.
- [x] **batches: an item's failure.** (Done: Python's `_item(what)`, a context manager, ruff's BLE001 wanting the traceback logged where it's caught.) Python repeats the `except _ITEM_ERRORS` / `except
  Exception` + `internal_error` pair four times (batches.py) ↔ the Worker's `itemFailed`
  (batches.ts:109) → a helper in Python, named alike.
- [x] **batches: `_send`** (Done.) (batches.py:312) ↔ inline in the Worker's `onBlock` → `send` on both.
- [x] **batches: `Run`.** (Done.) Python's `_still_simulate(tracked, sims)` takes two parallel lists ↔
  the Worker's `stillSimulate(runs: Run[])` (batches.ts:74) → one shape.
- [x] **batches: `_failed` / `plannedOrFailed`.** (Done: `_planned_or_failed`.) Python's `_covered` unwraps the plan, then
  `_failed` rejects (batches.py:448) ↔ the Worker's `plannedOrFailed` does both
  (batches.ts:472) → one shape.
- [x] **batches: `Slot` (low).** (Done.) Python's `_Slot` holds the Tracked, the Worker's an id (it's
  stored), its type inline (batches.ts:57) → named in TS.
- [ ] **plan: `Planned`.** Python's `type Planned = Plan | Exception` ↔ `PromiseSettledResult<Plan>`
  written six times → `Planned` in TS.
- [ ] **plan: `Draft`'s layout.** The Worker's `Draft` carries `erc20`, `payeeHolds`
  (plan.ts:98-99); Python's `_Draft` doesn't, passing them through `_planned`, `_estimate`,
  `_estimated` → one layout.
- [ ] **plan: `_count_empty(answers)`** slices (plan.py:343) ↔ `countEmpty(answers, first,
  count)` indexes (plan.ts:437) → one signature.
- [ ] **x402: `PaymentPayload`.** Python keeps x402's nesting (wire.py:802); the Worker flattens
  `accepted.extra` into `accepted` (wire.ts:115) → x402's nesting on both.
- [ ] **x402: inline in TS:** `X402Params.submission()`, `_requirements()` (x402.py),
  `result()` (x402.py:402, inline in app.ts) → named where the two would otherwise drift.
- [x] **operations: the `id` check.** Python checks the hash in rpc.py:195 (REST by its path
  pattern) ↔ the Worker in `statusOf` (operations.ts:148) → in `status_of` on both.
- [ ] **Homes.** The root's entry types (HoldingEntry … ChainEntry): wire.py:97-186 ↔
  front.ts:26-72 → front. `Hop`, `PoolKind`: config.py:77, 83 ↔ prices.ts:14, 16 → config.
- [ ] **front: `x402MinFee`** (front.ts:104) ↔ read inline in app.py → one shape.
- [ ] **bundle: `BundleParams`.** Python's `bundle_params`, `standalone_params`,
  `atomic_params`, `bundles` return dicts (bundle.py:89-125) ↔ the Worker's `BundleParams`
  (bundle.ts:25) → the type in Python.
- [ ] **maintenance (low): Python-only `top_ups`, `replenish`, `sweep`** reached only by the
  tests and live tooling → to bruno/tests.

### D. Found along the way

- [x] **Bug, both relays:** a malformed PAYMENT-SIGNATURE's message was prefixed twice. The
  parser now gives only why; the caller names the header. No message test (tests assert codes
  and data, the field here, which was right all along).
- [x] **Dead:** `Latest.base_fee_per_gas`, filled and never read, gone from both.
- [x] **`INVALID_PAYLOAD`**, x402 v2's §9 code, now used: a malformed PAYMENT-SIGNATURE's 400
  (x402's HTTP transport: "Invalid Payment, 400") carries a PAYMENT-RESPONSE with
  `errorReason: invalid_payload` and no payer, as every other refusal carries its code. Read
  from coinbase/x402's specs at 0e6e8c5. SPEC.md says so; tested on both, seen failing first.
- [x] **Stale docs, TS:** stacked comments resolved (`status()` given its own back; the rest
  older copies, removed); `refusal`'s and `simulations`' now say what Python's do; the orphan
  above `NATIVE_ASSET` gone (chain's `hasCode` has its own).
- [x] **Stale docs, Python:** `Payer`'s names `deployment`.

### E. Decisions (the user's, 2026-11-01)

- [x] The config keys `"bundlers"` and `"price"` stay: no config key changes.
- [x] `min_fee`, `min_fee_per_gas` stay: `min_fee_per_gas` is the relay's fee's floor a gas, as
  named.
- [x] The treasury's use of the token's `margin_multiplier` as the pools' cut is the design: the
  token's margin is what converting it costs (DEPLOYMENT.md, design/treasury.md); the chain's is
  the markup. The review read the two as one.
- [x] `postpaid_config` and the API stay: no change to the contract with clients.

### Reviewed, left as they are

Fine in their context, or not worth the churn:

- Dropped in the re-check of 2026-11-02, the code read: `_pending`/`pendingById` (private, in
  `Queue` now, `pending()` on both); `supported`, `fits`, `within_limits` raising (the same names
  on both, always called as statements); `min_usd` (it fills x402scan's price, declared
  `"currency": "USD"`: the name says what the document claims; whether `min_fee`'s token is a
  dollar is the document's assumption, USDC on both networks); the runner's `_call` and
  `clientCall` (each beside its assembler method, in its own context); `on_method` /
  `methodNotAllowed` (a Litestar exception handler, answering an unserved chain too, against a
  405 builder); `shown` / `front()` (the Worker's is Durable Object plumbing); **`BrunoError`**
  (`ChainError` and `JsonRpcError` are deliberately not BrunoErrors, their docstrings say why,
  and the Worker tells errors apart by class); **chain's Python-only methods** (`balance`,
  `code`, `head`, `receipt`, `send`: `make preflight`'s and the deployer's, operator tooling the
  Worker hasn't). Done already: `_quote`/`quoteAt` (`quote_at` on both, the pricing split).

- `_our_fee` → `relay_fee`: "our" says whose.
- `Erc20.transfer_gas` → `measured_transfer_gas`: `eip3009.transfer_gas(measured_gas, …)`'s
  parameter says which.
- `SignedTransaction.gas` → `gas_limit`: `gas` is the transaction field's own name.
- The runner's `_deploy` → `deploy_account`, `served`, `latest`/`head`, `$status`: each clear in
  its module.
- `record_payee` → `record_payee_holdings`, `operation` → `id_of_operation`, `_Signing` →
  `PayerCost`, `relay.domains` → `x402_domains`, `Plan.size`/`Tracked.size` → `unsigned_size`,
  `Draft.payeeHolds`: longer without being clearer.
- `DataFee.l1_fee` → `l1_fee_bound`, `DataFee` → `OpStackFees`, `_MAX_L1_FEE_RISE_*` as a
  Fraction, `tx_authorization`, `BALANCES` → `BALANCES_HELPER`, `EntryPoint.eip712` → `v0_8`,
  `X402_DESCRIPTION`, `refusal` → `refusal_reason`, a PaymentPayload variable `payment`, `send`'s
  local `accepted`, `UserOperationSent` → `…Result`, the `*_PATTERN` constants, TS's `handle` →
  `serveChain`, the treasury's stored `gas`/`sizes` names: small or local.
- Language-forced, notes only: `Submission.id` ↔ `submissionId()`; `_prune`, `_forget_at` public
  in TS (the Durable Object calls them); `RPC_TIMEOUT_SECONDS` ↔ `RPC_TIMEOUT_MS` (until the
  unit types); TS's `bigIntOf`, `bytesOf`; `Unavailability.category` ↔
  `unavailabilityCategory()`; Python-only helpers TS writes inline (`fee_payment`, `at_a_wei`,
  `_call_access`, `_hops`, `_steps`).

## 2. Splits (last)

- [x] `relay`'s pricing → `pricing.py`, `pricing.ts` (288/327 lines): `Pricing` (built from the
  chain, config, `Prices` and payee, holding the margins; `signing`, `quote_at`, `fits`,
  `within_limits`, `supported`, `tip`, `fees`, `charge`, `price`), `fee_ceiling`, `MAX_AMOUNT`,
  `TokenQuote`, `Quotes`, `Payer`, `Unsupported`, `UnsupportedError`. `Relay` holds it
  (`relay.pricing`) and keeps `quote`, `x402_quote` as entry points. `UnavailableError` and
  `schedule_of` → measurements (both relay and pricing raise it). Moves only, no renames;
  relay 1050/1093 lines.
- [x] `relay`'s queue → `Queue` in tracked.py, tracked.ts (A005 forbids a `queue` module): the
  submissions taken, pending and finished, the UserOps among them, the held requests, one
  taken at a time; in the Worker their `submission:*` keys. `Relay` holds it (`relay.queue`);
  `UnknownSubmissionError` with it. Moves only; relay 971/950 lines.
- [x] `relay`'s measuring → `Measuring` in measurements.py, measurements.ts: the measurements,
  `latest`, problems, internal errors and the accounts as last read (batches record them); in
  the Worker its `problems`, `internalErrors` and `accounts` keys. `Relay` holds it
  (`relay.measuring`). The Worker's `itemFailed` stays in relay.ts until batches (it needs
  relay's item errors). Moves only; relay 885/826 lines.
- [x] `relay`'s batches and blocks → `Batches` in batches.py, batches.ts: owns the batch and the
  funding in flight (the Worker's `batch`, `funding` keys), and holds the queue, pricing and
  measuring. `profit` → `Pricing`; `payee_tokens`, the item errors and the Worker's
  `itemFailed` with it; `RelayError` out of the item errors (nothing in a batch raises one).
  The Worker's `Store` type → store.ts (four modules took it from relay.ts). Relay 495/353
  lines, batches 464/511.

- [ ] `userops` (720/695): op, hash and estimate; send and receipt.
- `wire.py` (784) stays: type definitions, responses included (the Worker builds them inline).

## Also open

- **Docs:** a page, *An EOA, with permissionless.js*, in the accounts overview's table and the
  site's navigation (so in llms.txt), and `support.md`.
