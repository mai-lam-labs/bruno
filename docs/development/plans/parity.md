---
description: "What 0.5.4's alignment of the two relays left for later: names that differ between Python and the Worker but read fine on each, structural tidying nothing depends on yet, the userops split, and two deferred kinds of time type"
---

# Parity

The Python relay and the Worker are two implementations of one relay, equals, module for
module. 0.5.4 aligned them where it mattered most: the `relay` split, the batches, the
operations layer that 0.6's MCP calls, and units of time. What's below was reviewed in 0.5.4 and
left for later. Each item is either a name that differs between the two relays but reads fine on
each, or tidying that nothing depends on yet. Each is best done when a change touches its module
anyway, on both relays together.

## Names

Python's name is given first, the Worker's second, then the one both should use.

- **submission:** `authorities` is imported as `authorities_of` by both plans, and bare by the
  bundler and x402. It becomes `authorities_of` where it's defined.
- **chain:** `lasting_fees`'s `priority_fee` is `priority` in the Worker. Both use
  `priority_fee`.
- **datafee, transactions, forks:**
  - `DataFee.fee` and `beyondGas` become `beyond_gas`.
  - `_authorization` and `authorizationFields` get one name, as do `_signed`'s `beyond_gas`
    and `signed`'s `beyond`.
  - `GasSchedule.authorizations` and `authorizationsGas` become `authorizations_gas`.
- **jsonrpc:**
  - `JsonRpcError.message` and `nodeMessage` become `node_message`. Python's error keeps its
    `method` too, as the Worker's does.
  - `Answer.value()` and `resultOf(answer)` become `result_of`.
- **tracked:** `Tracked.user_operation` holds the op's hash, so it becomes `user_op_hash`. The
  Worker's stored JSON key stays `userOperation`, mapped explicitly, so storage doesn't change.
- **config:** `Token` and `TokenConfig` become `TokenConfig`.
- **userops:**
  - `FeePayment.at` and `paidAt` become `paid_at`.
  - `FeePayment.price` and `UserOperation.gas_price` against `priceOf` (a price a gas) become
    `gas_price`.
  - `GasEstimate.gas` and `gasOf` get one name.
  - `gas_estimate()` returns an `eth_estimateGas` call, beside the type `GasEstimate`. It
    becomes `estimate_call`.
- **router:** `_legs` and `legsOf` become `legs`.
- **server:**
  - `take` builds the paid submission, so it becomes `paid_submission`.
  - The Worker has two `transferOf`s, in wire.ts and x402.ts. x402's becomes `paymentTransfer`.
  - Python's `wire.data` and `wire.quantity` encode, while the Worker's private `data` decodes.
    They become `hex_data`, `hex_quantity` and `dataOf`.
  - `UserOperationReceiptJson` is a response among the `*Result`s;
    `UserOperationParams.operation()` and `UserOperationJson.user_operation()` are one
    conversion with two names; `SendParams` is decoded as "submission" everywhere else.
  - `ChainShown`'s `status` and `snapshot` against `status` and `treasury`, and `accounts`,
    become `accounts_status`, `treasury` and `concurrency_accounts`, the root's fields each
    becomes.
  - `account_rows` and `accountsOf` become `account_rows`.
  - The Worker's `openapi(origin, x402)` takes a minimum fee as `x402`, with `undefined` and
    `null` meaning different things. It becomes `minFee`, with the no-x402 case explicit.
  - The Worker's `STATUS` path pattern becomes `SUBMISSION`; Python's `_user_operation` and the
    Worker's `userOperationOf` get one name.

## Structure

- **x402:**
  - Python keeps x402's nesting of `PaymentPayload`, while the Worker flattens `accepted.extra`
    into `accepted`. Both should keep x402's nesting.
  - Python names `X402Params.submission()`, `_requirements()` and `result()`; the Worker writes
    them inline. They should be named on both, where the two would otherwise drift.
- **front:** the Worker's `x402MinFee` is read inline in Python's app.py. One shape on both.
- **maintenance:** Python's `top_ups`, `replenish` and `sweep` are reached only by the tests and
  the live tooling, so they belong in `bruno/tests`.
- **The userops split:** userops is about 700 lines on each relay. It splits into the op, its
  hash and estimate, and sending it and its receipt.

## Time

0.5.4 typed units of time (`Seconds` and `Millis`, in units.py and units.ts) for the relay's
clock, its configured durations, and where a block's time meets the clock. Three things were
left for later.

- **Chain time and tooling.** Block timestamps, EIP-3009's valid window, x402's timeouts and the
  treasury snapshot's `at` are still plain integers of Unix seconds, as are the operator
  tooling's waits (the deployer, `make preflight`, `Chain.send`). They could become `Seconds`
  where it's a duration. The one constraint is EIP-3009's `validBefore`: it's a signed
  `uint256`, and a client may give `2^256 - 1` for "never".
- **A UTC instant type,** as teeswap's `Timestamp`. It isn't worth it in Bruno yet. Bruno's
  instants arrive as integers from the chain and go back into block arithmetic or signed data,
  and the wire carries them as hex quantities, so a datetime would mostly convert at the edges.
  `validBefore` can't be one at all.
- **An instant type per clock.** Python's relay reads two clocks, `Millis.monotonic()` and
  `Millis.unix()`, and both readings are `Millis`, so subtracting one from the other would
  type-check. Today they meet only in the relay's step, each turned into a duration first. A
  type for each clock's instants would make mixing them an error. The Worker has one clock,
  `Date.now()`, so it needs none.
