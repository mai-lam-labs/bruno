---
description: "What 0.5.4's alignment of the two relays left for later: names that differ between Python and the Worker but read fine on each, structural tidying nothing depends on yet, and two deferred kinds of time type"
---

# Parity

The Python relay and the Worker are two implementations of one relay, equals, module for
module. 0.5.4 aligned them where it mattered most: the `relay` split, the batches, the
operations layer that 0.6's MCP calls, and units of time. What's below was reviewed in 0.5.4 and
left for later. Each item is either a name that differs between the two relays but reads fine on
each, or tidying that nothing depends on yet. Each is best done when a change touches its module
anyway, on both relays together.

## Names

Python's name is given first, the Worker's second, then the one both should use. Only names that
differ between the relays are listed. A Python method or property and the Worker's `xOf(…)` over
an interface are the same name (`GasEstimate.gas` and `gasOf`, `_legs` and `legsOf`).

- **jsonrpc:** `Answer.value()` and `resultOf(answer)` become `result_of`.
- **server:**
  - Python's `wire.data` and `wire.quantity` encode, while the Worker's private `data` decodes
    and its `hex` and `hexQuantity` encode. They become `hex_data`, `hex_quantity` and `dataOf`.
  - `ChainShown`'s `snapshot` and `treasury` get one name.
  - The Worker's `openapi(origin, x402)` takes a minimum fee as `x402`, with `undefined` and
    `null` meaning different things. It becomes `minFee`, with the no-x402 case explicit.

## Structure

- **front:** the Worker's `x402MinFee` is read inline in Python's app.py. One shape on both.
- **config:** Python's `ChainConfig` keeps `rpc` and `rpc_extra` as the file has them, with
  `nodes` a property over both, because msgspec decodes the fields as written. The Worker's
  holds `nodes` alone, decoded. Both are read through `nodes`; this one stays.

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
