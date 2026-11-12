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

**A runner's simulation must deploy the runner:** the deployer answers its address, 32 bytes;
any other answer (an address without code answers nothing) means no runner ran and no payment
was checked, and fails the simulation. An op paying by its fees has no runner: its `handleOps`
goes to the EntryPoint as is and answers nothing, and anything else fails its simulation.

**Every simulation is `eth_simulateV1`,** so a node of the chain must answer it. The
measurement asks for it with the rest, and a chain with no node that has it is unavailable,
the operator's to fix, before anything is quoted or taken.

## Clock and reads

**Work only while there's work.** A relay's clock runs while a submission is pending or a
funding transaction is in flight: a submission taken starts it, and its batch closes at once.
With none, it stops; it wakes only to forget finished submissions after `retention_seconds`,
reading nothing, and a submission taken meanwhile starts it again at once, not when that
falls due. Quotes, status and the configuration are answered on request. An idle relay
reads nothing from its nodes.

**A step a slot.** Each step reads the latest block and the next base fee, in one request, and
prices and schedules all its work by them: the block's work if there's a new block, the batch
if it's due. The next step is at the next slot's start, its block looked for
`block_delay_seconds` in (a twelfth of the slot unless configured, so a second on Ethereum), or
when the batch falls due, whichever is sooner, and never sooner than `min_step_seconds` after
the last. Nothing to do reads nothing more: a batch with
nothing queued, or nothing that still simulates, and a block with no batch to send. A step that
fails, its nodes not answering, is tried again a second later, then twice as long after each
failure in a row, up to five minutes.

**A slot's work is a few requests, whatever the queue.** Where each submission needs the same
read (its planning reads, its simulation, its receipts) or each account does (balance and
nonce), the reads go as one JSON-RPC batch. A node's error answer to one call fails only that
submission. Closing a batch simulates every ranked submission at once, and those that no longer
simulate are rejected, rather than simulating down the ranking until the accounts are filled.
Measuring a chain is one batch too ([measured, not assumed](reads.md#measured-not-assumed)), as
is a token's price (every pool on its path), and a treasury's reads.

**A batch is spread across the nodes, and only what failed is asked again.** A batch goes out
in pieces of at most 20 calls (a node's own `max_batch` if it has one), each piece to a
different node at once, so no node carries a whole slot's reads and none is sent a request
large enough to trip its limits. A node fails a piece whole (an HTTP error, a timeout, a web
page) or one call of it (rate limited, a method it hasn't, a block it hasn't seen yet); either
way only those calls go to another node, and the answers the piece did give are kept. Any
other error is the call's answer, a revert included: another node would say the same.

**A rate-limited node rests.** A node that answers 429, or a call with a rate-limit error, is
left out of every request for as long as its `Retry-After` says, or for five seconds without
one. Asking it again sooner spends a request to learn nothing, and some providers lengthen the
limit for it.

**A node is never asked a method it's configured `without`.** Keyless tiers often serve reads
and refuse `eth_simulateV1`. Such a node still carries its share of the reads, and the
simulations go to the others; a method every node is without is a configuration mistake.

**Reads that are one view of the chain go to one node** (`atomic`): the head with its fee
history, a balance with the nonce beside it, receipts with the float's nonce, a price's pools.
Two nodes can be a block apart, so splitting these would join answers from two states.
Simulations and gas estimates are asked at the block the relay has seen, which any node
answers alike, so they're split freely. The planning reads are still asked at the latest block
and so still go to one node.

**What follows a transaction is read after it.** The tooling that sends through the mempool and
waits (the deployer, the live tests, the verify run) reads a nonce, then signs with it. Asked at
the latest block, a node behind answers from its own older head, and the transaction signed
with that nonce is refused as too low (Sepolia, 2026-11-12: a node four blocks behind). So
these reads are asked at the highest block the process has seen, which a head read raises and
so does the receipt of a transaction it waited for. A node without that block can only say so,
and another is asked. A block no node has again within two slots was orphaned by a
reorganisation: the chain is then taken as the nodes have it, and read at its head.
