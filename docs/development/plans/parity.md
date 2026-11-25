---
description: "Where the two relays differ on purpose and why it stays, what to ask of a change made to both, and the deferred time types"
---

# Parity

The Python relay and the Worker are two implementations of one relay, equals, module for
module, each module's shared definitions in the same order and named alike on both. A Python
method or property and the Worker's `xOf(…)` over an interface are the same name
(`GasEstimate.gas` and `gasOf`). 0.6.1 made the names that differed one.

## Differences that stay

These are the differences that are meant. One that isn't listed here is taken to be an
accident, and is made the same on both.

- **State:** the Python relay keeps its queue, its batch and the funding in flight in memory,
  and a restart loses them. The Worker keeps them in its chain's Durable Object's storage, with
  the accounts as last read and the chain's problems, so they outlast an eviction
  ([Cloudflare Workers](../design/worker.md)). Both keep their measurements in memory only.
- **Stored rows from another version:** only the Worker has any. It reads what the deployed
  version before it stored, for as long as such rows can still be there, and nothing is
  migrated.
- **The clock:** the Python relay runs its steps from an asyncio task of its own. The Worker's
  steps are its Durable Object's alarms, and its treasury is woken by a cron.
- **Reading requests:** Python's request and response types are msgspec Structs, which Litestar
  decodes, validates and documents. The Worker reads each request by hand, field by field. The
  OpenAPI and OpenRPC documents are generated from the Python, and the server tests hold both
  to the same answers.
- **Operator tooling:** preflight, the deployer's and the forwarder's deployment, the verify
  run and the vectors' generation are Python's alone.
- **config:** Python's `ChainConfig` keeps `rpc` and `rpc_extra` as the file has them, with
  `nodes` a property over both, because msgspec decodes the fields as written. The Worker's
  holds `nodes` alone, decoded. Both are read through `nodes`.
- **front:** OpenAPI's least x402 price is read from the relays' configurations in Python's
  app.py, and from each chain's Durable Object (`ChainShown.x402MinFee`) in the Worker. The
  Worker's entry point reads only each chain's payee: a chain's configuration is read and
  checked by its own Durable Object, which reports what's wrong with it.

## A change made to both

Questions to ask of a change before calling the two relays the same. They're a reviewer's
prompts, not a step of the release.

- Is the state the same after it: what's queued, what's stored, what a status answers?
- Are requests checked in the same order, so the same request gets the same refusal?
- Is each failure classed alike: the client's, the node's to try again, the operator's, or a
  bug of ours?
- Is anything kept or timed differently: how long a reading is kept, when it's read again?
- Does each degrade and retry alike when a node or a token fails?
- Do the integers round the same way?
- On the Worker, is the result the same after its object is evicted and loads again?
- Is it arithmetic that a golden vector would hold both to?

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
