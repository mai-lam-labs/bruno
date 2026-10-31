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

1. [ ] LOG0 in a receipt crashes Python's `eth_getUserOperationReceipt` (`userops.py`, `log.topics[0]`).
2. [ ] Server failure answers:
   - [ ] unknown chain name: message differs;
   - [ ] x402 on a chain without x402: generic 404 in the Worker;
   - [ ] incomplete deployment in a quote: message and `data` differ;
   - [ ] check order: chain before parameters (P 400, W 404);
   - [ ] malformed ids in the path (P 400, W 404);
   - [ ] wrong HTTP method (P 405, W 404);
   - [ ] JSON-RPC envelope: the Worker doesn't validate `method` or `id`;
   - [ ] `maxFeePerGas: null`: P rejects, W reads 0;
   - [ ] non-hex node values: W 503, P 500.
   Default: Python's messages, the Worker's routing (404 for an unserved chain whatever the parameters).
3. [ ] A node answering badly:
   - [ ] planning: P fails the whole batch on an unexpected error (`plan.py` catch), W rejects one
     submission. Target: reject the one submission.
   - [ ] measuring: P catches node errors only (`relay.py`), W everything. Target: node errors only.
   - [ ] non-hex revert data: P raises, W reads empty.
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

- `sepolia-verify`: cap the gas in ETH as well as the 5 USDC cap?
- CI on PR #16.
