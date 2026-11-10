---
description: "Why submissions wait in a bounded queue ranked by profit, are simulated before every block, and how their accounts are funded"
---

# The queue, batches and funding

## The queue and batches

**Submissions never hold an account.** They wait in a queue; a batch maps some onto accounts
for one batch interval; the next batch maps afresh. Bundles target a single block, so a
transaction signed for a block that's passed needs no cancelling, and the next is signed at
whatever the account's nonce is.

**Ranked by profit a transaction, not per gas.** What's scarce is accounts, one transaction
each a block, not block gas. Profit: the amount in wei at the pools' price, less the
transaction's gas limit × the next block's fee.

**A bounded queue.** At most `max_pending`; a full queue takes a submission only by evicting
its least profitable, and otherwise answers with the amount that would get in. Deadlines are
at most `max_deadline_blocks` ahead. Nothing waits indefinitely and nothing grows without
bound.

**Simulated again before every block.** A client can make its submission fail after it's
taken: spend its tokens, use its signature elsewhere. Builders would drop it every block at
no cost to the client, holding an account until its deadline. Simulating before each block
rejects it within one.

**Simulated twice, the second time as it will be signed.** Ranking a batch simulates each
submission's transaction from one account, with no fees and no nonce, because no account is
chosen yet. Once a submission has its account, its transaction is simulated again as it will be
signed: from that account, at its nonce and the block's fees, with the node validating all
three as a block would. An account that can't cover its transaction is skipped before that, and
a transaction the node finds invalid sits out the block, still pending. Without the second
simulation, the first sign of either would be a builder dropping the bundle, which says nothing.

**Simulated at the highest block seen, never "latest".** A load-balanced node can answer
"latest" from a backend a block or more behind: a client's funding that has landed isn't
there yet, and the submission would be rejected for a revert that isn't. Pinned to the
highest block the relay has read, a node without it says so instead (EIP-1474's -32001,
geth's "header not found", or no result), and that's never a revert: on admission another
node is tried, then the client is told the relay is unavailable, to try again; a queued
submission sits out the block.

**A higher payer waits at most one batch.** A batch's mapping isn't changed mid-batch:
funding was computed for it. A submission whose transaction is out is never evicted, since it
may land, nor rejected for failing simulation, since a call that has landed no longer
simulates; its latest four transactions are each looked for, past a node lagging a block.

**Bounded state.** A finished submission's status is kept `retention_seconds`, then
forgotten; revert data kept with a rejection is capped. Submissions are queued one at a time,
so a duplicate or a full queue is decided once, however many arrive together.

## Funding

**A creation that leaves nothing.** The funding transaction's initcode sends each top-up, then
self-destructs to the float, as the runner does: no account is left behind by a funding round.

**Per submission, with headroom.** A batch maps each submission to the lowest-numbered free
account that already covers its transaction at the fee ceiling; failing that, the
lowest-numbered free account, topped up to `funding_multiplier` × that cost. With headroom,
an account covers its next transactions without another funding round, and since the lowest
accounts are used first, those that stay funded follow the actual concurrency.

**Not waited for.** A batch's covered submissions go out at the next block; those awaiting
funding join once it lands. While a funding is in flight, a batch leaves out what needs
funding.

**A bundle of its own, a block at a time.** The funding goes to every builder as a bundle of
its one transaction, for the next block, and is signed again for each block after at the
float's same nonce until it lands. Only one transaction at a nonce can land, so only one copy
does; nothing waits in a mempool, and nothing of the float's is ever stuck. Each copy is priced
as the runs are, for its block alone. Not inside the runs' bundles: a standalone builder takes
a bundle a submission, and the float's nonce can be in only one of them; on its own, the
funding works the same in either mode. One the float can't cover isn't sent and is dropped,
as is one whose nonce the float has used for something else: the next batch funds what's
needed then, the float perhaps topped up by the treasury.

**Bounded.** The accounts hold at most `accounts` × `funding_multiplier` × the largest
transaction's cost. A transaction can't spend more than its gas limit × max fee, and nothing
is signed unless its account covers that, so an account can't be overdrawn: one that falls
short sits out until topped up. Shortfalls cost capacity, not money.
