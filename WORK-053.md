# 0.5.3 code organisation (temporary, delete before merging)

Python/Worker parity and layout, from the drift review of 2026-11-01. Neither side is the default.

## 1. Moves (no behaviour change)

- [x] `bruno/net/jsonrpc.py` → `bruno/jsonrpc.py`; drop `bruno/net/`.
- [x] The Worker's JSON-RPC part of `chain.ts` → `worker/src/jsonrpc.ts`, shaped as Python's:
  `call`/`batch` per node, `AnswerError` (was `ChainError`), `StatusError`; `chain.ts`'s
  `failure()` names a node's failure as `chain.py`'s does ("not JSON", not "SyntaxError").
- [x] `bruno/entry_point_simulations_0_{7,8}.hex` → one `bruno/userops.json` in the Worker's shape
  (`{"entryPointSimulations": {"0.7": …, "0.8": …}}`), read by Python; the Worker's copy
  generated from it by `make vectors`.

## 2. Behaviour fixes (each on both sides, a failure-path test against both relays, seen to fail first)

1. [x] LOG0 in a receipt crashes Python's `eth_getUserOperationReceipt` (`userops.py`, `log.topics[0]`).
2. [x] Server failure answers. Decided 2026-11-01: clients rely on codes and `data`, never
   messages; tests assert code, status and `data`, not wording
   (`test_failures_are_answered_alike`, both relays).
   - [x] **Wire change:** invalid params' `data` is `{"field": <path>}` (was the bare field, or
     Python's `"params"` and Litestar's list), Invalid Request (-32600) the same. The field is
     named as SPEC.md's request column names it, over REST and JSON-RPC alike: an op's fields
     within it, a whole parameter by its OpenRPC name (`submission`, `quote`, `id`,
     `userOpHash`, `entryPoint`), a REST body that isn't an object `body`, a wrong count
     `params`. Python converts msgspec's and Litestar's validation errors in one place
     (`wire.validation_error`, `app._field`). SPEC.md and the user docs' examples updated.
   - [x] `"jsonrpc": "2.0"` required on both.
   - [x] JSON-RPC envelope: the Worker refuses a missing or non-string `method`, an object `id`.
   - [x] The chain checked first: 404 before parameters (a Litestar guard in Python).
   - [x] Malformed ids in the path: 400, `id` / `userOpHash`, on both.
   - [x] Wrong HTTP method: 405, `Allow`, the relay's error body, on both (the root's paths too).
   - [x] `maxFeePerGas: null` (and `maxPriorityFeePerGas`): invalid params on both.
   - [x] Ours branching on an error's message: none. `plan`'s geth "header not found" is a
     node's message (alike on both); `rpccheck.py` slices a ChainError's message for display.
   - Dropped (message only, same code and status): unknown chain wording, incomplete
     deployment, x402 on a chain without x402.
   - Moved to 3: non-hex node values (W 503, P 500).
   - Python's OpenAPI path parameters are `submission_id` / `op_hash`; SPEC.md says `{id}`,
     `{hash}`. Renaming hits lint (A002, N803); left, the field mapped to SPEC's names.
2b. [ ] Tests never assert our own messages (2026-11-01): a message only where a third-party spec
   fixes it (x402's errorReason; none of ours otherwise). What a test needed from a message
   moved into `data`:
   - [x] Bounds: invalid params' `data` gains `most` and `needs` (hex): gas, call bytes,
     verifyGas, authorizationList count, an op's gas and data, maxBlockNumber (`most` the latest
     block it may name). `Bound` in `submission` on both sides.
   - [x] Status `reason`: `Rejection` StrEnum / union, values unchanged, listed in SPEC.md.
   - [x] Tests rewritten to code, status and `data` (server, x402, signed payments, degraded,
     documents).
   - [x] -32012 / -32015 `data: {category, reason}` on both: `Category` (retry, request,
     operator) and `Unavailability` in `measurements`, `Unsupported` in `relay`; each reason's
     category fixed in code. `Measurements.unavailable` is `Unavailable(reason, why)`. SPEC.md
     lists them. Ruff's S105 ignored (it took a member named *TOKEN* for a password).
   - [x] Configuration errors: `ConfigError(reason, field, why)` on both, `Misconfiguration`
     (invalid-config, no-payee, unbound-key, invalid-key, not-configured). The Worker answers a
     misconfigured chain 500, -32603, `data: {reason, field}` (was 404 with the message); a
     chain not configured stays 404. Python's msgspec path reading moved to `common.Invalid`.
     msgspec has no constraints on Decimal, so checks in `__post_init__` (multipliers, min_fee's
     amount, runway_days, ETH's eip3009) name the object on both sides; `Node.max_batch` became
     a constraint (`rpc[0].max_batch`). SPEC.md and DEPLOYMENT.md updated.
   - Optional: per-entry authorization paths (`authorizationList[0].nonce`): check_signed
     doesn't know the entry; four callers a side.
   - Known gap: the client's `NoPayment.reasons` are strings; with its API.

3. [ ] A node answering badly:
   - [ ] planning: P fails the whole batch on an unexpected error (`plan.py` catch), W rejects one
     submission. Target: reject the one submission.
   - [ ] measuring: P catches node errors only (`relay.py`), W everything. Target: node errors only.
   - [ ] non-hex revert data: P raises, W reads empty.
   - [ ] non-hex node values generally: W 503 (ChainError), P 500.
4. [ ] Lookups: KeyError in P, defaults in W: a token's margin, its metadata, quote token lookups
   (W prices an unknown token as ETH).
5. [ ] Smaller:
   - [ ] `assembler.push` over 32 bytes: P emits a wrong opcode, W raises.
   - [ ] `max_pending: 0`: P `min([])` raises, W accepts over capacity.
   - [ ] config decimals: W stricter (no `1e-2`, no numbers).
   - [ ] builder `"error": null`: P success, W TypeError.
   - [x] "no node answered" for a batch: P listed its methods sorted, now in the calls' order as W.

Intended, noted: the Worker's treasury retries on its hourly cron (P in 2–5 min); Python's
treasury state is in memory (known gap).

## 3. Renames (a batch per module pair, pick the better name each time)

DataFee `fee`/`beyondGas`, `risen`/`risenDataFee`; JsonRpcError `message`/`nodeMessage`;
Answer `value()`/`resultOf()`; Chain `send`/`sendRaw`; `IndexResult`/`Index`; `status`/`statusOf`;
`InvalidParamsError`/`InvalidParams`; `X402Params`/`X402Call`; GasSchedule
`authorizations`/`authorizationsGas`; `config.Token`/`TokenConfig`; ChainShown
`snapshot`/`treasury`; and the rest of the review's ~30 pairs.

## 4. Splits (last)

- [ ] `relay` (1247/1329): pricing and quotes (`quote`, `x402_quote`, `charge`, `price`, fees,
  tip, `Payer`, `Quotes`); batches and blocks (`close_batch`, `on_block`, settling, funding);
  `relay` keeps submissions, status, measurements, clock and Store.
- [ ] `userops` (720/695): op, hash and estimate; send and receipt.
- `wire.py` (784) stays: type definitions, responses included (the Worker builds them inline).

## Also open

- -32601 is both "no such path" and "chain isn't served": give the latter
  `data: {"chain": …}`?

- `sepolia-verify`: cap the gas in ETH as well as the 5 USDC cap?
- CI on PR #16.
