---
description: "What could cost the relay or its clients: builders including reverting transactions, grants to the caller, denial of service, what clients see, keys"
---

# Risks

**A builder that includes a reverting transaction** (ignoring `droppingTxHashes`, or a
standalone bundle's failure) makes the relay pay its gas, unpaid. Builders are used only once
seen to drop; nothing is ever listed in `revertingTxHashes`.

**Grants to the caller.** Each runner is a fresh address, salted by its sender and block, and
gone after its transaction: approvals or roles a call grants its `msg.sender` are dead after
it. The relay's own accounts are never the call's `msg.sender`.

**Denial of service.** Every submission taken costs simulations. The queue, deadlines, gas,
calldata and authorizations are bounded, and each authorization is range-checked (y_parity,
low s, its nonce) before any work; rate limiting belongs in front of the endpoint, and is
required for a public deployment ([DEPLOYMENT.md](../../DEPLOYMENT.md)): a held x402 request is cheap to keep, but
nothing else bounds how many a client opens. One
submission failing (a drained pool, a node's bad answer) skips it, not its batch or block.

**What clients see.** An internal error is answered as one, its detail logged: a node's URL,
an API key in it, never reaches a response. A chain is reached by its canonical id or its
network's name, both the same chain; the Worker routes only a chain the configuration names,
by its id, so no request creates an object, or a second one for a chain.

**Keys.** The float's key is hot: it holds ETH for gas, bounded by the funding settings.
Payments accrue to the payee, whose key is held elsewhere.
