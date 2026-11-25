---
description: "Why submissions wait in a bounded queue ranked by profit, are simulated before every block as they'll be signed, a sponsored account's own transactions among them, and how their accounts are funded"
---

# The queue, batches and funding

## The queue and batches

**Submissions never hold an account.** They wait in a queue; a batch maps some onto accounts
for one batch interval; the next batch maps afresh. Bundles target a single block, so a
transaction signed for a block that's passed needs no cancelling, and the next is signed at
whatever the account's nonce is.

**Ranked by profit a submission, not per gas.** What's scarce is accounts, not block gas: an
account carries one submission a block, whether that's one transaction or several
([bundling](bundling.md)). Profit is the amount in wei at the pools' price, less what the
relay's own transactions cost at the next block's fee: their gas, and the ETH they send. A
sponsored account's own transactions cost the relay nothing beyond the ETH its first
transaction sends for them.

**A bounded queue.** At most `max_pending`; a full queue takes a submission only by evicting
its least profitable, and otherwise answers with the amount that would get in. Deadlines are
at most `max_deadline_blocks` ahead. Nothing waits indefinitely and nothing grows without
bound.

**Simulated again before every block.** A client can make its submission fail after it's
taken: spend its tokens, use its signature elsewhere. Builders would drop it every block at
no cost to the client, holding an account until its deadline. Simulating before each block
rejects it within one.

**Simulated twice, the second time as it will be signed.** Ranking a batch simulates each
submission's transactions in order, the relay's own from one stand-in account, with no fees
and no nonces, because no account is chosen yet. Once a submission has its account, they're
simulated again as they'll be signed: the relay's from that account, at its consecutive nonces
and the block's fees, with the node validating each as a block would. An account that can't
cover them is skipped before that, and a submission the node finds invalid sits out the block,
still pending. Without the second simulation, the first sign of either would be a builder
dropping the bundle, which says nothing.

**A sponsored account's transactions are simulated as it signed them,** both times, between
the relay's two, in one simulation. Each transaction runs in the state the one before left:
the account funded, its nonces used in order, its payment in the forwarder when the check
runs. None is simulated alone.

**Simulated at the highest block seen, never "latest".** A load-balanced node can answer
"latest" from a backend a block or more behind: a client's funding that has landed isn't
there yet, and the submission would be rejected for a revert that isn't. Pinned to the
highest block the relay has read, a node without it says so instead (EIP-1474's -32001,
geth's "header not found", or no result), and that's never a revert: on admission another
node is tried, then the client is told the relay is unavailable, to try again; a queued
submission sits out the block.

**A higher payer waits at most one batch.** A batch's mapping isn't changed mid-batch:
funding was computed for it. A submission whose transactions are out is never evicted, since
they may land, nor rejected for failing simulation, since a call that has landed no longer
simulates; its latest four sends are each looked for, past a node lagging a block.

**Decided on the queue as it stands.** Taking a submission and the relay's work on each block
run at the same time, and only taking is one at a time. Whatever is read of the chain is read
first, and the decision is made on the queue as it is then, with nothing awaited in between. An
eviction chooses among the submissions still pending and not yet sent, and a block signs only
those not evicted while it checked them. A submission finishes once, and what became of it
isn't overwritten.

**Bounded state.** A finished submission's status is kept `retention_seconds`, then
forgotten; revert data kept with a rejection is capped. Submissions are queued one at a time,
so a duplicate or a full queue is decided once, however many arrive together.

## Funding

**A creation that leaves nothing.** The funding transaction's initcode sends each top-up, then
self-destructs to the float, as the runner does: no account is left behind by a funding round.

**Per submission, with headroom.** A batch maps each submission to the lowest-numbered free
account that already covers its cost at the fee ceiling (the relay's own transactions' gas,
and the ETH a sponsorship's first transaction sends); failing that, the lowest-numbered free
account, topped up to `funding_multiplier` × that cost. With headroom,
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

**Simulated before it's first sent.** Its gas is the gas schedule's, not an estimate, so a
schedule short of what a fork charges would fail it, and a builder drops a bundle whose
transaction reverts: sent anyway, it would be signed again every block, never land, and leave
the queue waiting without a word. So it's simulated once, at the block, before its first copy
goes out. One that fails is a bug of ours: it isn't sent, it's dropped, and it's listed with
the chain's problems.

**Bounded.** The accounts hold at most `accounts` × `funding_multiplier` × the largest
submission's cost. A transaction can't spend more than its gas limit × max fee and the ETH it
sends, and nothing is signed unless its account covers that, so an account can't be overdrawn:
one that falls short sits out until topped up. Shortfalls cost capacity, not money.
