# 0.6.1: what 0.6.0 left

A working note, kept out of the 0.6.0 branch and deleted before 0.6.1 merges. 0.6.0 (PR #18) is
the interface (the quote taking the call, the breakdown, the transfer to sign, refusals), the
funding as a bundle, the EntryPoint called directly, and calls spread across a chain's nodes.
What's below wasn't done in it.

## 0.6.1: atomic sponsorship

Designed and reviewed, not built: [sponsorship.md](docs/development/plans/sponsorship.md). Its
"Still to decide" section holds what's open before building:

- whether to build `transactions` alone first;
- whether the switching-over machinery goes (and the submission's `release` field with it);
- the sponsorship deadline's key and default (suggested: `sponsorship_deadline_blocks`, 3).

Its "Structure: what the code still needs" lists the four things the code lacks.

## Left from 0.6.0's own work

**The nodes' routing** (design/reads.md):

- **The planning reads still go to one node whole,** though they're now asked at the seen
  block (0.6.1: an atomic read is one block's). They're the batch that grows with the queue.
  Pinned, they could be split; each caller then needs to handle a node without the block, as
  the simulations do.
- **A receipt from a node that's behind** reads as not mined. Receipts take no block, so
  this can't be pinned. True before the routing too.
- **A resting node is strictly out.** If every node that could answer a call is resting, the
  request fails as unavailable. The default Sepolia configuration has one node that simulates
  (publicnode), so a 429 from it is 5 seconds of refusals: find a second keyless one.
- **Only a rate limit rests a node.** A timeout or a 5xx sends the piece elsewhere and the
  node stays in rotation.
- **A `Retry-After` given as a date isn't read:** it gets the 5 seconds.

**`make rpc-check`:**

- It takes a configuration. A list of URLs (a file, or stdin), each node's chain found from the
  node, is `scratchpad/rpc_urls.py` for now: fold it in as a second way to run the check.

**Releasing:**

- `dist/config.json` isn't rebuilt when `config.local.json` is deleted, only when it changes.

**Messages and estimates:**

- A direct `handleOps` that reverts at the send is refused with the runner's caveat in its
  message (a one-off contract made the call), wrong for it, though only a bug of ours reaches
  it.
- An unsigned `authority`'s call is estimated with its current code: delegated elsewhere
  already, the estimate is of the old delegate.
- Refusals: a structured next step in `data`, beside the message, if wanted (a wire change).

**The documents:**

- The guides' examples were recorded on Sepolia against 0.5.4. The whole-call example has no
  response yet: record it after the 0.6.0 deploy.
- [parity.md](docs/development/plans/parity.md): the Time items.

**Tests not written:**

- a funding that reverts;
- a treasury bundle that doesn't land. The treasury's bundles stay on offer for up to five
  blocks while the next run signs at the same payee nonces; at most one set lands, benign as
  far as found.

**Housekeeping:** delete `scratchpad/ax`.

## Later

- **The client without `-dev`.** Its API settled, its `.d.ts` written as the reference a model
  reads, OpenAPI and OpenRPC aligned with it. Once it's not `-dev`, the API is a promise.
- **Leaner sending, the rest:** Safe and Multicall3 called without the runner, with the Safe
  work ([erc4337.md](docs/development/plans/erc4337.md)). The funding put ahead of the runs in
  a dropping bundle would save a block.
- **A Safe by one signature** ([safe.md](docs/development/plans/safe.md)): Safe's contracts
  vendored, deployed on first use, the SafeTx with a payment leg. Its four-item verification
  list first.
- **Money hygiene:** the cold wallet; persistence for the Python relay's queue and treasury
  snapshot, which it forgets on restart.
- **Base** ([morechains.md](docs/development/plans/morechains.md)): set aside while Bruno
  sends only through `eth_sendBundle`.
