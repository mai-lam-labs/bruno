---
description: "What the relay measures of a chain and when, degrading rather than failing, and keeping each slot's work to a few requests"
---

# Measuring and reading the chain

## Measured, not assumed

**What the relay knows of a chain, it measures,** in one batch: its nodes' chain id, the fork,
the deployer's code, each token's `balanceOf` gas, decimals and symbol, each x402 token's
transfer, the payee's holdings. Nothing is read on start; the chain is measured when it's first
needed, and again once `measure_seconds` old (an hour) when next needed, so an idle relay reads
nothing, and a fork or a token's upgrade goes unnoticed at most that long. On the Worker, each
wake measures afresh.

**Degrade, never die.** A token that can't be measured is suspended and the rest served; one
whose x402 transfer fails is taken without x402. A chain whose nodes are another chain, whose
fork no schedule knows, or whose deployer isn't its runtime is unavailable: nothing is quoted,
taken or sent, and the treasury holds off, until it measures right. Nodes that don't answer
keep the last measurements, tried again a minute later; with none yet, the chain is
unavailable and its nodes aren't asked again for a minute. A node's errors stay in the log:
what clients and the root see names no URL. The root lists what's wrong.

**A simulation must deploy the runner:** the deployer answers its address, 32 bytes; any other
answer (an address without code answers nothing) means no runner ran and no payment was
checked, and fails the simulation.

## Clock and reads

**Work only while there's work.** A relay's clock runs while a submission is pending or a
funding transaction is in flight: a submission taken starts it, and its batch closes at once.
With none, it stops; it wakes only to forget finished submissions after `retention_seconds`,
reading nothing, and a submission taken meanwhile starts it again at once, not when that
falls due. Quotes, status and the configuration are answered on request. An idle relay
reads nothing from its nodes.

**A step a slot.** Each step reads the latest block and the next base fee, in one request, and
prices and schedules all its work by them: the block's work if there's a new block, the batch
if it's due. The next step is at the next slot's start, its block looked for a second in, or
when the batch falls due, whichever is sooner. Nothing to do reads nothing more: a batch with
nothing queued, or nothing that still simulates, and a block with no batch to send. A step that
fails, its nodes not answering, is tried again a second later, then twice as long after each
failure in a row, up to five minutes.

**A slot's work is a few requests, whatever the queue.** Where each submission needs the same
read (its planning reads, its simulation, its receipts) or each account does (balance and
nonce), the reads go as one JSON-RPC batch, of at most 100 calls a request. A batch fails over
to the next node whole; a node's error answer to one call fails only that submission. Closing a
batch simulates every ranked submission at once, and those that no longer simulate are
rejected, rather than simulating down the ranking until the accounts are filled. Measuring a
chain is one request too ([measured, not assumed](reads.md#measured-not-assumed)), as is a token's price (every pool on its
path), and a treasury's reads.
