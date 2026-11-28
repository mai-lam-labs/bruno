# 0.7.0: what's left

A holding place for what's still to do. 0.7.0 is x402: several calls in a submission first,
then the facilitator built on them. The rest is carried from 0.6.1.

## Where the work stands (2026-11-28)

Everything built so far is committed and pushed on `next-0.7.0`, with the draft pull request
(#20), whose description is kept up with each push. The last push's run on CI hasn't been
read yet: read it first (`gh pr checks 20`).

- The groundwork is done: steps 1 to 7, several calls in a submission and one planner for
  everything, in both relays, the client and the docs (`39e5147` and before).
- With the facilitator's commit: a quote's breakdown has a `value` part (the ETH the calls
  send; the parts still sum to the amount); a transaction after the one carrying a
  submission's authorizations is estimated with them applied (found by a new flow: a payer
  its own submission delegates, paying by its key's signature, was refused when its calls
  were sent directly); "the calls" in the user pages and plans.
- The hosted facilitator's endpoints are built in both relays, with its docs. Its 36 flow
  tests pass on both (`bruno/tests/test_facilitator.py`), and vectors hold the Worker's
  receipt keys and URL parsing to Python's. `make check` passed on it locally, 572 tests on
  each fork on both relays, before its docs were written; `make npm-test` and `make npm-e2e`
  last ran before the facilitator, and are left to CI.
  - `receipt.py`, `receipt.ts`: the receipt wallet's key, a root on the float's key at
    `m/44'/60'/2'/0/0` hashed with the payout address.
  - `server/facilitator.py`, `facilitator.ts`: what a URL's path names (`route_of`,
    `Terms`); `settlement` (the checks, the fee as the relay's quote for the three
    transfers with the wallet's own at their dearest, the wallet's two authorizations
    signed); `verify` (the settlement made and simulated, nothing sent); `settle`
    (submitted with the payment's digest and held for its outcome; a payment being settled
    or settled is answered by that submission, one not included is tried again); `Split`
    and the `EXTENSION-RESPONSES` header; `supported`; `info`.
  - The relay: `would_take` (a submission checked, priced and simulated, nothing taken),
    `submit(settlement=)`, `x402_deadline(..., blocks)`, `receipt_account`;
    `Tracked.settlement`, `Finished.settlement`, `Queue.settlement(digest)`, a `settlement`
    key in the Worker's stored rows, absent from 0.6.0's. `transfer_refusal` reads why a
    transfer failed.
  - The routes have no chain in the path: a payment names its network.
    `GET /x402/facilitator/<payout>/[terms/]` answers where buyers pay on each chain, as
    JSON for now; `.../supported`; `POST .../verify`, `.../settle`. In the Worker the outer
    handler takes them before a chain's routes, joins each chain's part for the two GETs,
    and sends a payment on to the object of the chain it names
    (`/{chain}/__facilitator/...`).
  - A token is taken if the relay takes it over x402, its key-signed transfer is marked
    `bounded`, and the terms take it: so USDC, as configured today.
  - Docs: `docs/using/facilitator.md` (a merchant's page, in the navigation, its `task`
    the `llms.txt` route), `design/facilitator.md`, and `plans/facilitator.md` cut down to
    what isn't built.

What's next, in order:

1. A stock merchant and a stock buyer run end to end with x402's own packages, in Docker as
   the other user docs' scripts are (`npm-userops/`, `make npm-e2e`). It tests the one
   integration a merchant makes, and whether the reference server hands the merchant the
   fee from `EXTENSION-RESPONSES`, which the merchant's page says and nobody has seen.
2. [The example](#example-be-your-own-x402-facilitator), a merchant running its own.
3. [The pages people see](#the-pages-people-see): the page for a payout address as HTML,
   and the relay's home page, in one style.
4. [Before the release](#before-the-release).

v0.6.1 was never deployed and 0.7 is the next deploy. So the Worker reads a submission's
stored row as 0.6.0 wrote it, and no sponsorship's row in an older shape.

How the work is done:

- Each change is made in Python, its flow test seen failing on the Worker, then ported.
  `make vectors` regenerates what the Worker reads, and `make workerd-stop workerd-start`
  rebuilds the Worker before a test run.
- When a full check runs in the background, record `make check`'s own exit status
  (`make check > log 2>&1; echo "make check exit $?" >> log`): the shell around it reports 0
  whatever the check did.
- When trying one fork by hand, give pytest the fork too:
  `make anvil-start ANVIL_HARDFORK=amsterdam`, then `ANVIL_HARDFORK=amsterdam pytest ...`.
- A test that waits on a request the relay holds can wait for ever. Run one that's being
  written under `timeout -s KILL`, with its output in a file.

## Left of the groundwork

- A call only an EOA may make. It's for EntryPoint v0.9 alone, which reverts if a contract
  with code calls it, and is added with it. Until then a list of calls is sent all directly
  or all in one runner. With it comes splitting a list: a call that must be direct ends a
  runner and is a transaction of its own, and a submission paying by its calls is refused
  where they can't all be one runner. The proofs still each plan their own layout
  (`proof_of`, each proof's `plan`), sharing the steps they're made of.
- Nothing fails locally without the 63rd of headroom a transfer sent directly is given over
  its bound, or without the 63rd of its largest call a Multicall3 group is given. Mainnet's
  USDC, which anvil has, leaves room to spare; only Sepolia's showed the first.
- Nice to have: why a runner or a group failed, from one more simulation run only once it
  has failed. For a runner, with Arachnid's address overridden by code that passes the
  revert data up (the runner's tests do this, `REASONING`). For a group, with its calls
  simulated apart.

## Before the release

- `@mailam/bruno-agentkit` is still on npm and JSR, where published versions can't be
  withdrawn: mark it deprecated on npm and archive it on JSR, by hand, with 2FA, pointing at
  the client and x402. The release notes say so under the breaking changes.
- The verify run rehearsed locally, and the live check on Sepolia.
- This note removed, and the version set.

## Hosted x402 facilitator on Ethereum

Built, as [Where the work stands](#where-the-work-stands-2026-11-28) says. What a merchant
does is in [Accepting x402 payments](docs/using/facilitator.md), why it's built this way in
its [design page](docs/development/design/facilitator.md), and what isn't built in
[its plan](docs/development/plans/facilitator.md#not-built).

Two things about it that only this note says:

- What's said of x402 in those pages was read on 2026-11-28 from the specification, its
  extension specs and the reference TypeScript code by an agent, and not checked further.
  Not read: `settlePayment`, which prepares a settle result before it's encoded for the
  buyer, and so whether the reference server really hands a merchant `extensionResponses`
  untouched. The end-to-end script with x402's own packages would show it.
- `/settle`'s wait of five blocks and its answer `bruno_not_included` were built as I
  proposed them. The user ruled "a reasonable wait and then whatever the HTTP equivalent or
  x402 equivalent is of EAGAIN", and hasn't said yes to this form of it in so many words.

## The pages people see

After the facilitator, before the release: the relay's home page and the facilitator's page
for a payout address, in one style, Mai Làm's. Easy to find from each other and from the
docs, and each explaining what's needed on its own: what Bruno is, what a facilitator URL
is and how to set one up, its terms and how to write them, the fee now.

 * Make the same style as MaiLam generally
 * Cream background, include the Bruno logo on the page 
 * Improve the accounts status display, I think it should be expandable and not loaded by default, so it's AJAX loaded style from a JSON resource on the backend (this reduces the consumption of the landing page)

## Example: Be your own x402 facilitator

Add a runnable merchant-side example at `docs/using/x402-facilitator.md` under Using Bruno
→ Integrations. Link it from [Paying over x402](docs/using/x402.md). Show one paid HTTP
endpoint, a small facilitator adapter wired into the x402 server, and a stock x402 buyer.
The merchant keeps the key for its own receipt wallet and advertises that address as
`payTo`. The adapter uses Bruno's existing Ethereum REST API. The example pays in USDC, and
nothing in the adapter is USDC's alone: the token is the payment's.

1. In `verify`, check the buyer's signed `exact` payment against the merchant's requirements,
   including the token, the amount, the receipt address and the validity window. ABI-encode
   the authorization as a call to the token's `transferWithAuthorization`.
2. Send that call to `POST /ethereum/v1/quote` with the token in `tokens` and the merchant's
   receipt address as `payer`. This prices the buyer's transfer and the merchant's signed
   fee transfer together. Check the live quote against the merchant's fee cap and the
   buyer's payment amount.
3. In `settle`, quote again: the handler has run since `verify`, and the price holds only
   until the quote's `validUntilBlock`. Have the merchant sign that quote's EIP-712
   `typedData`, with a `validBefore` at its deadline's time if the relay takes one nearer
   than the quote's (to check). Send the same call and the fee authorization in
   `payment.transfer` to `POST /ethereum/v1/submissions`. Choose a `maxBlockNumber` within
   the buyer's authorization window.
4. Wait for inclusion at the submission's status URL and return its transaction's hash in
   the buyer-facing x402 settlement. Keep the submission's id by the buyer's nonce, and on a
   retry read that submission's status before signing another fee authorization.

Demonstrate a merchant starting with none of the token and a buyer using a stock client. Check that
the buyer's transfer and the fee transfer land in one transaction, that the merchant
receives the payment less the quoted fee, and that the settlement response names that
transaction. Explain that anyone who has the merchant's fee authorization can submit it on
its own until its `validBefore`. Link to the [direct settlement
design](docs/development/design/runner.md) and [REST request examples](docs/using/rest.md).

## After deploying

- The REST and JSON-RPC pages' sponsorship example is from a local chain, and says so, and
  had `"calls": []` added to its answer by hand. Take it again from Sepolia.
- The REST and JSON-RPC pages' quote and malformed-request examples had `calls` written into
  them by hand. Take them again from Sepolia.

## Open

### Improve LLM discoverability

- A smoke check against a deployed relay: take the x402 page's request as the docs site
  publishes it (`using/x402-request.json`) and post it to the relay, expecting its price and
  not the reference call's. The verify run is the place for it. Locally, a test already holds
  the page's request to the relay's discovery example and posts it to both relays.

### First: CI runs the same checks three times on one commit

A release's commit goes through the whole `check` workflow three times: on the pull request,
on the push to main, and on the tag. The merge is a fast-forward and the tag is main's commit,
so all three are the same commit, and the second and third repeat the first (the checks on
both forks, the packages under npm, pnpm and Bun, the image's build), about ten minutes each.

The checks are to run once for a commit. A push to main or a tag whose commit has already
passed goes straight to what only it does: publishing the site, or the image, the packages
and the GitHub release. Those still wait on that commit having passed, and the publish jobs
stay as isolated as they are. [releasing](docs/development/releasing.md) steps 9 and 11 say
the checks run again, and change with it.

### The gas a sponsorship's quote fills in

Where a transaction comes without its gas, the quote gives it the gas it used in one
simulation of the whole sequence, and a tenth more. Two separate facts make that weaker than a
call's gas, which is `eth_estimateGas`'s:

- Gas used isn't the limit a transaction needs. Refunds are taken off it, and a call is given
  only part of the gas left at each depth, so the limit needed can be well over the gas used.
  An approval followed by a Uniswap swap is quoted, and the swap then fails at its quoted gas.
- A transaction that depends on an earlier one of the same sponsorship can't be given to
  `eth_estimateGas` instead, by the relay or by the client. It takes one transaction, and
  can't be told what the earlier ones did.

The same figure is used for the payment's gas. A sponsorship's calls aren't part of this:
the relay makes those, and gives each its bound or its estimate as it does a submission's.

A client is to state the gas of its own transactions, and the relay simulates what it's given.
The figure the relay fills in is described as a simulation's. A proper estimate for a sequence
would take significant changes, and isn't to be patched with retries or a larger multiplier.

A possible check: the quote runs the sequence once more at the limits it's about
to give, and refuses if it fails there. It would stop a quote being answered whose filled-in
limits already fail against the state it was quoted on. It wouldn't estimate a dependent
sequence, and wouldn't guarantee later acceptance or inclusion, since the chain and its fees
move. It costs one more simulation for each token quoted, and needs a refusal that names the
transaction that must come with its gas.

### Sponsorship

- A quote's deadline can't be given as a count of blocks, so a client that doesn't know the
  head gets the furthest deadline, which costs the most.
- A transaction to sign can't pin its own nonce or fees, and a quote takes only authorizations
  signed already.
- A sponsorship's quote has no breakdown of its price.
- No test shows a sponsorship sitting out a block whose fees its payment doesn't cover.
- Sponsorship isn't ready for an OP-stack chain: the account isn't sent its own transactions'
  data fees.

### The rest

- EntryPoint v0.9 ([erc4337.md](docs/development/plans/erc4337.md)). An op paying by its calls
  reuses the plan a sponsorship is sent as: `handleOps`, then the forwarder's check. The
  forwarder is already in `postpaid_config`. What's left is v0.9's encoding, a call only an
  EOA may make ([above](#left-of-the-groundwork)), simulating the transactions in sequence
  where a helper contract did it, the plan built for either use, and an ERC-7769 receipt
  read from the `handleOps` transaction.
- The funding in a dropping bundle ahead of the runs, which would save a block.
- A Safe by one signature ([safe.md](docs/development/plans/safe.md)).
- Money hygiene: the cold wallet, and persistence for the Python relay's queue and treasury
  snapshot.
- `make burn-in` fails on both relays, as it did before this branch (seed 16136353). Every
  violation is the relay asking its one node about a block the node doesn't have yet. No
  transaction reverts and no status is wrong. Not traced.

### Refactors to consider

- The measurements split into what's measured of a chain, what's reported as wrong with it,
  and what the root shows of the accounts.
- The accounts' state a batch is built from kept apart from what's read for the root alone.
- The Worker's stored forms and its readers of another version's rows moved out of its
  tracked module.
- The Python relay's clock moved out of the relay.
- One type for what's taken, a submission or a sponsorship.

