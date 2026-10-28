---
description: "The decisions behind Bruno, and why: only what the specification, the code and the user documentation don't already say"
---

# Design

The decisions behind Bruno's implementation of the [specification](../../SPEC.md), and why.
How each works is in the code; running a relay is in [deployment](../../DEPLOYMENT.md); what's
planned and the known gaps are on the [roadmap](../plans/priorities.md).

- [The deployer and the runner](runner.md): the transaction Bruno sends, and why it's shaped so.
- [Bundling](bundling.md): how transactions reach builders, and the builder rule relied on.
- [Gas](gas.md): gas limits computed or estimated, pricing before signing, and per-fork schedules.
- [The queue, batches and funding](queue.md): ranking, simulating every block, and funding accounts.
- [Treasury](treasury.md): the float refilling itself from what the payee collects.
- [Measuring and reading the chain](reads.md): what's measured, degrading, and few requests a slot.
- [Price](price.md): the tip, our fee, quotes, and token prices.
- [x402](x402.md): Bruno as x402's facilitator, and where it departs from x402's text.
- [Risks](risks.md): what could cost the relay or its clients.
- [Cloudflare Workers](worker.md): the relay on Workers.
