# 0.6.x: the menu

MCP spins out as a project of its own, likely with a collaborator, using Bruno behind the
scenes. Bruno's public interface is the client's `.d.ts`, OpenAPI and OpenRPC, and x402. 0.6 is
likely several point releases, each taking items from the groups below.

## Next steps (2026-11-04)

Done: the quote takes the call, its gas optional and estimated as it will run; x402's gas
optional likewise; DEPLOYMENT.md's note on tokens whose transfers run more code.

| # | Item | State | Open before building |
|---|---|---|---|
| 1 | Refusals say what to do next | built: messages only | a structured next step in `data`, later if wanted |
| 2 | Binding x402 quotes | deferred | a saving worth the MAC (below) |
| 3 | EIP-712 typed data in the quote | built | |
| 4 | The fee broken down | built | |
| 5 | Funding as a bundle of its own | built | nothing |
| 6 | Leaner sending: the EntryPoint first (ops paying by their fees) | researched | unpaid cases tested first, both relays |
| 7 | EIP-8037 on Amsterdam | covered | nothing to build |
| 8 | Atomic sponsorship | design open | the payment check |

Refusals (built): design/refusals.md the rule; every refusal's message says what failed and
what to do next, the same text on both relays; a failed simulation decodes its revert
(`Error(string)`, `Panic`, the runner's errors, `FailedOp`; else the selector), in the vectors;
unavailable's next step by its category; OpenRPC lists -32015 and the codes each method can
return; SPEC.md: messages aren't the interface, -32700, x402's other refusals; a chain without
x402 answered alike. Then one catalogue (`bruno/refusals.py`, 80-odd templates, exported to
`worker/src/refusals.json` by `make vectors`, the Worker's keys typed from it): neither relay
writes a refusal inline; test_refusals checks every template is used by both and the export is
current. `eth-not-taken` gone: an op paying by its fees on a relay not taking ETH is
`not-accepted` wherever it's asked (estimate, gas price, send), its message saying to pay by
its calls (test with `Setup(eth=False)`). Type errors keep their decoders' wording, by
choice.

Typed data and breakdown (built 2026-11-05): with `payer`, each EIP-3009 token's quote has
`typedData`, the TransferWithAuthorization as EIP-712's JSON (decimal strings; valid for the
quote's blocks and the longest deadline; nonce fresh). Checked in Docker: viem 2.38.3 signs it
as is, ethers 6.15.0 wants `EIP712Domain` left out of `types`; both match eth_account. SPEC.md's
security considerations: the client checks it's the payment; the TS client does
(`checkedTransfer`), signs it as given, and takes its domain where it knows none. Every call
quote has `breakdown: {setup, call, payment, relay}`, each `{gas, amount}`, the parts at the
fee ceiling and price rounded down, the relay's the rest, so they sum to the amount; the
payment its own part. The guides' recorded examples predate both: record them after the deploy.
CI's npm job (the npm test script's old quote) and the Amsterdam failure (anvil's estimate
leaves out an empty authority's state gas, foundry-rs/foundry#18126; the test has a funded case
and a strict expected failure for the empty one) fixed alongside.

Funding (built 2026-11-05): a bundle of its own, its one transaction, to every builder in
either mode, for the next block; signed again each block at the float's same nonce (the fees
of that block alone) until one lands. `funding_timeout_seconds` and the give-up path gone
(the key, if still in a configuration, is ignored, as any unknown key is). Dropped, for the
next batch to fund afresh, when the float can't cover it or its nonce was used by another
transaction. The Worker stores the top-ups, not a signed transaction; a 0.5 funding stored at
the deploy is let go. Tests: blocks built without it, then it lands, the float's nonce used
once; the float's nonce taken elsewhere, funded again. The harness's `send()` lands a funding
before the runs, `land()` through it. Found alongside: a relay that saw a block anvil then
reverted (the mock builder's trial blocks) took anvil's `BlockOutOfRangeError` for a revert
and rejected the submission; it's now an unseen block on both relays, tested with geth's.
The deployer CLI stays on the mempool (`make <network>-deploy`, by hand); `sweep` and
`replenish` are the live tests', not the relay's. Deferred: whether `replenish` goes, once the
live standalone run is funded by the relay's own bundle.

Point releases: 0.6.0 the interface (the quote, 1, 3 and 4: wire changes together, so clients and
the spec change once, before the client drops `-dev`); 0.6.1 the funding (5); 0.6.2 the
EntryPoint without the runner (6); later, atomic sponsorship, and Safe with its trusted targets.
Alongside: releasing.md (`anvil-verify` before the merge); the guides' whole-call example
recorded after the deploy; `scratchpad/ax` deleted.

## DX

- **Refusals say what to do next,** not only what happened (the AX trials' first failure).
- **`gas` optional in the quote.** Given only the call, the relay estimates it as it will run:
  from the runner, with the deployment, the EIP-7702 authorization and its overheads applied,
  and later a Safe deployed first. The quote returns the gas it priced, which the client then
  signs. Pricing needs the estimate anyway, so Bruno has the numbers. A call that reverts is
  refused before anything is signed. To settle: the margin, given that reth's estimate is
  already inflated and state can change before inclusion.
- **The EIP-712 domain in the quote and terms** where a signature is asked for. Maybe the
  typed data verbatim, ready to sign, though it differs between signing mechanisms (EIP-3009,
  a SafeTx, a user op).
- **The fee broken down:** account setup (deployment, authorization), the call's own gas, and
  the relay's fee (Bruno's overhead: the runner, the payment check). Bruno's part is ours to
  bring down by sending more efficiently.

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

## Gas efficiency

- **Leaner sending:** trusted targets called without the runner: the EntryPoint, Multicall3,
  later Safe, and others whose own rules make an unpaid call revert
  ([erc4337.md](docs/development/plans/erc4337.md)).
- **EIP-8037 on Amsterdam:** should already be covered, since Sepolia runs it and the live
  checks pass there. To confirm rather than build.

## Reliability

- **Only `eth_sendBundle` (decided 2026-11-04).** Bruno sends nothing through the public
  mempool: a bundle gives an exact block window, landing in the block it's signed for or not at
  all, so nothing of Bruno's is ever stuck. The funding becomes a bundle of its own. This
  keeps Bruno to chains whose builders take bundles, and sets aside Base through `upfront` x402
  (priorities, Later): a narrower scope, chosen to stay focused.

## Atomic sponsorship

As Titan's sponsored transactions, through the builder: Bruno pays gas into the account that
sends the transaction, the transaction pays the payee, and Bruno checks it was paid, all
atomic through `eth_sendBundle`. Likely the user's transaction sandwiched between two of ours:
a prefix that deposits the gas, a suffix that verifies the payment. Open: how the suffix checks
the payment's delta. Comparing against a balance read before sending fails if someone else pays
the payee in the same block. [sponsorship.md](docs/development/plans/sponsorship.md) offers a
per-submission CREATE2 payment address and a sponsor contract as candidates. Both were added while
writing that plan, not part of the model, and are still to consider.

# Research (2026-11-04)

What each item finds in the code today, what it would take, and what's left to decide. Code
references are Python's; the Worker has the same in its namesake module unless noted.

## Decided: the quote takes the call (2026-11-04)

- `POST /v1/quote` and `postpaid_quote` take one object, the same on both:
  `{call?, callDataLength?, gas?, authorizationList?, authority?, deployment?, tokens, payer?,
  verifyGas?}`. The GET quote goes.
- `call` or `callDataLength`; both, and they must match (invalid params `callDataLength`, its
  `needs` the call's length). `callDataLength` without `call` needs `gas`: priced as today, the
  call at its worst case, nothing simulated.
- `gas` absent: estimated by running the call as it will run (the runner without its payment
  leg, from the sender, with the deployment and authorizations), times 11/10. A call that
  reverts is refused at the quote.
- `authorizationList` (signed) or `authority` (unsigned), not both. With `authority` alone the
  call is estimated only if the account is already delegated; otherwise give `gas` or the
  signed list.
- The answer adds `gas`, the gas priced, for the client to sign.
- `callBytes` becomes `callDataLength`; `postpaid_config`'s `maxCallBytes` becomes
  `maxCallDataLength`; the config key `max_call_bytes` stays.

**Built (uncommitted):** both relays, the vectors (the unpaid runner and the gas inverses), the
spec, the REST and JSON-RPC guides, design/gas.md, the client (`quote(request, tokens,
signing)`, `PaidRequest.gas` optional, `payAndSubmit` signing the quote's gas). Tests: a
delegated call estimated through its signed authorization lands with the gas answered; a call
quoted whole costs less than with generous gas, the same over REST and JSON-RPC; a reverting
call refused at the quote; an undelegated `authority` without `gas` refused.

Then (2026-11-04): the estimating runner is the real runner's builder less the transfer and the
check (the payee's balance read kept, so the call finds the state it will), the transfer priced
from its per-token bound; tests that the bare estimate, headroom off, lands for a call paid by a
transfer and for a delegated call. x402's `gas` optional: estimated for the challenge and again
when paid, a dearer one refused as underpaid (x402 now prices the real call and all its
authorizations, not a stand-in and the first). DEPLOYMENT.md: tokens whose transfers run more
code may not work.

**Deferred:**
- The guides' examples were recorded on Sepolia against 0.5.4: the whole-call example has no
  response yet; record it after the deploy.
- An unsigned `authority`'s call is estimated with its current code: delegated elsewhere
  already, the estimate is of the old delegate.

## Refusals say what to do next

**Today.** Every refusal is `{code, message, data?}` (`server/operations.py` `error_object`).
The machine-readable parts are `field` (with `most` and `needs` on limits), `{token, required}`
on underpaid and no capacity, `{category, reason}` on unsupported and unavailable, and
`revertData` on a failed simulation. No refusal carries a hint: any guidance is free text in
`message` ("see /", "try again", "only v0.8 0x…, v0.7 0x…"). A failed simulation says only
"simulation failed", with the raw revert data. Tests assert codes and data, never messages, so
messages can change without changing the contract.

**Specifics.**
- Messages that say what to do next, the same text on both relays (today some differ: a missing
  REST query parameter is "gas: missing" on Python, "gas: a quantity, as hex" on the Worker).
- A failed simulation's message decoding its revert data where it's known: `Error(string)`,
  `Panic(uint256)`, the runner's three errors. Plus the runner's caveat, said once where it
  matters: the call's `msg.sender` is a one-off contract, not the payer, so a call moving the
  payer's tokens reverts; sign an EIP-3009 transfer instead.
- Found while looking, to fix alongside:
  - An over-long call in a quote is refused as `field: "call"`, but the quote's parameter is
    `callBytes` (`pricing.py:261`).
  - OpenRPC lists no `-32015` (unavailable) on any method, though every chain-reading method can
    return it; `eth_estimateUserOperationGas` doesn't list unsupported, though it can return it.
  - SPEC.md doesn't list `-32700`, nor x402's plain REST errors (400 or 503 with no
    `PAYMENT-RESPONSE`), nor the 404 for a chain without x402. A chain without x402 answers
    differently on each relay.
  - The spec says `eth-not-taken` for estimating a fee-paid op on a relay not taking ETH; the
    code answers `not-accepted` there and uses `eth-not-taken` only when sending.

**To decide.** Messages only, or a structured next step in `data` too (a wire change, and one
more thing for clients to read). Messages alone covered the trials: the models read them.

## `gas` optional in the quote

**Today.**
- The quote takes `gas` and `callBytes` and never simulates. It prices a stand-in call of
  `callBytes` bytes of `0xff` to the payee or the authority (`pricing.py:204`). Only a
  deployment is estimated at quote time.
- The client never estimates: `gas` is whatever the caller passes, and the README's examples
  use `100_000n`. In the trials' code-first runs, the model chose the number.
- At send, the transaction's gas limit comes from the declared `gas` through the gas schedule,
  except with a deployment or a contract payer, when it's one `eth_estimateGas` of the whole
  runner transaction from the sender, with no headroom (`plan.py:242-294`).
- The payer is charged on gas given, not used: the amount is fixed when they sign.
- Elsewhere: user-op limits get 11/10 headroom (`userops.py`), the treasury's estimates 5/4.

**The estimate itself.** reth and geth stop their binary search once within 1.5% of the
answer, so they return at most about 1.5% above the least gas that succeeds (reth's
[`ESTIMATE_GAS_ERROR_RATIO`](https://raw.githubusercontent.com/paradigmxyz/reth/main/crates/rpc/rpc-server-types/src/constants.rs)
is 0.015, "taken from Geth's implementation";
[geth's](https://raw.githubusercontent.com/ethereum/go-ethereum/master/internal/ethapi/api.go)
`estimateGasErrorRatio` the same, commented that wallets add 20–25% anyway). That's a rounding
tolerance, not a safety margin. anvil searches to the exact boundary
([api.rs](https://raw.githubusercontent.com/foundry-rs/foundry/master/crates/anvil/src/eth/api.rs)),
so tests on anvil see the bare minimum. All three take state and block overrides in
`eth_estimateGas`.

**Specifics.**
- The quote and x402's body take the call (`call: {to, data}`) with `gas` optional. Given
  `gas`, it's used as now; without it, the relay estimates. The quote answers the `gas` it
  priced, for the client to sign. `callBytes` becomes the call's own length.
- How to estimate "as it will run": the runner transaction itself, estimated as
  `plan.py` already does, from the sender, with the deployment and authorization. Two things
  stand in the way:
  - The runner checks a payment that isn't signed yet. The estimate needs a runner without
    its payment leg, and the transfer's gas added as it's priced today (measured per token).
  - The runner gives the call all the gas that's left, so the call's own gas is the
    estimate less the runner's frame without a call. The runner's address differs from the one
    that will run (the salt is the sender and block), which a call is unlikely to notice.
  - The simpler alternative is `eth_estimateGas` of the call alone, from a fresh address, with
    overrides for the deployment and the delegation. It's cheaper to build, but it isn't how
    the call runs.
- A call that reverts is refused at the quote, with the decoded revert data (see refusals).
- The client's `gas` becomes optional, and the README stops guessing.

**To decide.** The margin over the estimate: none (the payer signs the bare estimate), a fixed
multiplier as user ops have (11/10), or the client's choice. The runner already adds 1/63 for
the 63/64 rule.

## The EIP-712 domain in the quote

**Today.** Each relay knows its tokens' EIP-3009 domains from configuration (`Token.eip3009`)
and serves them only in x402's requirements (`extra: {name, version}`). The client hard-codes
them per chain in `ALIASES` (`tokens.ts`). The quote answers only `validUntilBlock` and
`quotes: [{token, amount}]`.

**Specifics.**
- Each token's quote carries its domain, where the token takes EIP-3009.
- With `payer` given, the quote could answer the `TransferWithAuthorization` typed data whole:
  the domain, `from` the payer, `to` the payee, `value` the amount, `validBefore` from
  `validUntilBlock`, a fresh nonce. Every field is known to the relay, so the client signs it
  as is. The other mechanisms each have their own typed data: a user op's hash (ERC-4337's),
  later a SafeTx. Each would answer its own when it exists.

**To decide.** The domain alone, or the typed data whole. The whole is what removes guessing;
it's also more for the relay to answer and the spec to state.

## The fee broken down

**Today.** One amount per token: everything summed into the transaction's gas limit, at the fee
ceiling (base fee risen for the quote's blocks, tip doubled), plus the OP-stack data fee, plus
Bruno's fee (`max(tip, min_fee_per_gas)` × gas limit, at least `min_fee`), converted at the
pool price and multiplied by the margins (`pricing.py` `charge`). The gas limit's parts are
known separately while it's computed:
- account setup: the deployment's measured gas; each authorization's intrinsic and delegation
  gas;
- the call: its gas, plus the 1/63, and its calldata;
- the payment: the EIP-3009 transfer's measured gas and the signature check (`verifyGas`);
- the relay's own: the intrinsic base, the deployer's overhead, the runner's balance reads and
  opcodes.

**Specifics.** Parts in the token: account setup, the call, and Bruno's (its overhead and its
fee). Each priced at the same fee per gas and price, summing exactly to the amount, the rounding
in Bruno's part.

**To decide.** Where the payment's transfer and signature check go: Bruno's overhead, or the
payer's (it grows with a smart wallet's signature, which is the payer's choice). And whether
Bruno's fee and Bruno's gas are one part or two.

## Leaner sending

**Today.** Every transaction is the runner through the deployer, and that's hard-wired: `Plan`
holds a runner; the transaction is always `to: DEPLOYER, data: runner.initcode`; a simulation
answering nothing counts as a revert (`plan.py` `_revert`, since a deployer that answers nothing
ran no runner); measurements mark a chain unavailable without the deployer; the treasury sizes
the largest transaction against it. User ops are `handleOps` called by the runner, the payee as
beneficiary. A fee-paid op's payment is already checked before each signing (`FeePayment.at`),
off chain. Nothing of Multicall3 or Safe is vendored. Preflight checks no EntryPoint code.

**Specifics.**
- A plan is either a runner or a direct call to a trusted target, from the same concurrency
  account, in the same bundles.
- First target, the EntryPoint, for ops paying by their fees: a failed validation reverts
  `handleOps` and the bundle drops it; a failed execution still pays from the deposit. The
  check it needs exists. It saves the deployer's overhead (32,115 gas on Prague, 195,715 on
  Amsterdam), the runner's own and its initcode's calldata, on every such op.
- Preflight and measurements check each target's code at its canonical address.
- Tested unpaid cases first, on both relays.
- Multicall3 and Safe follow with the Safe work; nothing to build for them before it.

## EIP-8037 on Amsterdam

**Confirmed covered.** The gas schedule is measured, not configured (`forks.py` `schedule_of`),
its Amsterdam figures from Sepolia (`make gas-probe`, 2026-10-19). `make check` runs on both
Prague and Amsterdam. User-op simulations run at 2^24 gas, so state gas stays out of the
reservoir. What's left is upstream: the runner's account costs 183,600 to create and gets
nothing back when it self-destructs in the same transaction
([upstream.md](docs/development/upstream.md), our Magicians post). If EIP-8037's refunds
change, the schedule is measured again; no code to write before then.

## Stuck transactions

**Today.** A running relay sends one kind of transaction through the public mempool: the
float's funding transaction. Submissions go out only as bundles signed for one block, so a
missed block needs no cancelling. The treasury's runs are atomic bundles for five blocks.
The deployer CLI (by hand, `make <network>-deploy`), and `sweep` and `replenish` (Python only,
used by the live tests) use the mempool and wait for a receipt, with no retry.

What can go wrong with the funding:
1. **The known gap.** Given up on after `funding_timeout_seconds` but still pending, it holds
   the float's nonce. The next is signed at the same nonce (read at `latest`) with no fee bump,
   and a node refuses it as an underpriced replacement. Each batch repeats this until the old
   one lands or leaves the pool (`batches.py:233-254`, `378-380`).
2. **A refusal taken as not sent.** Any JSON-RPC error on the first send clears it, though only
   one node was asked and another may hold it.
3. **Errors swallowed while resending.** Each block resends the same bytes and ignores every
   JSON-RPC error, so "nonce too low" or "underpriced" are silent until the timeout.
4. **Python forgets it on restart** (its queue and treasury snapshot too, a known gap).
5. **One that lands after it was given up on** is never reconciled; its top-ups show only in the
   next survey's balances.
6. **The treasury's bundles still on offer** for up to five blocks when the next run signs at the
   same payee nonces. At most one set lands, so this is benign as far as found.

No test covers the timeout, an underpriced replacement, a reverted funding or a treasury bundle
that doesn't land. The Faults harness has no "underpriced" answer and no way to hold a
transaction pending.

**Decided: fund inside the bundle.** The alternative, kept here for the record:
- **Keep the mempool, never abandon a nonce.** Read the float's nonce at `pending` too. A
  funding past its timeout is replaced at the same nonce with fees bumped as replacement rules
  require (the bump is each client's pool rule, to check per client), until one lands. And a
  cancellation (a self-send) only if the float's balance can't cover the replacement.
- **Fund inside the bundle.** `bundle.py` already builds a dropping bundle with the funding
  first (`bundle_params`), but the batches always pass none and send the funding by mempool.
  Inside the bundle, the funding lands only with the block's runs, nothing waits in a pool,
  and the float's nonce is used once a block or not at all. The costs: standalone-mode builders
  can't take it (their accounts must be funded beforehand), and the funding goes in every
  block's bundle until it lands. Why the mempool was chosen isn't recorded in the design
  pages.

**What follows from it.**
- The funding is a bundle of its own, the one transaction, sent to every builder in either
  mode, signed again each block at the float's nonce until it lands. Only one copy can land,
  the nonce being one. `funding_timeout_seconds` and the give-up path go, and with them gaps 1,
  2, 3 and 5; Python's restart (4) loses only a funding not yet landed, which is signed again.
- Its max fee needs to last only its block, as the runs' do, not the timeout's blocks.
- Why not inside the runs' bundles: a standalone builder takes a bundle a submission, and the
  float's nonce can be in only one of them; Sepolia's only builder is standalone. On its own,
  the funding works the same in both modes. Putting it ahead of the runs in a dropping bundle
  (which `bundle_params` can already do) would save a block, an optimisation for later.
- Runs awaiting the funding go out in the block after it lands, as they wait for it now.
- The deployer CLI, `sweep` and `replenish` stay on the mempool, waiting for a receipt: none
  is the relay's (the deployer is run by hand, the other two by the live tests).
- Tests: a funding whose bundle misses blocks, then lands; the float's nonce used once.

## Atomic sponsorship

**What builders already do.** Titan: a bundle failing with `LackOfFundForGasLimit` gets Titan's
ETH automatically, provided "the bundle must of course increase the builder balance"
([Titan](https://docs.titanbuilder.xyz/api/eth_sendbundle),
[Tech Teatime](https://titanbuilder.substack.com/p/titan-tech-teatime-1)). There's no request
field, and the check is the builder's own simulation, off chain. A
[Flashbots proposal](https://collective.flashbots.net/t/proposal-builder-funded-gas-sponsorship-for-bundles/5820)
(July 2026, unanswered) would bring it to rbuilder. Bruno's version works through any builder
honouring atomic bundles, and is paid in tokens.

**What atomicity guarantees.** The specs promise a bundle executes in order, all or nothing
([Flashbots](https://docs.flashbots.net/flashbots-auction/advanced/understanding-bundles),
[BuilderNet](https://buildernet.org/docs/api)). None says its transactions are contiguous in the
block. rbuilder's code keeps them contiguous, adding only its refund payout after them
([order_commit.rs](https://raw.githubusercontent.com/flashbots/rbuilder/develop/crates/rbuilder/src/building/order_commit.rs)).
Transient storage can't carry a value from the prefix to the suffix: it's discarded at the end
of each transaction ([EIP-1153](https://eips.ethereum.org/EIPS/eip-1153)).

**The delta check.** The trouble with the payee's balance is that Bruno's own runs pay it, in
the same bundle and the same block. Ways to check this payment alone:
- **Pay the concurrency account sending the bundle** (an inference, not a published design).
  The user's transaction pays the token to the account that sends the prefix and suffix. That
  account is Bruno's, named in the quote and so fixed when the user signs, held for this
  submission until it lands or expires, and nobody else pays it (a gift only helps). The suffix, from the same account, checks its token balance is at least what it
  held at the parent block plus the amount, a constant signed into it each block. No contract,
  no storage of Bruno's. Tokens only: its ETH moves with the gas it spends. The treasury sweeps
  the account as it does the payee.
- **An absolute check** is the published pattern
  ([FlashbotsCheckAndSend](https://raw.githubusercontent.com/kendricktan/searcher-sponsored-tx/main/contracts/FlashbotsCheckAndSend.sol)):
  the last transaction checks a state against an expected value, with no "before" carried
  across. The bullet above is this pattern, on an account only Bruno's bundle touches.
- **The two candidates in sponsorship.md** (a fresh CREATE2 address per submission, a sponsor
  contract recording the balance) both write a new storage slot each time: a token balance or
  the contract's own. On Amsterdam a new slot is 107,920 gas (`forks.py` `storage_set`), against
  10,000 for changing one. That's an argument against both. Paying the concurrency account
  costs 10,000 if it keeps a balance of each token it takes.

**The prefix.** A plain ETH transfer from the concurrency account to the user's account, of
the transaction's gas limit × max fee, as the EVM checks before running it. Not the funding
transaction's creation, which pays EIP-8037's account cost.

**To decide.** The check (the concurrency account looks simplest); fees fixed at signing for
the user's transaction (sponsorship.md's `lasting_fees` point stands); and only chains with
atomic bundles.

------------------------

Here are six directions, each a coherent 0.6 on its own. All of them draw on what's already in the plans. Sizes are rough, relative to 0.5.x releases.

## A. The interface, settled: small and fast
- **The client's API settled,** its `.d.ts` written as the reference a model reads; OpenAPI and OpenRPC aligned with it; published without `-dev`.
- **The relay's AX fixes:**
  - refusals that say what to do next;
  - the call's gas estimated in the quote;
  - the token's EIP-712 domain in the quote and terms;
  - a fee broken down into gas and Bruno's part.
- **Code-first docs pages,** each with a tested script.
- **Unlocks:** a stable surface agents and developers can build on, in time for the mid-November listings push.
- **Risk:** low, but it's a commitment. Once it's not `-dev`, the API is a promise.

## B. A Safe by one signature: feature-led
- **A, plus** the Safe route: Safe's contracts vendored, deployed on first use, the SafeTx with a payment leg, on REST, JSON-RPC and the client. Existing and n-of-m Safes too.
- **Unlocks:** "act as my account" for every wallet that signs typed data, which is nearly all of them, with no EIP-7702. It's the headline UX/DX story.
- **Risk:** medium. There's the four-item verification list in safe.md first, and Safe's address conventions to get right.

## C. Leaner sending: the core model
- **Trusted targets:** the EntryPoint (ops paying by fees), Safe and Multicall3, called without the runner. That saves 32k gas a transaction on Prague and 196k on Amsterdam.
- **Readiness for EIP-8037 on Amsterdam,** whose state cost is what makes the runner expensive.
- **The funding-replacement known gap**, a given-up funding transaction blocking the next one, since it's in the same code.
- **Unlocks:** much cheaper transactions once Amsterdam ships. It only matters for user ops (and for Safes if B exists), until Amsterdam's date is known.
- **Risk:** highest. It changes how payment is proven, and its unpaid cases need testing first on both relays.

## D. Atomic sponsorship: a new integration class
- **A signed transaction as a new kind of submission:** funding, the user's own transaction, and a check of the payment, in one atomic bundle. The payment goes to a fresh CREATE2 address per submission.
- **Client support.**
- **Unlocks:** agents' own keys, and infrastructure wallets (Privy, Turnkey, Dynamic, Openfort), acting as themselves, with any call and no setup. A clean "Titan, but general" story.
- **Risk:** medium. It's new pricing (fees fixed at signing, leftover ETH) and only works where bundles are atomic (Ethereum, Sepolia).

## E. Reaching Base: the L2 track
- **`upfront` x402, with a choice of settler.**
- **OP-stack L1-fee pricing.**
- **Deployments on Base Sepolia, then Base.**
- **Unlocks:** where agents and smart wallets actually are.
- **Risk:** large. And Base is where the vendors already sponsor gas, so Bruno's advantage is thinnest there.

## F. Money hygiene: small, the relay's own funds
- **The cold wallet.**
- **The funding-replacement gap.**
- **Persistence for the Python relay's queue and treasury snapshot.**
- **Some parity leftovers.**
- **Unlocks:** less exposure on the hot keys, and fewer ways for Bruno to lose money.
- **Risk:** low. It's too small to be a release on its own; it rides along with any of the others.

## How they combine

| | Size | Depends on |
|---|---|---|
| A, the interface | small | nothing |
| B, Safe | medium | A, to expose it well; gains from C |
| C, leaner sending | medium, risky | nothing; matters most with B and Amsterdam |
| D, sponsorship | medium | nothing |
| E, Base | large | nothing |
| F, hygiene | small | nothing |

**My recommendation: 0.6 = A + F, then 0.7 = B, with C's Safe part folded in.**
- **A + F** fits the mid-November push. It turns the trials' findings into the relay and the client while they're fresh, and deals with the own-funds items under the standing priority rule.
- **B is the bigger story,** and better told on a settled interface. Building C alongside it means Safe transactions never use the runner at all.
- **D and E stay as later tracks.** D is what to do next if the wallets that sign raw transactions turn out to matter. E depends on whether the agents on Base are a market Bruno can win.

If you'd rather lead with the headline, **A + B in one release** is the alternative: bigger, but with a story to tell at launch.