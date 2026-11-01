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

#### relay
- [ ] **parity:** `_quote` (relay.py:588) ↔ `quoteAt` (relay.ts:594) → `quote_at`.
- [ ] **parity:** `authority` (relay.py:451, 471, 594) ↔ `authorityAddress` (relay.ts:443, 470,
  599) → `authority`.
- [ ] **parity:** `_failed` (relay.py:1180) ↔ `plannedOrFailed` (relay.ts:1254): the same check,
  Python unwrapping the plan in `_covered` → one name and one shape (name open: `planned`?).
- [ ] **parity:** `Accepted(id, new)` (relay.py:251) ↔ inline `{id, isNew}` (relay.ts:656) →
  `Accepted` named in TS, `is_new` on both.
- [ ] **parity:** `_pending` (relay.py:315) ↔ `pendingById` + `pending()` (relay.ts:251, 1084) →
  `pending` the map on both.
- [ ] **confusion:** `supported`, `fits`, `_within_limits` (relay.py:1215, 1195, 1206 ↔
  relay.ts:1309, 1279, 1297) read as predicates and raise → `check_supported`, `check_fits`,
  `check_limits`, as `check_authorizations`.

#### plan
- [ ] **parity:** `_revert` (plan.py:405) ↔ `revertOf` (plan.ts:402) → `revert_of`.
- [ ] **parity, confusion:** TS's `settled<T>` (plan.ts:420), a promise unwrapped, inline in
  Python's `make_plan` (plan.py:71) → inline on both, or one helper not called "settled".
- [ ] **confusion:** `empty(chain, authorities)` (plan.py:326, plan.ts:348) returns a count,
  beside the field `Draft.empty` → `count_empty`.

#### submission
- [ ] **parity:** `authorities` (submission.py:125, submission.ts:104) is imported as
  `authorities_of` by both plans → `authorities_of` at the source.

#### chain
- [ ] **parity, confusion:** `Chain.send` (chain.py:389) sends and waits for the receipt ↔
  `sendRaw` (chain.ts:226) sends only; Python's relay calls `rpc("eth_sendRawTransaction")`
  itself (relay.py:955, 1116) → `send_raw` on both, used by Python's relay; the waiting one per
  C (chain, Python-only methods).
- [ ] **parity:** `lasting_fees(…, priority_fee, …)` (chain.py:212) ↔ `lastingFees(…, priority,
  …)` (chain.ts:234) → `priority_fee`.

#### datafee
- [ ] **parity:** `DataFee.fee` (datafee.py:89) ↔ `beyondGas` (datafee.ts:73) → `beyond_gas`
  (as `SignedTransaction.beyond_gas`).

#### transactions
- [ ] **parity:** `_authorization` (transactions.py:216) ↔ `authorizationFields` (.ts:250) →
  `authorization_fields`.
- [ ] **parity:** `_signed(…, beyond_gas)` (:182) ↔ `signed(…, beyond)` (.ts:195) →
  `beyond_gas`.

#### forks
- [ ] **parity:** `GasSchedule.authorizations` (forks.py:82) ↔ `authorizationsGas`
  (forks.ts:76) → `authorizations_gas` (Python's `intrinsic()` has a count parameter of the
  same name).

#### jsonrpc
- [ ] **parity:** `JsonRpcError.message` (jsonrpc.py:42) ↔ `nodeMessage` (.ts:34) →
  `node_message` (TS can't use `message`).
- [ ] **parity:** TS's `JsonRpcError` keeps `method` (.ts:33), Python's doesn't → kept on both.
- [ ] **parity:** `Answer.value()` (:71) ↔ `resultOf(answer)` (.ts:67) → `result_of`.

#### builders
- [ ] **parity, confusion:** a builder's error answer raises `JsonRpcError("eth_sendBundle", …)`
  in Python (builders.py:62), whose docstring says a node answered ↔ `BuilderError`
  (builders.ts:24) → `BuilderError` on both.
- [ ] **parity:** `send_bundle(client, url, params, identity)` (:42) ↔ `sendBundle(builder,
  params)` (.ts:40) → Python takes the `Builder`.

#### maintenance
- [ ] **parity:** `AccountState.exists` (maintenance.py:39) ↔ rebuilt inline in TS
  (relay.ts:914-919) → `exists` in maintenance.ts.

#### tracked
- [ ] **confusion:** `Tracked.user_operation` (tracked.py:62, .ts:42) holds the op's hash →
  `user_op_hash`.

#### evm
- [ ] **parity:** `EncodedCall.data` is `HexStr` in Python (evm.py:26), bytes in TS (.ts:20) →
  `bytes`.

#### config
- [ ] **parity:** `Token` (config.py:103) ↔ `TokenConfig` (config.ts:44) → `TokenConfig`.

#### userops
- [ ] **parity:** `FeePayment.price` (userops.py:152) and `UserOperation.gas_price` (:270) ↔
  `priceOf` (userops.ts:180) → `gas_price` (EntryPoint's `getUserOpGasPrice`).
- [ ] **parity:** `FeePayment.at` (:160) ↔ `paidAt` (.ts:190) → `paid_at`.
- [ ] **parity:** `GasEstimate.gas` (:316) ↔ `gasOf` (.ts:205) → `total`, as `GasUsed.total`.
- [ ] **confusion:** `gas_estimate` (userops.py:353, .ts:340) returns an `eth_estimateGas` Call,
  beside the type `GasEstimate` → `estimate_call`.

#### runner
- [ ] **parity, confusion:** `_call` (runner.py:248) ↔ `clientCall` (runner.ts:239), which TS's
  `Assembly.clientCall` (assembler.ts:210) also is → `call`.

#### eip3009
- [x] ~~**parity:** `TransferAuthorization.deployment` (eip3009.py:72) ↔ `transferDeployment`
  (eip3009.ts:79) → `deployment`.~~ Left: a method in Python, a free function in TS, where
  the owner prefix is the Worker's convention (A, encoded).

#### router
- [ ] **parity:** `_legs` (router.py:75) ↔ `legsOf` (router.ts:101) → `legs`.

#### server/x402
- [ ] **parity:** `X402Params` (wire.py:695) ↔ `X402Call` (wire.ts:89) → `X402Params` (TS's
  decoder is already `x402Params()`).
- [ ] **confusion:** `take` (x402.py:196 ↔ x402.ts:267) builds the paid Submission; taking a
  submission is the relay's → `paid_submission`.
- [ ] **confusion:** `min_usd` (x402.py:418 ↔ x402.ts:366) is `min_fee.amount`, in any token →
  `min_fee`.
- [ ] **parity:** `challenge`'s `params` (x402.py:138) ↔ `call` (x402.ts:144) → `params`.
- [x] **parity:** `settled`'s `status_url` (x402.py:295) ↔ `url` (x402.ts:337) → `status_url`. (Done.)
- [ ] **confusion:** two TS `transferOf`s, wire.ts:234 (TransferJson) and x402.ts:252
  (PaymentPayload) → x402's `paymentTransfer`, beside `paymentPayload` in wire.ts.

#### server/operations
- [ ] **parity:** `status` (operations.py:152) ↔ `statusOf` (operations.ts:340) → `status_of`.
- [ ] **parity:** `InvalidParamsError` (operations.py:83) ↔ `InvalidParams` (wire.ts:64) →
  `InvalidParamsError`.

#### server/wire
- [ ] **confusion:** Python's `wire.data`, `wire.quantity` (wire.py:88, 92) encode; TS wire's
  `data` (wire.ts:415) decodes; TS encodes with `evm.hex`, `hexQuantity` → Python `hex_data`,
  `hex_quantity`; TS's decoder `dataOf`, as `quantityOf`.
- [ ] **parity:** `UserOperationReceiptJson` (wire.py:419), a response among `*Result`s →
  `UserOperationReceiptResult`.
- [ ] **parity:** `UserOperationParams.operation()` (wire.py:361) ↔
  `UserOperationJson.user_operation()` (wire.py:300), one conversion → `user_operation()`.
- [ ] **parity:** `SendParams` (wire.py:520), decoded as "submission" everywhere else →
  `SubmissionParams`.

#### server/front
- [ ] **parity:** `IndexResult` (wire.py:185) ↔ `Index` (front.ts:109) → `IndexResult`.
- [ ] **parity:** `ChainShown.status`, `snapshot` (front.py:57-58) ↔ `status`, `treasury`
  (front.ts:100-101), and `ChainShown.accounts` → `accounts_status`, `treasury`,
  `concurrency_accounts`: the ChainEntry field each becomes (wire.py:176-179).
- [ ] **parity:** `account_rows` (front.py:161) ↔ `accountsOf` (front.ts:262) →
  `account_rows`.
- [ ] **parity:** `shown` (app.py:266) ↔ `ChainRelay.front()` (index.ts:659) → `shown`.
- [ ] **confusion:** TS `openapi(origin, x402)` (front.ts:142): `x402` is a minimum fee, with
  `undefined` and `null` meaning different things → `minFee`, the no-x402 case explicit.

#### server/app
- [ ] **parity:** `MAX_BODY_BYTES` (app.py:133) ↔ `MAX_BODY_CHARS` (app.ts:44), TS counting
  UTF-16 units → `MAX_BODY_BYTES`, counted in bytes.
- [ ] **parity:** `on_method` (app.py:294) ↔ `methodNotAllowed` (app.ts:89) →
  `method_not_allowed`.
- [ ] **parity:** TS `MethodNotFound` (rpc.ts:541) → `MethodNotFoundError`, as every error.
- [ ] **confusion:** TS's `STATUS` regex (app.ts:58) is the submission's path → `SUBMISSION`,
  as `RECEIPT`.

#### server/rpc
- [ ] **parity:** `_user_operation(params)` (rpc.py:150) ↔ `userOperationOf(params)`
  (rpc.ts:486) → `user_operation_of`.

### C. Structural

- [ ] **Unit types.** Time is a bare number on both sides, its unit only in names (`_seconds`,
  `_ms`, `MEASURE_RETRY_SECONDS` ↔ `MEASURE_RETRY_MS`), and the two relays differ:
  `Measurements.at`, `Finished.finished_at` are monotonic seconds in Python
  (measurements.py:93) and `Date.now()` ms in TS (.ts:68). → `Seconds` and `Millis`, neither
  passable as the other without converting:
  - Python: `class Seconds(float)` with `.to_millis() -> Millis`, `class Millis(int)` with
    `.to_seconds() -> Seconds` (not NewType); `__add__`, `__sub__` and the rest that's used
    overridden to return the same unit (a float or int subclass otherwise returns a plain
    float or int).
  - TS: branded `Seconds` and `Millis` (as `Address` is, evm.ts:12), with `toMillis` and
    `toSeconds`; `Date.now()` kept, typed once where it's read.
  - Home: a module of its own on both sides, or common (below). Then config's `*_seconds`
    fields, `Measurements.at`, the clocks and timeouts, a slice at a time.
- [ ] **relay: `Run`.** Python's `_still_simulate(tracked, sims)` (relay.py:1118), two parallel
  lists ↔ TS's `stillSimulate(runs: Run[])`, `Run {tracked, plan, sender}` (relay.ts:223); Python's
  `_run` is the clock loop → one shape on both (a `Candidate` if a type).
- [ ] **relay: an item's failure.** Python repeats `except _ITEM_ERRORS` / `except Exception` +
  `_internal_error` five times (relay.py:893, 1017, 1078, 1156, 1167) ↔ TS's `itemFailed`
  (relay.ts:358) → a helper in Python, named alike.
- [ ] **relay: `_send`** (relay.py:1032), inline in TS's `onBlock` (relay.ts:1044-1055) → `send`
  on both.
- [x] **relay: `TokenQuote`, `_Slot`** (TokenQuote named in pricing.ts; `_Slot` still open) (relay.py:210, 259) ↔ inline `{token, amount}`, `{id,
  account}` (relay.ts:201, 206) → named in TS.
- [ ] **plan: `Planned`.** Python's `type Planned = Plan | Exception` (plan.py:78) ↔
  `PromiseSettledResult<Plan>` written six times → `Planned` in TS.
- [ ] **plan: `Draft`'s layout.** TS's `Draft` carries `erc20`, `payeeHolds` (plan.ts:98-99);
  Python passes `tokens`, `payee_holds` through `_planned`, `_estimate`, `_estimated`
  (plan.py:163, 242, 281); the suspended-token check is in `_planned` (plan.py:173) ↔
  `makePlans` (plan.ts:160) → one layout.
- [ ] **plan: `_count_empty`** slices (plan.py:343) ↔ `countEmpty(answers, first, count)`
  indexes (plan.ts:436) → one signature.
- [ ] **x402: `PaymentPayload`.** Python keeps x402's nesting (wire.py:801), TS flattens it
  (wire.ts:97) → x402's nesting on both.
- [x] **x402: `challenge`'s `least`** (Done; and `statusExtension` named in TS.), a bare tuple in Python (x402.py:140) ↔ `{token, amount}`
  (x402.ts:146) → `TokenQuote` on both.
- [ ] **x402: inline in TS:** `X402Params.submission()` (wire.py:709 ↔ x402.ts:315),
  `status_extension()` (x402.py:329), `_requirements()` (x402.py:175); `result()` (x402.py:402)
  inline in TS's app.ts:242 → named on both where the two would otherwise drift.
- [x] **operations: `NotServedError`** (operations.py:91, data `NotServed`, wire.py:612) ↔ TS's
  `notServed()` Response (app.ts:79) → `NotServedError`, `NotServed` in TS, mapped in
  `errorObject`.
- [x] **operations: node failures.** Python's `node_failed()` (operations.py:65-76) ↔ inline in
  TS's `errorObject` (operations.ts:410-417) → `nodeFailed` in TS.
- [ ] **operations: the `id` check.** Python checks the hash in rpc.py:195 ↔ TS in `statusOf`
  (operations.ts:344) → in `status_of` on both.
- [x] **wire: `invalid(code, field, why)`** (wire.py:649) ↔ hand-built in app.ts:186, rpc.ts:443
  → exported from wire.ts.
- [ ] **Homes.** (`ErrorObject` moved to wire.ts, with `invalid`.) `ErrorObject`: wire.py:604 ↔ operations.ts:230 → wire. The root's entry types:
  wire.py:96-183 ↔ front.ts:26-72 → front. `Hop`, `PoolKind`: config.py ↔ prices.ts → config.
- [ ] **front: `x402MinFee`** (front.ts:104) ↔ read inline in app.py:611; TS's `leastFee`
  (index.ts:390) inline in Python → one shape.
- [ ] **Errors: `BrunoError`.** No TS counterpart; Python's `ChainError` (chain.py:39),
  `JsonRpcError`, `AnswerError` (jsonrpc.py:35, 46) extend `Exception`, not `BrunoError` →
  `BrunoError` in TS (common.ts, with measurements.ts's generic `lookup`), Python's three under
  it.
- [x] **Bare tuples (CLAUDE.md).** (Done: `Intrinsic`, `Assembled`, the treasury's `PayeeCall`,
  `Approval`, and `Denomination`, on both.) `GasSchedule.intrinsic` (forks.py:61) ↔ `{standard, floor}`
  → a type; `funding_initcode` (bundle.py:52) ↔ `{code, gas}`, and `Assembly.assemble`'s; the
  treasury's payee calls and approvals (treasury.py:417, 283) ↔ `PayeeCall` (treasury.ts:80,
  whose doc says "and its gas" but has none); `survey`'s tokens (maintenance.py:99) ↔
  `{decimals, symbol}`.
- [ ] **bundle: `BundleParams`.** Python's `bundle_params`, `standalone_params`,
  `atomic_params`, `bundles` return dicts (bundle.py:93-125) ↔ TS's `BundleParams` (.ts:25) →
  the type in Python.
- [ ] **chain: Python-only methods** no relay path uses: `balance`, `code`, `head`, `base_fee`,
  `next_base_fee` (no caller), `fees`, `receipt`, `send` (chain.py:318-389) → to the test
  tooling; TS's bundler.ts:156 reads a receipt through `rpc` where Python's bundler uses
  `chain.receipt`: one way on both.
- [ ] **maintenance: Python-only `top_ups`, `replenish`, `sweep`, `MaintenanceError`**
  (maintenance.py:135-205), only tests and live runs → to the test tooling.

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
- [ ] `Batches` holding the queue, pricing, measuring and the rest it uses: the split by owned
  state, as designed with the user 2026-11-01.
- [ ] `relay`'s batches and blocks (`close_batch`, `on_block`, sending, settling, funding,
  re-simulation): tangled with `Relay`'s state, its own design first. `relay` keeps
  submissions, status, measurements, clock and Store.
- [ ] `userops` (720/695): op, hash and estimate; send and receipt.
- `wire.py` (784) stays: type definitions, responses included (the Worker builds them inline).

## Also open

- **Docs:** a page, *An EOA, with permissionless.js*, in the accounts overview's table and the
  site's navigation (so in llms.txt), and `support.md`.
