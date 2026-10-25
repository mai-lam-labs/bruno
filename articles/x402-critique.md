# x402 critique article

## Brief (as handed over)

```markdown
# Brief: x402 critique article — positioning, structure, and launch plan

## Context
- Product: permissionless gasless relay on Ethereum mainnet, the first module of a
  "modular glue" suite that fills gaps between existing blockchain tools.
- Relay settles via builder/bundler infrastructure: private orderflow (no public
  mempool exposure) and reverted transactions cost the operator nothing.
  This is why only mainnet is supported today.
- The relay settles the x402 payment atomically with the user's requested
  transaction.
- Goal of the article: a high-signal technical critique of x402 that doubles as
  the product's positioning and the top of an adoption funnel.
- The author is deeply expert in the engineering. Do not add advice on fee
  recovery, griefing, or relay correctness. Focus on argument, positioning,
  structure, and growth.

## Core thesis
x402 treats payment as a side channel to work rather than part of it. Its design
assumes low-value, facilitator-mediated, fast-finality payments, and those
assumptions break as value rises and when settlement must be atomic with
on-chain execution.

## Objections (from the author's implementation experience)
1. **No nonce binding between request and payment.** The server can't tie a
   payment to a specific request. To detect double-spend attempts (a client
   settling several of the same payment concurrently), the server must track all
   in-flight client nonces itself, or it can't tell which one settled.
   -> Symptom: payment not bound to the request.
2. **Synchronous settlement assumptions.** Atomic payment + transaction means the
   tx ID isn't known in advance, and waiting for inclusion exceeds the default
   timeout of most x402 clients.
   -> Symptom: payment not bound to the transaction's outcome.
3. **transferWithAuthorization only; no receiveWithAuthorization.** The receiving
   contract can't pull funds within its own call, so payment and on-chain
   execution are separate events: one processing doesn't imply the other ran.
   Breaks composability with the contract ecosystem; makes atomic fee splitting
   hard.
   -> Symptom: payment not bound to on-chain execution.
4. **Discovery coupled to facilitator settlement.** Bazaar listing requires
   settling through a facilitator. There are no facilitators on Ethereum
   mainnet, and using one would break the atomic settlement model.
   -> Distribution problem: the main passive discovery channel is closed.
5. Further practical awkwardness, especially at higher values: see the author's
   notes. Candidate for an appendix or follow-up post.

Objections 1–3 are three symptoms of the core thesis; 4 is the growth cost.
VERIFY each claim against the current x402 spec version before publishing.

## Tone and stance
- A technical essay, not a takedown. State each problem, its practical
  consequence, and what correct design looks like.
- Stay wire-compatible with x402; never make adoption contingent on agreeing.
- Position the product as "x402-compatible, with these guarantees."
- Positioning line: "Composable x402."
- Mainnet-only framed as rigor: "we ship only where we can guarantee private
  orderflow and revert protection," with chain expansion conditional on that.

## Constructive proposals (to open upstream as spec issues/PRs)
- Request-bound payment identifier (fixes 1).
- Async settlement / receipt pattern (fixes 2).
- New payment scheme built on receiveWithAuthorization (fixes 3), using x402's
  existing scheme extensibility.
- Decouple discovery from settlement (fixes 4).

## Suggested structure
1. Hook: where x402 breaks once payments carry real value.
2. Thesis: payment as a side channel.
3. Three symptoms, each with a concrete failure scenario.
4. The growth cost: discovery tied to facilitators; none on mainnet.
5. What correct design looks like (proposals above).
6. How the relay delivers it today, staying x402-compatible.
7. Appendix: further practical issues.

## Launch sequence
1. Publish the essay.
2. Same week: open the upstream spec issues and link them from the essay.
3. Immediately after: tutorials that give agreeing readers somewhere to go:
   - "Atomic revenue splits for paid MCP tools" (lead hook; payment, split, and
     execution in one tx)
   - "MEV-protected gasless stablecoin transfers on mainnet"
   - A client wrapper/config that handles async settlement against the relay
4. Build discovery independent of Bazaar: MCP registries, agent tool
   directories, llms.txt.

## Open items for the receiving agent
- Pull the additional objections from the author's notes.
- Align proposals and tutorials with the project roadmap and the next modules.
- Identify which composability use cases (escrow, conditional payments,
  refunds) are planned, for a follow-up post.
- Confirm current status of x402 spec versions, Bazaar requirements, and any
  mainnet facilitators.
```

## Verification against x402 (coinbase/x402 at `0e6e8c5`, 2026-10-23)

Spec copies in `scratchpad/x402spec/`. Each objection as the spec stands:

1. **Request binding: holds, sharpened.** The signed EIP-3009 authorization commits to `from`,
   `to`, `value`, the validity window and a 32-byte nonce: nothing of the request. x402 now has
   a `payment-identifier` extension (`extensions/payment_identifier.md`): a client-chosen id,
   16–128 characters, "an idempotency key", carried in `extensions`, outside the signature. It
   deduplicates retries of an honest client; it doesn't bind the payment, since anyone holding
   the payload can strip or change it. The article must name it, or a reader will.
   Our concrete case differs from the brief's wording: the same signed payment sent twice is
   one submission (same id); the case we handle is a client re-signing after a timeout, a
   different payment for the same request, which the relay matches by payer and exact call
   (`Relay.pending_like`) and holds for the first. The draft uses that.
   Constructive angle found: the 3009 nonce is the client's to choose, so a nonce derived from
   the request (`H(request, salt)`) binds them inside the existing signature, no new field.
2. **Outcome binding: holds, with x402's two provisions named.** `settlement_pending` exists
   (§9) but MUST carry a broadcast transaction hash, which atomic settlement doesn't have before
   inclusion (re-signed per block). The `upfront` flow (§6.1; `exact` MAY use it) gives a hash
   at once by settling first, giving up atomicity. `maxTimeoutSeconds` exists; x402's own MCP
   client sets its read timeout from it; HTTP clients mostly don't (httpx defaults to 5 s).
   "Most x402 clients" time out: true of HTTP clients as configured by default; say so
   precisely.
3. **Execution binding: holds; the framing to lead with.** `exact` on EVM has three methods:
   EIP-3009 (`transferWithAuthorization`), Permit2 (`permitWitnessTransferFrom` through
   `x402ExactPermit2Proxy`, which enforces the receiver: the proxy calls, not the payee), and
   ERC-7710 delegations. None is `receiveWithAuthorization`, whose `msg.sender == to` check
   lets the payee contract collect within its own call. The sharp consequence:
   `transferWithAuthorization` can be submitted by anyone who sees the signature, so the
   payment can land on its own, without the work it pays for. Bruno closes this by private
   orderflow and its runner, not by anything in x402.
4. **Discovery: holds for the Bazaar that matters; "no facilitators on mainnet" is unverified.**
   Bazaar cataloging is done by facilitators from the `PaymentPayload`s they process
   (`extensions/bazaar.md`, "Facilitator Behavior"); §8 lets any facilitator host discovery.
   CDP's facilitator (the one CDP's Bazaar indexes from) settles on Base, Polygon, Arbitrum,
   Monad, World, not Ethereum. docs.x402.org's facilitator list names none for Ethereum, but
   Corbits and Solvador claim multi-EVM coverage: check them before writing "none". Safe
   claim: "the Bazaar most clients search can't see a mainnet resource that settles itself".
   **Correction (2026-10-23): Daydreams' facilitator settles on Ethereum** (`/supported` lists
   `eip155:1`, `exact` and `upto`, keyless). "No facilitators on mainnet" is false; drop it.
   The niche is narrower and still true: **atomic** x402 on Ethereum (the payment can't land
   without the call), which a facilitator settling the transfer alone can't give.

## Alignment with the plans (`plans/`)

- **The niche: atomic x402 on Ethereum mainnet, deployed, within the spec** (`exact`,
  EIP-3009, settled in the call's own transaction). Facilitators do settle on Ethereum
  (Daydreams lists `eip155:1`), but a facilitator settles the transfer alone: none can make
  the payment conditional on the call. Evidence to link: a mainnet x402
  settlement (`make ethereum-verify`, live, not run yet; Sepolia's passed against v0.3.0).

- **The relay runs on Ethereum and Sepolia,** not mainnet only; Sepolia is the test network.
  "Ship only where we can guarantee private orderflow and revert protection" matches
  `v0X0-morechains.md`: Unichain qualifies (public `eth_sendBundle`, revert-protected by
  default, one sequencer), Linea behind Infura's beta.
- **The relay holds x402 requests until mined** (the 200 carries the transaction). Objection 2
  is our own cost, paid deliberately; the article can say so.
- **`v0X0-x402-async.md` adds `upfront`** (settle first, any chain). The essay argues for
  atomic; `upfront` is then x402's own provision, offered beside atomic where the guarantee is
  missing, never instead. The essay should not read as if Bruno rejects it.
- **"Atomic revenue splits for paid MCP tools"** isn't buildable yet: x402 over MCP is out of
  `v0X0-MCP.md` for atomic settlement (no hash before inclusion), and a split needs
  `v0X0-facilitator.md`'s option D (forwarder) or E. The plain-MCP relay (`submit`, a signed
  3009 transfer as payment, Calibur batches) can do payment + split + execution in one
  transaction without x402: a tutorial for that exists sooner.
- **"MEV-protected gasless stablecoin transfers on mainnet"** is possible by design, not yet
  shown. Through the x402 endpoint: the call is the user's own `transferWithAuthorization` to
  the recipient, the payment a second one to the payee, both made by the runner in one
  transaction through builders. No test or live run does exactly that yet. `make
  <net>-verify`'s client transfers only to the payee (its call is the payment), it ran on
  Sepolia, and it has never run on mainnet. A tutorial needs a live third-party transfer first;
  `v0X0-MCP.md`'s `submit` with a signed transfer as payment would make it simpler.
- **Discovery independent of Bazaar** is in `v0X0-MCP.md` (MCP Registry under `app.mailam`,
  Glama, Anthropic's directory); `/.well-known/x402` and `/openapi.json` with
  `x-payment-info` exist now; llms.txt doesn't.
- **Composability follow-up** (escrow, conditional payments, refunds): `auth-capture` (Base
  only, `v0X0-facilitator.md`) is x402's escrow; conditional payment is what the runner
  already is.

## Draft

**Composable x402**

x402 makes a payment one HTTP round trip: a `402` names the price, the client signs, the
server settles and answers. For a fraction of a cent of API access, settled by a facilitator on
a fast L2, that is the right shape. Pay for something on chain with it, something worth more
than the request it rides on, and the shape stops fitting. Not because of a bug: because of
where x402 puts the payment.

### Payment as a side channel

x402 treats a payment as a channel beside the work: verified before, settled after, joined to
the request by the server's say-so. The signed object binds a token, an amount, a recipient and
a window. It binds nothing about the request, nothing about whether the work happened, nothing
about what the work produced. At a tenth of a cent, nobody needs it to. When the payment is the
gas for a $40 swap, or the price of a call that must succeed for the payment to make sense,
each missing binding becomes a failure you can name.

We built a relay that takes x402 for Ethereum transactions: pay in USDC, no ETH, and the
transaction is sent for you. It is x402-compatible, its conformance judged by x402's own SDK.
Building it showed three places where payment and work come apart.

### 1. The payment isn't bound to the request

An EIP-3009 authorization commits to `from`, `to`, `value`, a validity window and a nonce.
Nothing in it says which request it pays for. x402's `payment-identifier` extension adds an id,
but outside the signature: an idempotency key for an honest client, not a binding.

So a server can't tell, from the payment, what it is for. A client that times out and signs
again sends a second, different payment for the same request; nothing in either says they're
the same purchase, so a server that settles both has been paid twice for one thing. Ours
matches pending payments by payer and by the exact call they pay for, and holds the second for
the first: state the server keeps because the payment can't carry it. And a payment seen once
(in a log, by a proxy) is good for any request at the same price to the same recipient.

The fix needs no new field: the 3009 nonce is the client's to choose. Derive it from the
request, and the signature the client already makes binds the two.

### 2. The payment isn't bound to the outcome

x402's success means settled, and settled means a transaction hash. When the payment is made
in the same transaction as the work, so that one can't happen without the other, there is no
hash until that transaction is included: it is signed afresh for each block it competes for.
x402's `settlement_pending` must carry a broadcast hash too. So the server holds the request
until inclusion, a slot or several, while most HTTP clients give up after a few seconds by
default. `maxTimeoutSeconds` says how long to wait; it's up to every client to honour it.

The alternative x402 offers, `upfront`, settles first and works after. The hash comes at once,
and the payment is gone whether or not the work succeeds, with no refund defined. That is the
side channel made explicit.

What fits instead is a receipt: accept the paid request, answer with a handle, settle the
payment and the work together, and report the outcome when there is one.

### 3. The payment isn't bound to execution

`transferWithAuthorization` can be submitted by anyone holding the signature. The payment can
land on its own, in a transaction of its own, whether or not the call it paid for ever runs. A
server can make them atomic only by keeping the signature to itself until it sends both
together, privately.

EIP-3009 has the primitive that binds them: `receiveWithAuthorization`, which only the payee
can execute, inside its own call. A contract that collects its payment that way does its work
and takes its payment as one event. It also composes: a payee contract can split, escrow or
forward within the same call. x402's EVM methods (3009's transfer, Permit2 through x402's proxy,
ERC-7710 delegations) all move funds to the payee from outside it. A `receive` method is a
scheme x402's extensibility already allows for.

### The growth cost: discovery runs through facilitators

The Bazaar, x402's discovery, is built by facilitators from the payments they settle. A
resource is listed by settling through one. The facilitator most clients' Bazaar searches index
from doesn't settle on Ethereum; and a server that settles its own payments atomically, with
the work, has no facilitator to settle through. The main passive discovery channel can't see it.

Discovery should be declared by the resource, not inferred from settlement: x402 already has
the metadata (the `bazaar` extension) and a discovery API any host can serve.

### What correct design looks like

- **A request-bound payment:** the authorization's nonce derived from the request.
- **A settlement receipt:** paid requests answered with a handle; the outcome reported when it
  exists.
- **A `receive` scheme:** payment collected by the payee contract within its own call.
- **Discovery declared, not settled into.**

Each of these is wire-compatible with x402 as it is: a nonce rule, a flow, a scheme, an index.
We'll open them upstream.

### How the relay does it today

The relay settles the x402 payment and the user's transaction in one transaction: a runner
that makes the transfer, makes the call, and reverts unless both happen. It sends only through
builders that drop reverting transactions, privately, so a payment never lands without its
call, and a call that fails costs no one anything. That is why it runs only where builders give
that guarantee: Ethereum today, chains with the same guarantee next.

It speaks x402 v2 `exact` with EIP-3009, as the spec writes it. Where x402 can't express a
guarantee, the relay keeps the guarantee and pays the cost: requests held until inclusion, one
payment per payer in flight, discovery outside the Bazaar.

Composable x402: x402 on the wire, the payment bound to the work.

### Appendix: further practical issues

(From the author's notes.)
