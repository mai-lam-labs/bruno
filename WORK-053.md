# 0.5.3 code organisation (temporary, delete before merging)

Python/Worker parity and layout, from the drift review of 2026-11-01. Neither side is the default.

0.5.3 ends at 2d (the user, 2026-11-01). Sections 3 and 4, the renames and splits, move to 0.5.4,
as preparation for 0.6: carry them into WORK-054.md when this file is deleted.

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

3. [x] A node answering badly. Decided 2026-11-01, one rule on both relays:
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
   - [x] 3.4 Per-item isolation, on both: planning (P caught only the client's and node error
     answers), its estimates and `simulations` per answer (a malformed one the node's failure,
     that submission sitting out), ranking, signing, settling, covering. A bug of ours in one
     submission's work: logged with its traceback and listed in the root's problems (the last
     5: P `Relay._internal_errors`, W `internalErrors` stored), the rest going on. Measuring: a
     bug is reported and the chain not measured for a minute (P measured again on every
     request); old measurements kept only after a node's failure (W kept them for anything).
     Pool reads check their length (a short answer read "pool is empty"). Tests, seen failing
     first on P (W already isolated these): a malformed answer for one holding up only it, a
     short pool answer. A bug's path has no test: no input reaches one, and faking one means
     patching internals. ruff's BLE comment says what it now enforces.
   - Intended: the treasury retries in 5 min (P) and at the next hourly cron (W).

4. [x] Lookups (2026-11-01): every token lookup is reached only after `supported()` (configured,
   not suspended) and measuring drops a token that fails, so none can miss: a miss is a broken
   invariant, a bug of ours. Python's indexing raises (KeyError: loud, isolated and reported
   since 3.4); the Worker guessed (an unknown token priced as ETH, its `null` meaning ETH; a
   margin of 1; 18 decimals; its address for its symbol; 0 for a balance or allowance). The
   Worker now looks up strictly (`measurements.lookup`), throwing as Python does. Kept: the
   Worker's `sizes.get(token) ?? largest`, gas stored before sizes were (Python holds both in
   memory). No test: nothing reaches a miss but a planted invariant.
5. [x] Smaller (2026-11-01):
   - [x] `assembler.push` over 32 bytes: Python raises AssemblyError, as the Worker did (no
     test: internal, no input reaches it).
   - [x] `max_pending: 0`: refused by both configurations, so a full queue is never empty;
     the Worker's unreachable branch throws now, where it took the submission over capacity.
   - [x] Config decimals: Python took a JSON number, an exponent, "Infinity" (and crashed on
     "NaN"); the Worker trimmed spaces. Both now take exactly a decimal string
     (`config.DecimalText`, the Worker's `decimal` untrimmed). `test_a_decimal_is_written_exactly`,
     seen failing first.
   - [x] A builder's `"error": null` beside its result: the Worker threw reading it (a
     spurious failure logged; the bundle was taken), now no error, as Python. No test: nothing
     observable but a log line.
   - [x] "no node answered" for a batch: methods in the calls' order on both (done in step 1).

Intended, noted: the Worker's treasury retries on its hourly cron (P in 2–5 min); Python's
treasury state is in memory (known gap).

## 2c. Both are reference implementations (2026-11-01)

The user: the Python relay and the Worker are the same product, equals, no primary; structured
alike so they're easy to compare, and what one finds improves the other.

- [ ] Wording: 12 places call one side "the reference" or "the port" (comments, docs; some added
  in this PR, such as "as the reference's DecimalText"). Name the counterpart module instead.
- [x] CLAUDE.md's "Python and the Worker" section: both implementations, equals (the user agreed
  the rewrite).

## 2d. Duplication (scanned 2026-11-01, both relays; each fold on both sides, same home)

Ordered by value. Homes chosen so both relays keep each helper in the same module.

- [x] A guess left from item 4: both treasuries' `_margin`/`margin` fall back to `Fraction(1)` for
  a token not found (it can't be: the treasury converts only the measured, configured tokens).
  A margin of 1 reads a conversion as costing nothing: sized short, never refused as too costly,
  the payee's own funds. Raise, as `measurements.lookup` does; in the first hygiene commit.
- [x] `eth_call(to, data)` Call builder: private copies in prices and treasury on both, inline in
  tokens, x402, chain (and rpccheck's hand-written balanceOf). Home: `jsonrpc` (owns `Call`).
- [x] Selectors: `keccak(...)[:4]` in ~10 modules a side; `balanceOf` defined 4 times a side,
  `token0`/`token1` twice, `allowance` once outside tokens. `selector()` and `BALANCE_OF` in
  `evm` (a leaf; tokens can't hold it: tokens imports runner); `ALLOWANCE` and `allowance_call`
  in `tokens`; the treasury uses `tokens.balance_of_call`, not its own.
- [x] ABI helpers: the Worker's router.ts and treasury.ts re-define `word`, `addressWord` and
  `encodeBytes` (as `tail`), and treasury.ts hand-rolls `concat`; Python's treasury has its own
  `_word` (address padding, also in tokens and userops). Decided: `bruno/abi.py` mirroring
  `abi.ts`, for what eth_abi isn't used for directly (selector, word, address_word).
- [x] Pool reads (token0, token1, the spot) in prices and treasury on both, already drifting:
  prices reads loosely, treasury strictly (`word_bytes`). One reader in `prices`. Done as
  `pair_calls`, `pair_of` and `walk` in prices (both equally strict by then; the path walk was
  the copy), each module keeping its own extra read (spot, fee); a bad path PriceError in both.
- [ ] Python config addresses: a plain `str` re-checksummed at 31 call sites; the Worker's are
  checksummed as decoded. Decided: decode as `ChecksumAddress` (type changes are fine where
  they make better types).
- [ ] The Worker's hex quantity formatting `0x${v.toString(16)}`: 4 local helpers (`quantity`,
  `q` twice, `q`), 14 inline. One in `evm.ts` (not `quantity`: chain's parses the other way;
  Python uses `hex()`). Also Python's `wire.quantity` (int → str) and `chain.quantity`
  (str → int) share a name.
- [ ] plan's block tag (`chain.seen` or "latest") twice on both: a `Chain` property.
- [ ] Small: `Receipt.of` repeats `receipt_of` (added for a type check); rpccheck and preflight
  read 3 node values with bare `int(x, 16)`; the Worker's `chain.ts AccountState` shares a name
  with maintenance's, a subset of it; runner's `word(selector)` is `bigIntOf << 224`; tools
  re-define ARACHNID and CALIBUR; test helpers (verify, transfers, live) hand-build balanceOf
  and re-define router and pool selectors.
- Not folded: Python's per-item `except` pairs in relay (four, ~12 lines). A helper would hide
  the logging from ruff's BLE001, which requires it inside the handler (3.4).
- Kept: the 2^256 - 1 constants with distinct meanings (MAX_AMOUNT, UNLIMITED, NEVER).

## 3. Renames, 0.5.4 (a batch per module pair, pick the better name each time)

DataFee `fee`/`beyondGas`, `risen`/`risenDataFee`; JsonRpcError `message`/`nodeMessage`;
Answer `value()`/`resultOf()`; Chain `send`/`sendRaw`; `IndexResult`/`Index`; `status`/`statusOf`;
`InvalidParamsError`/`InvalidParams`; `X402Params`/`X402Call`; GasSchedule
`authorizations`/`authorizationsGas`; `config.Token`/`TokenConfig`; ChainShown
`snapshot`/`treasury`; and the rest of the review's ~30 pairs.

## 4. Splits, 0.5.4 (last)

- [ ] `relay` (1247/1329): pricing and quotes (`quote`, `x402_quote`, `charge`, `price`, fees,
  tip, `Payer`, `Quotes`); batches and blocks (`close_batch`, `on_block`, settling, funding);
  `relay` keeps submissions, status, measurements, clock and Store.
- [ ] `userops` (720/695): op, hash and estimate; send and receipt.
- `wire.py` (784) stays: type definitions, responses included (the Worker builds them inline).

## Also open

- [x] -32601 is both "no such path" and "chain isn't served": give the latter
  `data: {"chain": …}` (decided, in 0.5.3).
- `sepolia-verify`: cap the gas in ETH too (yes, in principle), revisited after the fixes. The
  care: ethereum-verify runs (rarely) through the same code, and Sepolia's Uniswap pools don't
  price ETH realistically, so a cap read from the pools could let mainnet spend everything.
- CI on PR #16.
