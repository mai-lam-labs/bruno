---
description: "What could cost the relay or its clients: builders including reverting transactions or part of a bundle, grants to the caller, denial of service, what clients see, keys"
---

# Risks

**A builder that includes a reverting transaction** (ignoring `droppingTxHashes`, or a
standalone bundle's failure) makes the relay pay its gas, unpaid. Builders are used only once
seen to drop; nothing is ever listed in `revertingTxHashes`.

**A builder that includes part of a bundle.** A submission of several transactions pays in
its last: a listed call's transfer, a sponsorship's check
([bundling](bundling.md)). A builder that lands the first without the rest leaves the relay
unpaid for it. For a listed call that's its gas. For a sponsorship it's also the ETH the first
transaction sent the account, which is the larger part. What the account is sent is capped
when a sponsorship is taken, with the relay's own gas, at what one transaction of `max_gas`
costs by the deadline; the relay's own gas then follows the market, as a call's does
([sponsorship](sponsorship.md#what-the-account-is-sent)). The relay finds a broken bundle by its own receipts, rejects the submission and sends nothing more of it. The same can follow a reorg,
since an orphaned block's transactions are public ([sponsorship](sponsorship.md)).

**Grants to the caller.** Each runner is a fresh address, salted by its sender and block, and
gone after its transaction: approvals or roles a call grants its `msg.sender` are dead after
it. A concurrency account is itself a call's `msg.sender` in four places. Three grant their
caller nothing: a target on the list of contracts that treat every caller alike, the
EntryPoint for an op paying by its fees ([the runner](runner.md)), and the forwarder, which has
no permissions. The fourth is a sponsored account's own code, which the account asked to have
called.

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
