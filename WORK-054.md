# 0.5.4 code organisation (temporary, delete before merging)

Python/Worker parity and layout, carried from 0.5.3 (the drift review of 2026-11-01), as
preparation for 0.6 (MCP). Neither relay is the reference: each rename picks the better name of
the pair, and each split is made alike on both sides.

## 1. Renames (a batch per module pair, pick the better name each time)

DataFee `fee`/`beyondGas`, `risen`/`risenDataFee`; JsonRpcError `message`/`nodeMessage`;
Answer `value()`/`resultOf()`; Chain `send`/`sendRaw`; `IndexResult`/`Index`; `status`/`statusOf`;
`InvalidParamsError`/`InvalidParams`; `X402Params`/`X402Call`; GasSchedule
`authorizations`/`authorizationsGas`; `config.Token`/`TokenConfig`; ChainShown
`snapshot`/`treasury`; and the rest of the review's ~30 pairs.

## 2. Splits (last)

- [ ] `relay` (1247/1329): pricing and quotes (`quote`, `x402_quote`, `charge`, `price`, fees,
  tip, `Payer`, `Quotes`); batches and blocks (`close_batch`, `on_block`, settling, funding);
  `relay` keeps submissions, status, measurements, clock and Store.
- [ ] `userops` (720/695): op, hash and estimate; send and receipt.
- `wire.py` (784) stays: type definitions, responses included (the Worker builds them inline).

## Also open

- **Docs:** a page, *An EOA, with permissionless.js*, in the accounts overview's table and the
  site's navigation (so in llms.txt), and `support.md`.
