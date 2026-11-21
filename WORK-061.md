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

- Its batch-size probe (181 calls) uses up a rate-limited node's allowance before the batch of
  every method is tried, so a keyless tier can read as failing that batch for the check's own
  load (Tenderly, 2026-11-12).
- It takes a configuration. A list of URLs (a file, or stdin), each node's chain found from the
  node, is `scratchpad/rpc_urls.py` for now: fold it in as a second way to run the check.

**Releasing:**

- `dist/config.json` isn't rebuilt when `config.local.json` is deleted, only when it changes.
- **The Worker's configuration is bound as text, and Cloudflare caps a text binding at about
  5.1 kB.** 0.6.0's deploy was refused at 5,913 bytes (17 and 8 nodes, indented) and went up
  minified, by hand, at 4,043 (`make worker-deploy WORKER_CONFIG=dist/config.min.json`).
  `deploy.ts` should upload it minified and say how close to the cap it is; about 1,000 bytes
  are left, a dozen keyed URLs. Past that the configuration needs another home (a secret, or
  KV).
- `make rpc-check` asks every node of a chain at once, about 250 calls each: with many keys at
  one provider it trips their shared limit and reports different keys failing each run. One
  node at a time, sizes only up to the node's cap.
- Nothing tests the Cloudflare side before the tag (how nodes treat Cloudflare's addresses,
  say). A staging Worker under another name would, with a funded float of its own: two relays
  on one float would fight over its nonces.

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
- [parity.md](docs/development/plans/parity.md): the chain modules' order, the names, the
  Worker's `x402MinFee`, the Time items.

**Tests not written:**

- a funding that reverts;
- a treasury bundle that doesn't land. The treasury's bundles stay on offer for up to five
  blocks while the next run signs at the same payee nonces; at most one set lands, benign as
  far as found.

**Housekeeping:** delete `scratchpad/ax`.

## Deferred: binding x402 quotes

- **Binding x402 quotes.** x402 v2 has no quote id, no price expiry and no link between a 402
  and the payment answering it: only `accepted`, the requirement the client chose, echoed whole
  in its payment, which the spec never says to check. What's in a requirement's `extra` comes
  back with it. For `exact` on EVM, `extra` carries the token's EIP-712 `name` and `version`;
  x402 reserves `assetTransferMethod` and `paymentFlow`; any other key is the server's. Bruno's
  `extra` (wire.py `TokenDomain`) gains `quote`:
  - `extra.quote = {validUntilBlock, gas, mac}`: the quote's last block, as `postpaid_quote`'s;
    the call's gas it's priced for (estimated if the body had none), taken as the submission's;
    and a MAC. No id: the MAC is deterministic over what the quote promises, so it identifies
    it (logs, a re-challenge's `error`), and nothing is stored.
  - `mac = HMAC-SHA256(quote_key, …)` over the chain id; the requirement as issued (`asset`,
    `amount`, `payTo`); `validUntilBlock` and `gas`; and a digest of the request (`call`,
    `authorizationList`, `payer`, `deployment`, `verifyGas`: the body posted again with the
    payment), so a cheap call's quote can't pay for a dear one.
  - `quote_key = HMAC-SHA256(float key, "bruno/x402-quote/v1")`: derived, no new secret; one-way,
    so it reveals nothing of the float key; HKDF-Expand's single block (RFC 5869). Python's
    `hmac`, the Worker's `@noble/hashes` (`hmac`, `sha2`), already a dependency. Rotating the
    float key lapses outstanding quotes, which last `quote_blocks` anyway.
  - A payment whose `extra.quote` verifies against the requirement echoed and the body posted,
    at or before `validUntilBlock`: its call given the quote's `gas`, not estimated again.
    Anything else (a client not echoing `extra`, a tip, which changes `accepted.amount`, a lapsed
    quote, a MAC that doesn't verify): estimated again, as now.
  - Not 100% binding: every payment, quoted or not, is still refused below the next block's
    charge (402 at what's required), Bruno's prerogative where keeping the quote would cost it
    money (gas past the quote's ceiling, the token's price moved). Above the quote is a tip, as
    now. A refusal names the quote it refuses, by its MAC, and why: lapsed, or short. And the
    submission is simulated either way, so a call that stops working is still refused.
  - To verify first: x402's own SDK echoes `extra` verbatim, unknown keys included (Bruno's
    tests already run against it).
  - Deferred (2026-11-05). The MAC is needed whenever the quote is trusted, and its point was
    to spare the payment's estimate. But the floor above, the next block's charge, means
    working out the charge again on every payment, quoted or not, and simulating it either way:
    the MAC saves one `eth_estimateGas` and nothing else. Truly binding (the quoted amount
    taken within validity, the charge's rise Bruno's risk) would save the work, at the cost of
    the floor. Revisit if the estimate shows up as a cost, or a client needs the quoted amount
    honoured.

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
