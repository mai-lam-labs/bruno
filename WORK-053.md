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

3. [ ] A node answering badly. Decided 2026-11-01, one rule on both relays:
   - **The client's mistake:** refused (400/422); in the loop, that submission rejected.
   - **A node's failure, a malformed answer included:** never the client's fault, never a
     rejection. Every node value through strict parsers raising ChainError (quantity, data,
     address, ABI words of checked length). HTTP 503 `node-failed`; in the loop the submission
     sits out the step, the rest proceed.
   - **Our bug (anything else):** isolated per item in the loops, never crashing the batch, and
     reported: logged as an error and listed in the root's problems. HTTP 500. Measuring backs
     off a minute on any failure, keeping old measurements only after a node's.
   Steps, each with failure-path tests on both (a Faults node answering null, short words,
   non-hex):
   - [x] 3.1 The treasury's reads (next to our own funds): the Worker's bare `BigInt`
     (`""` read 0, decimal accepted), Python's `int(x, 16)`. Strict parsers in `chain` on both:
     `quantity` (the Worker's now strict everywhere), `word_bytes`/`wordBytes`,
     `word_of`/`wordOf`, `address_of`/`addressOf` (checks 32 bytes).
     `test_a_malformed_balance_is_the_nodes_failure`, seen failing first on both (P ValueError,
     W "nothing sent"). Harness: `Faults.garbled`, `served.treasury_refused()`.
   - [x] 3.2 Strict parsers, and the silent misreads, on both, in `chain`: `hex_of`/`hexOf`
     (whole bytes), `data_of`/`dataOf`, `code_of`/`codeOf` ("0x0" is none), `has_code` moved
     from `evm` (Python's took None for code), `object_of`/`list_of` for blocks and fee
     histories, `revert_data`, `Receipt` and `receipt_of` (a numeric status: "0x01" read as not
     landed in 5 places). Every node read through them. `datafee.read`/`agrees` take parsed
     values (Python: chain imports datafee). ABI reads of node answers checked: Python's eth_abi
     DecodingError is ChainError (`userops._decoding`), the Worker's words check their length
     (a short answer read 0: "pays nothing" 400s, silently wrong estimates). Tests, each seen
     failing first: a status "0x01" (both left it pending), eth_getCode null (P 500), a short
     simulation (P 500, W estimated from zeros).
   - [x] 3.3 Never blaming the client (done with 3.2): the Worker's `_delegate` reads code by
     `codeOf`, so a malformed answer is the node's failure, only real code delegating nowhere the
     client's; the
     receipt (done in 3.2: its logs a ChainError on both, the Worker's non-object no longer
     null); `simulations` rejecting a non-word result (done in 3.2: only "0x" is a rejection).
   - [ ] 3.4 Per-item isolation: planning's reads and estimates (P fails make_plans, W
     rethrows non-ChainError in itemFailed), settling receipts (P aborts the step), revert
     data, pool reads (short answer escapes PriceError on both); x402's `_magic` (done in 3.2,
     inside its try); a bug isolated and reported. Measuring: P skips its backoff on an unexpected error,
     W keeps old measurements for anything.
   - Intended: the treasury retries in 5 min (P) and at the next hourly cron (W).

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
