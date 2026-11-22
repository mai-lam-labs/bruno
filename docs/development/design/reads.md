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

**A node that fails rests.** A node that fails a request (a timeout, a refused connection, an
HTTP error, an answer that isn't JSON-RPC), or answers a call that it's over its rate limit, is
left out of every request for as long as its `Retry-After` says, or for five seconds without
one. Asking it again sooner spends a request to learn nothing, a timeout's worth of waiting for
one that hangs, and some providers lengthen a rate limit for it.

**A node is never asked a method it's configured `without`.** Keyless tiers often serve reads
and refuse `eth_simulateV1`. Such a node still carries its share of the reads, and the
simulations go to the others; a method every node is without is a configuration mistake.

**Reads that are one view of the chain go to one node, at one block** (`atomic`): the head with
its fee history, a balance with the nonce beside it, receipts with the float's nonce, an op's
payment read before and after its run. Two nodes can be a block apart, so splitting these would
join answers from two states. One node isn't enough either: it answers a batch in requests of
at most its `max_batch`, and behind a load balancer from more than one head, so a block can land
between two of its answers. So every call in such a read that takes a block is asked at a block
number. The relay's work on a block (reconciling, the accounts read for signing and for the
batch, the treasury) asks at the block it's for. An op's or a payment's reads (what an op pays,
x402's checks) ask at the highest block seen, which the head read each of them starts with
raises.

Two of these reads can't be pinned. The head is read at the latest block, and its fee history
must end at the head's block; on an OP-stack chain the head is read again after the data fee's
inputs, which can't say which block answered them. If either check fails, the fee history and
the inputs are read again at the head's number. Receipts take no block, so the float's nonce is
always asked beside them at the block being reconciled, and read first. A node without the block
fails it, so the receipts are a node's that has the block: one behind has none of the block's
receipts, and a submission landing in its deadline block would read as expired. A nonce the
funding moved then means its receipt is there to find. A price's pools are still asked at the latest block, since a price is
kept for `price_ttl_seconds` anyway, and pools a block apart are no older than that.

Simulations, gas estimates and a submission's plan (its call's target, its authorities and its
payer, the batch that grows with the queue) are asked at the highest block seen, which any node
that has it answers alike, so they're split freely. A node without the block yet has failed the
call, and another is asked; if none has it, a submission sits the block out, and a quote is
answered that a node failed.

**What follows a transaction is read after it.** The tooling that sends through the mempool and
waits (the deployer, the live tests, the verify run) reads a nonce, then signs with it. Asked at
the latest block, a node behind answers from its own older head, and the transaction signed
with that nonce is refused as too low (Sepolia, 2026-11-12: a node four blocks behind). So
these reads are asked at the highest block the process has seen, which a head read raises and
so does the receipt of a transaction it waited for. A node without that block can only say so,
and another is asked. A block no node has again within two slots was orphaned by a
reorganisation: the chain is then taken as the nodes have it, and read at its head.
