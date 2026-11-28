# 0.7.0: what's left

A holding place for what's still to do. 0.7.0 is x402: several calls in a submission first,
then the facilitator built on them. The rest is carried from 0.6.1.

## Harry Notes

 * Our /verify interface for the merchant x402 facilitator, we should return some kind of fee estimate, it serves similar to our quote interface.
 * Investigate how to require a minimum net payout or maximum Bruno cost, perhaps additional items in the URL?

## Where the work stands (2026-11-28)

Committed and pushed on `next-0.7.0`, with a draft pull request (#20) whose description says
what's done and what's next and is kept up with each push: steps 1 to 5 below, and the
AgentKit provider's removal. `make check` passed on it locally. `make npm-test` and
`make npm-e2e` are left to the pull request's `npm` job.

Committed and not pushed: steps 6 and 7 in both relays, the client and the docs (below),
which ends the groundwork. `make check` passed on it, 530 tests on each fork on both relays,
and `make npm-test` and `make npm-e2e`. The pull request's description doesn't have them
yet. v0.6.1 was never deployed and 0.7 is the next deploy, so the Worker reads no stored
row of 0.6.1's: a submission's row as 0.6.0 stored it, and nothing of a sponsorship's.

When a full check runs in the background, record `make check`'s own exit status
(`make check > log 2>&1; echo "make check exit $?" >> log`): the shell around it reports 0
whatever the check did.

The steps, in order, and where each is:

1. Done. Revert decoding and account reads out of the planning module.
2. Done. `calls` on the wire, in the id and in the Worker's stored rows.
3. Done. A draft carries its calls as planned
   (`PlannedCall`: the call, its gas, whether the relay bounded it), decided in one place
   (`plan.planned_call`, `call_bound`, `bound_reads`, and `bounds_of` for a quote). USDC's
   key-signed transfer is marked `bounded` in `targets.json`. A bounded call needs no stated
   gas and isn't run to be quoted; any other without gas is refused, `call-no-gas`. Twenty
   vectors hold the Worker to Python.
4. Done. A submission, a quote and an x402 request take up to
   eight calls (`MAX_CALLS`), made in order, all or none.
   - The runner makes them in order (`in_call_runner(calls, ...)`, `PlannedCall` in the
     runner's module). A bounded call is given exactly its gas, a cap among the runner's
     overhead. Any other is given what's left less the runner's rest and the later unbounded
     calls' needs, and the runner reverts `GasExhausted` unless that's at least its stated
     gas. A deployment and a contract's transfer run before the calls and keep back every
     unbounded call's need; a key's transfer runs after.
   - Calls all to listed targets, paid by a signed transfer and deploying nothing, are sent
     directly: a transaction each, the first carrying the authorizations, then the
     transfer's, one bundle. Grouping waits for step 7.
   - One call may state no gas and be estimated, the others at theirs; a second is refused,
     `call-no-gas`. `max_gas` and `max_call_bytes` hold for each call and for all together
     (a refusal names the call, or `calls`).
   - An op's `handleOps` is exactly one call. A call's `value` is still refused (step 5), and
     a sponsorship still takes one call (step 6).
   - The `sole` accessors are gone from both relays.
   Tests: `test_runner.py` (order; a call that burns its gas leaving the next its own, seen
   failing with the later calls' reserve taken out); `test_x402.py`, both relays (two listed
   calls as three transactions of a bundle, a mixed pair in one runner, a second call without
   gas refused); the `severalCalls` vectors hold the Worker's runners and direct plans to
   Python's. Docs: SPEC.md, design/runner.md, the REST, JSON-RPC, x402 and using pages, and
   x402's own description.
   Also done: one REST flow (`test_several_calls_are_quoted_and_sent_over_rest`), and a scan
   of the docs for what still said one call, its wrong statements fixed: SPEC.md (the id's
   encoding, `transactionHashes`, the limits, the one estimate, the security note), the
   runner, gas, bundling, x402 and risks design pages, the REST, JSON-RPC, x402 and using
   pages, both READMEs, the docs' home, networks.md, DEPLOYMENT.md, the routes' descriptions
   and the relays' home page.
   Left of it:
   - Smaller wording the scan listed and nothing fixed: "the call" where "the calls" is
     meant in parts of rest.md, x402.md and the client's README, and in the plans
     (erc4337.md, agent-standards.md, MCP.md's order of the transfer and the call, erc6492.md).
   - The pull request's description doesn't say several calls yet.
5. Done. Every runner through Arachnid's deployer, the runner sending ETH with a call.
   - The salt, the user's ruling of 2026-11-28: never zero, and not guessable. No two of
     Bruno's transactions run from the same address, and an adversary can't work out a
     runner's address before its transaction is seen. So it's a keyed hash, nothing stored
     and nothing random: `keccak256(key ‖ submission id ‖ target block)`, the key a 32-byte
     private key derived from the float's on a path of its own (the concurrency accounts are
     `m/44'/60'/0'/0/i`), the same in both relays, with vectors. A plan with no submission
     behind it (a quote, an estimate) uses a stand-in. A transaction's gas limit prices the
     salt's 32 bytes as nonzero whatever they are, so the limit and the charge don't move
     with it. The simulation of a transaction as it'll be signed carries the salt it's
     signed with.
   - What a salt can't do: once a runner's transaction is public, anyone can create the same
     runner at the same address again, since Arachnid takes the salt from calldata. It's the
     same calls and the same payment check, so it pays the payee again or reverts. Only a
     deployer of Bruno's own could stop that, and that's the one that goes. SPEC.md says
     nothing may treat a runner's address as authority.
   - In two parts: first the deployer swapped with nothing else changed (every test that
     reads a runner's revert data changes, nine test files; the unit-short refusal in the
     verify run among them), then a call's `value` (the transaction carries it, Arachnid
     passes it to the runner, the runner sends it with the call, what's left returns to the
     sender; priced into the amount and under `within_cost`; a call with value always goes
     through a runner).
   - Goes with it: `bruno/deployer.py`'s own deployer and `make <net>-deployer-deploy`,
     preflight's check of it (Arachnid's is checked instead), and the pages on it.
   Part one is done and committed: every runner through Arachnid's deployer with the keyed
   salt, in both relays (`deployer.py`, `deployer.ts`; `Draft.salt`, `make_plan(salt=)`,
   `make_plans(salts=)`). A runner that fails is refused `revert-empty`, `revertData: "0x"`;
   the runner's own tests read a failure's reason through `REASONING` in
   `tests/deployments.py`, a deployer put at Arachnid's address by a state override.
   Part two, a call's `value`, is done in both relays:
   - The runner sends each call its value (`push(call.value)` in `_client_call`), its charge
     and a new account's in R; a bounded call with a value is a `RunnerError`. The runner's
     transaction carries the calls' values together (`_sent(draft)`, `runner_call(...,
     value)`), sized with it.
   - Already there from sponsorships and reused as it is: a plan's cost and so its charge
     include what its transactions send (`CostBasis.value`); a simulation gives the sender
     that balance; a concurrency account is funded for it.
   - New: an estimate of a transaction sending ETH gives its sender the balance by a state
     override (`_estimate_call`, `estimateCall`); the cap holds for every plan
     (`within_cost(..., sponsored=)` at a quote and when taken), a submission over it refused
     `cost-over`, naming `calls` (a sponsorship's is still `sponsored-over`); a call with a
     value is never sent directly (`targets.direct` already said so).
   - Tests: on-chain, a runner sends a call its value to a new account at the gas limit the
     bounds give (`test_runner.py`); over x402, a call sending ETH paid for in USDC lands,
     dearer than the same call sending none, and a call sending an ether is refused
     `cost-over` (`test_x402.py`; passing on the Python relay, the Worker's to run); vectors
     `valueRunners` hold the Worker's runners to Python's, and `refusals` has a bounded call's
     value.
   - A transaction sending ETH is charged for it up front on Amsterdam (`tx_value`, EIP-2780),
     so the deployer's `transaction_gas` and `runner_gas_within` take `sending`; found by the
     Amsterdam run failing the three value tests with `GasExhausted` where Prague passed.
     When trying one fork by hand, give pytest the fork too:
     `make anvil-start ANVIL_HARDFORK=amsterdam`, then `ANVIL_HARDFORK=amsterdam pytest ...`.
   - `make check` passed on it, 524 tests on each fork on both relays.
   - Left: a quote's breakdown doesn't show the
     value apart (it's in the relay's part); `sponsored-over` and `cost-over` are one cap
     under two names, to settle with step 6; docs (SPEC.md's "`value` is zero if given" and
     Semantics, the REST and JSON-RPC pages, design/runner.md and gas.md, the pull request).
6. and 7. Next, as one piece of work: one planner for everything, agreed with the user on
   2026-11-28 as below. The user's aim, in their words:
   "everything goes through a single code path, and that code path decides the best mechanism
   given the capabilities needed or privileges permitted (e.g. whitelisted function of a
   specific contract means it can be called direct, if there are multiple in a row we can
   batch them) and how the possible sequence of operations can be turned into real things
   that are included in a real MEV bundle... including the fee check so the entire thing is
   fully atomic".
   - What's planned is one shape: the relay's `calls`, then an account's signed
     `transactions` (a sponsorship's, none otherwise), then how the payment is known.
   - Each call is one of three kinds, decided in one place: it must be sent directly (only
     an EOA may call it; none is listed yet, EntryPoint v0.9's `handleOps` would be the
     first), it may be sent directly (a listed target's function, sending no ETH), or it
     must be isolated (anything else, any call into an account's code, ETH sent to code).
   - The planner turns the calls into transactions, in order. A call that must be isolated
     opens a runner, and the calls beside it that may go either way join that runner, which
     is cheaper than a transaction each. A call that must be direct ends a runner and is a
     transaction of its own. With nothing to isolate, the calls are sent directly, a run of
     consecutive bounded ones as one Multicall3 `aggregate3` (step 7), any other a
     transaction each.
   - The payment: measured by the runner only where everything is one runner (a submission
     that pays by its calls needs that, and is refused where it can't be). A signed
     transfer goes inside that one runner, else is the last transaction. A sponsorship's
     forwarder check is always the last transaction, and its runners check nothing. More
     than one transaction is a bundle, whole or not at all.
   - A sponsorship is then: its `calls`, with one more the relay adds, the ETH its
     transactions need sent to the account (a plain send where the account has no code and
     the request doesn't delegate it, else a value call from a runner), then the account's
     transactions, then the forwarder's check. Its direct call into the account's code goes.
   - Its two requests take `calls` in place of `call` and `gas`, a breaking change the user
     confirmed ("we have to do it"): the client's `sponsor`, `npm-userops/sponsor-7702.ts`.
   - The proof classes become how the payment is known, chosen inside the planner
     (`proof_of` and each proof's `plan` fold into it).
   - One cap, one reason ("one cap, which is the entire cost, one number"): `cost-over` for
     a sponsorship too, `sponsored-over` gone.
   - A mixed list goes in one runner. The user: fine, "it's internal logic so can be changed
     later".
   - Funding an account that has code: no fixed gas. The user: "we should estimate the gas,
     see how much it costs, if it has code it would go through the runner anyway for
     isolation unless it's one of the whitelisted contract+method pairs". So it's a step of
     the runner given the gas left over, as a deployment and a contract's transfer are, its
     transaction estimated as it will run when it's quoted and when it's taken. Nothing new
     on the wire for it.
   - Open: Multicall3's overhead to measure per fork.
   Step 6 is done in both relays, uncommitted: a sponsorship is drafted, planned, estimated
   and priced by the code a submission is.
   - `Sponsorship.calls` where `call` and `gas` were, to any target (`owned` no longer asks
     that a call be to the account); its id encodes the calls as a submission's does
     (`callsEncoded` in the Worker); `transfer` and `to_deploy` answer none so a draft reads
     it as it reads a submission.
   - `plan.py`, `plan.ts`: `Funding` (the ETH the account is sent: `plain`, or through a
     runner), `Draft.funding`, `Draft.transactions`, `Draft.checking_gas`;
     `CheckedByForwarder` is a `DraftedProof` whose `plan` is `_forwarder_checked`; a
     sponsorship is drafted in `make_plans` (`_sponsored_draft`, `sponsored`) and planned and
     estimated by `plan_drafts`; shared `_planned_calls`, `_runner_step` (a runner checking
     nothing, the funding its first call, given the gas left over, its step `estimated`) and
     `_direct_steps`. A sponsorship's own calldata is priced at its dearest. `sandwich`,
     `_sandwiched` and `funding_overhead` are gone.
   - `sponsoring.py`, `sponsoring.ts`: the quote prices its calls by `Pricing.priced`, drafts
     with `sponsored`, simulates the plan's own steps then the account's transactions for
     their gas, and plans the result with `plan_drafts`. Its answer has each call's gas.
   - The wire: the two requests take `calls` (none allowed, eight at most), and the quote
     answers `calls: [{gas}]`, always there. A sponsorship's calls are held to `max_gas` and
     `max_call_bytes` as a submission's are (`within_limits` in `quote_sponsorship` and
     `sponsor`; a test saw a call over the size limit taken before).
   - One cap, one reason: `cost_within(basis, ceiling, max_gas, field)`, `cost-over` naming
     `calls` or `transactions`. `sponsored-over` and `sponsored-no-gas` are gone.
   - The Worker stores a sponsorship's calls as a submission's (`callToJson`).
   - The client: `quoteSponsorship(account, transactions, tokens, {calls, ...})`, its answer's
     `calls: [{gas}]`; `submitSponsorship({calls, ...})`; `npm-userops/sponsor-7702.ts` on
     them. `make npm-e2e` hasn't run on it.
   - Tests: `test_sponsorship.py` on both relays (an account delegated already is sent its
     ETH by a runner and lands; a delegate's payment as a call; a call given no gas refused
     `call-no-gas`; a call too long refused `call-too-long`; the cap `cost-over`); the
     sponsorship vectors drafted by `sponsored`, each plan with its account's reads and each
     step's `estimated`; the client's tests read what it sends with the Worker's parsers.
   - Docs: SPEC.md's Sponsorship, Semantics and Interface; the REST, JSON-RPC, sponsorship
     and EIP-7702 pages; the client's README; design/sponsorship.md (why a concurrency
     account never calls an account's code), bundling, risks, queue and runner.
   Not built, by the user's ruling of 2026-11-28: a kind for a function only an EOA may call.
   It's for EntryPoint v0.9 alone ("another one of those whitelisted contracts where we need
   explicit control over how we interact with it because it reverts if a contract with code
   invokes it"), and is added with it. Until then a list is sent all directly or all in one
   runner.
   Left of step 6:
   - The REST and JSON-RPC pages' sponsorship example had `"calls": []` added by hand.
   Step 7 is done in both relays, uncommitted: with nothing to isolate, a run of consecutive
   bounded calls, a key's payment transfer the last of them, is one Multicall3 `aggregate3`.
   - `multicall.py`, `multicall.ts`: `aggregate3(calls)`, `own_gas(calls, schedule)`,
     `OWN_GAS` (Multicall3's own gas by number of calls, 0 to 9, for calls of a bounded
     call's 292 bytes answering nothing), `CODE_HASH`. With each target's access taken out
     by the schedule, the measured figures are the same on Prague and Amsterdam, so there's
     one table. It isn't a line (memory), which is why it's a table.
   - `_direct_steps(chain_id, draft, calls, ...)` groups; `_transfer_planned` gives it the
     calls and a bounded transfer as one more bounded call. A group's limit: its intrinsic
     gas, `own_gas`, the calls' needs, and a 63rd of the largest. A sponsorship's direct
     calls group the same way.
   - A chain is measured to have Multicall3's own code, as it is the deployer's: without it
     the chain is unavailable, `no-multicall3`.
   - Tests: `test_multicall.py` (the table to the gas by a probe, on each fork; two transfers
     and their payment landing as one transaction at its limit, its limit and its gas used
     under the same three sent apart); over x402 and REST on both relays, bounded calls and
     their payment land as one transaction to Multicall3; a chain without Multicall3 is
     unavailable on both relays; the `severalCalls` vectors hold the Worker's groups, their
     calldata and gas, to Python's, to one target and to several.
   - Left: nothing fails locally without the group's 63rd of headroom, as with a direct
     transfer's (below, The rest): anvil's USDC leaves room to spare.
   Order of work: the three kinds and the planner in Python, submissions going through it
   with nothing changed in what they're sent as (the vectors and tests as they are say so);
   then a sponsorship drafted through it, its wire on `calls`; then grouping; the Worker
   after each; the client, the scripts and the docs last.

How the two relays are kept in step while this is built: each change is made in Python, its
flow test seen failing on the Worker, then ported; `make vectors` regenerates what the Worker
reads; `make workerd-stop workerd-start` rebuilds the Worker before a test run.

## After deploying

- The REST and JSON-RPC pages' sponsorship example is from a local chain, and says so. Take it
  again from Sepolia.
- The REST and JSON-RPC pages' quote and malformed-request examples had `calls` written into
  them by hand. Take them again from Sepolia.

## Open

### Deprecate the AgentKit package

The provider is gone from this branch. `@mailam/bruno-agentkit` is still on npm and JSR,
where published versions can't be withdrawn: mark it deprecated on npm and archive it on
JSR, by hand, with 2FA, pointing at the client and x402. The release notes say so under the
breaking changes.

### Improve LLM discoverability

- `llms.txt` has no route for "I want to accept x402 payments on Ethereum" yet. Give the
  facilitator's page that `task` in its front matter once the page exists.
- A smoke check against a deployed relay: take the x402 page's request as the docs site
  publishes it (`using/x402-request.json`) and post it to the relay, expecting its price and
  not the reference call's. The verify run is the place for it. Locally, a test already holds
  the page's request to the relay's discovery example and posts it to both relays.

### Make Bruno page better

 * Make the same style as MaiLam generally
 * Cream background, include the Bruno logo on the page 
 * Improve the accounts status display, I think it should be expandable and not loaded by default, so it's AJAX loaded style from a JSON resource on the backend (this reduces the consumption of the landing page)

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

The same figure is used for the payment's gas and for the gas the account's code is given,
which a client can't always state.

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
  forwarder is already in `postpaid_config`. What's left is v0.9's encoding, simulating the
  transactions in sequence where a helper contract did it, the plan built for either use, and
  an ERC-7769 receipt read from the `handleOps` transaction.
- The funding in a dropping bundle ahead of the runs, which would save a block.
- A Safe by one signature ([safe.md](docs/development/plans/safe.md)).
- Money hygiene: the cold wallet, and persistence for the Python relay's queue and treasury
  snapshot.
- `make burn-in` fails on both relays, as it did before this branch (seed 16136353). Every
  violation is the relay asking its one node about a block the node doesn't have yet. No
  transaction reverts and no status is wrong. Not traced.
- The headroom a transfer sent directly is given (a 63rd over its bound) has no local test
  that fails without it. Mainnet's USDC, which anvil has, leaves 267 gas to spare; only
  Sepolia's showed it.

### Refactors to consider

- The measurements split into what's measured of a chain, what's reported as wrong with it,
  and what the root shows of the accounts.
- The accounts' state a batch is built from kept apart from what's read for the root alone.
- The Worker's stored forms and its readers of another version's rows moved out of its
  tracked module.
- The Python relay's clock moved out of the relay.
- One type for what's taken, a submission or a sponsorship.

## Several calls in a submission

A submission takes `calls`, a list of `{to, data, value?, gas?}` made in order, all or none,
in place of `call` and `gas`. The change reaches the quote, the submission, the x402 request, JSON-RPC,
the submission's id, both relays, the vectors, the TypeScript client and SPEC.md. `max_gas`
and `max_call_bytes` apply to the calls together.

A quote's entry is the call, or `{callDataLength, gas}` for one not built yet. Its answer
gives each call's gas, and its breakdown keeps one part for the calls together.

A sponsorship is the same thing with two additions, and is planned by the same code. Its two
methods stay, as two ways into that code which keep the requests' types apart. It takes
`calls` as a submission does, any number of them. After them come the account's own signed
transactions, and then the relay's call to the forwarder, which checks the payment. The ETH
the account's transactions need is one more call the relay makes, to the account. `sandwich`
and the direct plan build their transactions separately now, and a sponsorship's one call is
sent directly whatever it calls: nothing requires its `account` to be the client's own, so a
concurrency account can be made to call any contract with any data. Under one path that call
is classified as any other is.

A concurrency account calls nothing directly but a listed target, and sends ETH directly
only to an account with no code, where nothing runs. That an account signed something in the
request doesn't make its code safe to call, and an account the request's authorizations
delegate counts as having code. So a sponsored account's funding is a plain send while the
account has no code, and goes through a runner carrying the ETH once it has. Any call into
the account's code goes through a runner too.

A plain send's gas limit is exactly what sending costs, so nothing is left for code to run:
if the account has gained code by then, the transaction fails and its bundle with it. Only
code that does nothing lets it through.

A call may send ETH, its `value`. The calls' values together are priced with the plan, and
with its gas they come under one cap, as a sponsorship's cost does now (`within_cost`), for
every plan.

Every runner is created through Arachnid's deployer, and Bruno's own deployer goes. Arachnid's
passes the transaction's value to the runner, takes a transaction with authorizations, and is
on every chain already, so one transaction carries ETH, authorizations or both, and nothing
is deployed. The runner's address follows from its initcode by CREATE2, so no other
submission's runner can be at an address prepared for this one. A plain contract creation
is never a runner: its address follows from the sender and nonce alone, which anyone can
work out ahead and which whatever is sent next would occupy.

Two things change with it:

- A runner that reverts answers nothing. A submission whose runner fails is refused without
  the runner's reason or its call's. What the relay works out itself is unchanged: the amount
  a payment is short by, and x402's codes for a signature or a balance.
- Nothing enforces that only Bruno's account deploys at a runner's address, or only in one
  block. Bruno picks the salt from the caller and the block, but anyone holding the initcode
  can run the same runner again at the same address. So nothing may treat a runner's address
  as authority, and SPEC.md says so.

For it:

- The deployer's part of a transaction's gas is Arachnid's, measured as the deployer's is
  now, and its answer is the runner's address in 20 bytes.
- The runner sends ETH with a call. It refuses a call with a value now. The funding
  initcode's sends are the pattern.
- A runner without its payment check is sent for real in a sponsorship, the forwarder
  checking after it. That runner is built only to estimate now.
- The tests that assert a runner's reason, and the pages that describe the deployer
  (SPEC.md, design/runner.md, DEPLOYMENT.md, the deployer's make target), change with it.

Nice to have: the reason for a failure, from one more simulation run only once it has
failed. For a runner, with Arachnid's address overridden by code that passes the revert data
up, which creates the runner at the same address. For a Multicall3 group, with its calls
simulated apart.

Each call is listed or not, and bounded or not. A draft carries its calls as planned, each
with the gas it's given and whether the relay bounded it (design/gas.md), decided in one
place for a quote and a submission. The plans still read the draft's one call (`sole`).

`proof_of` decides for all the calls together, with the outcomes it has now:

1. An op paying by its fees goes to its EntryPoint.
2. If every call is listed, the payment is a signed transfer and nothing is deployed, the
   calls are sent directly. Each run of consecutive bounded calls, the payment's transfer
   among them, is one `aggregate3` to Multicall3, with every call required to succeed. A run
   of one, and any call that isn't bounded, is a transaction of its own. Several transactions
   are a bundle.
3. Anything else is one runner, which makes the calls in order and then checks the payee's
   balance. It gives a bounded call exactly its bound, and any other call its stated gas,
   keeping back what the later calls need.

Gas:

- A bounded call is never estimated. A group's gas limit is computed from its calls' bounds
  and Multicall3's own overhead, and the plan is simulated at its limits, as every plan is.
- Nothing caps one call within a group, a client's transfer included. The transaction's
  limit is the most Bruno pays for and the payment covers it, as for every plan, and a group
  that runs short fails whole.
- Multicall3's overhead is measured for each fork, as the wallets' figures are. A test shows
  that a call merged into a group costs less than a transaction of its own, on both forks.
- A call that isn't bounded states its gas. A quote estimates it only where it's the only
  such call.

Also:

- A group that fails reverts with Multicall3's own message, so its submission is refused
  without saying which call failed.
- SPEC.md and design/runner.md say a submission is one call, and that a client batches
  through its own delegate. Rewrite both: a list is run whole by the relay, and anyone who
  sees the calls can still run each alone.
- No flow tests a transfer whose signer the same submission delegates. Such a signer's
  transfer is taken as a contract's now, estimated, for a payment and for a call alike, and
  only the vectors hold both relays to it: a flow needs a delegate that takes a key's own
  signature.
- A sponsorship's quote is drafted as a call's is. Its gas is filled in from one simulation
  of the sequence and a call's from an estimate, so the two need one rule
  ([above](#the-gas-a-sponsorships-quote-fills-in)).
- The Worker stores a pending submission with its `call`. Rows taken by 0.6.1 are read for
  as long as they last, which is its deadline of ten blocks and an hour's retention.
- A refusal names the entry it's about, `calls[2].gas`, as `transactions[2]` is named now.
- What describes the wire changes with it: the OpenAPI and OpenRPC documents, x402's
  discovery document and the bazaar extension's input, the TypeScript client and the
  AgentKit package, the docs' tested scripts, and the REST page's statement that a call has
  no `value`.

## Hosted x402 facilitator on Ethereum

Give each merchant a facilitator URL of the form
`https://bruno.mailam.app/x402/facilitator/<payout-address>/`, with x402 v2's `/supported`,
`/verify` and `/settle` under it. A `GET` of the URL itself is an HTML page for that payout
address. It shows the receipt address and the setup steps (the facilitator URL, the `payTo`
and the facilitator client's timeout), and links to the documentation.

Bruno derives the receipt key from the float's key and the payout address, the same way in
both relays, with vectors. The merchant points its x402 middleware at the URL and puts the
receipt address in its `payTo`. The buyer signs an ordinary USDC `exact` payment to that
address. Bruno checks that the signed recipient is the receipt address derived from the
route, and pays out to the route's address.

A settlement is a submission Bruno makes, of [several calls](#several-calls-in-a-submission):

1. The buyer's `transferWithAuthorization` to the receipt wallet.
2. The receipt wallet's authorization to send the merchant the payment less the fee.
3. As its payment, the receipt wallet's authorization to pay Bruno the fee.

Bruno signs the second and third with the receipt key, each valid until the deadline block's
time. With a buyer that's a key, all three are bounded, so the settlement is one `aggregate3`
transaction. A deployed smart wallet's transfer isn't bounded, and is a transaction of its
own ahead of the other two. The fee is Bruno's quote for the submission when `/settle` takes
it, and a payment that doesn't exceed the fee is refused. `/settle` answers with the
transaction's hash once it lands.

A settlement is found by the EIP-712 digest the buyer signed, through a map from the digest
to the submission's id, as a user operation is found by its hash. A repeated `/settle` is
held for the first one's outcome, or answered from its status while that's kept. After that,
the token itself answers that the authorization is used.

The receipt wallet keeps one USDC base unit (0.000001 USDC), as Bruno's forwarder does, and
nothing seeds it. The first payment to a receipt address sets its balance slot from zero and
pays Glamsterdam's cost for that, and its outgoing transfers leave one unit behind. Every
later settlement's outgoing transfers add up to the incoming payment, so the slot stays set.
The third transfer still adds gas compared with the merchant-run flow.

Bruno controls the receipt key. Receipt addresses follow the float's key, so rotating the
float is a problem for later.

Rewrite the [facilitator plan](docs/development/plans/facilitator.md) to this design. Its
proposed decisions and its open question on the fee are from before it, and so is the
facilitator's line in [priorities](docs/development/plans/priorities.md).

The fee and its cap, from the notes at the top. Decided by the user on 2026-11-28:

- No token is assumed. Whatever says the fee names its token, and gives the split: what the
  buyer paid, what Bruno keeps, what the merchant is paid out.
- A cap on the fee is optional and the merchant's to give. Without one a settlement is made
  at whatever the fee is, as designed above.
- A settlement whose fee is over its cap isn't made, and `/settle` says so at once. A
  merchant that served before settling carries that itself.
- The merchant's terms are part of the base URL's path, so no client changes: the reference
  TypeScript client builds `${url}/verify` by joining strings, so a path prefix works and a
  query string breaks. Each is one segment after the payout address, `name=value`, a term
  once, several values separated by commas ("a good general pattern that's easy to parse"):
  `…/<payout>/tokens=<a>,<b>/max-fee=<a>:<n>,<b>:<m>/`. `=`, `:` and `,` need no encoding
  in a path segment (RFC 3986's `pchar`); a segment is percent-decoded before it's read, for
  a client that encodes them anyway.
  - `max-fee`: the most the fee may be in a token, an amount in its base units.
  - `tokens`: the tokens taken, a payment in any other refused at `/verify`. Without it,
    every token the relay takes by EIP-3009. A token taken and given no cap settles at
    whatever the fee is.
- One receipt address for a payout address, whatever the terms. The terms are limits on the
  call they come with, enforced as that call is handled, and nothing of them is in the
  receipt key. I proposed deriving the address from the terms too and the user refused it:
  "it's going to make it an absolute nightmare". Accepted with it: a payment signed to the
  receipt address can be settled through that payout's URL with other terms, or none, by
  whoever holds it (the buyer, the merchant). The buyer pays the same either way.

- The split is answered in the `EXTENSION-RESPONSES` header of `/verify` and `/settle`, as
  an extension of Bruno's own, named in `/supported`'s `extensions`: "so the buyer doesn't
  need to see it". The header is a base64 JSON object keyed by extension name, "not part of
  the JSON response body and not forwarded to buyers"; the reference client puts it on the
  result as `extensionResponses` and strips it before the buyer. Not the body's
  `extensions`, which the reference resource server encodes whole into the buyer's
  `PAYMENT-RESPONSE`, nor `extra`, whose keys are the scheme's to define and which the
  specification has on `/verify` only. `/verify`'s is the price now, as a quote's is;
  `/settle`'s is what was charged.
- A token not taken and a fee over its cap are refused with reasons of Bruno's own, written
  as x402's are. The specification's codes (section 9) have none for either, it doesn't hold
  a reason to its list ("may be returned"), and the reference client reads `invalidReason`
  and `errorReason` as any string.

Read on 2026-11-28 from the specification and the reference client by an agent, and not
checked further: `settlePayment`, which prepares a settle result before it's encoded for the
buyer, and seven of the nine extension specs (the two read don't cover fees). The
extension's name and the two reasons' names are still to choose.

To investigate before building:

- Buyers that are undeployed smart wallets (ERC-6492). The buyer signs exactly the price, so
  its wallet's deployment can't be charged to it, and it isn't to come out of the merchant's
  part or Bruno's.
- The buyer's and the merchant's timeouts together, for Ethereum's block time: the buyer's
  wait on the merchant, the merchant's wait on `/settle` (x402's clients give a facilitator
  90 seconds unless set otherwise), and the payment's validity window.

## Example: Be your own x402 facilitator

Add a runnable merchant-side example at `docs/using/x402-facilitator.md` under Using Bruno
→ Integrations. Link it from [Paying over x402](docs/using/x402.md). Show one paid HTTP
endpoint, a small facilitator adapter wired into the x402 server, and a stock x402 buyer.
The merchant keeps the key for its own USDC receipt wallet and advertises that address as
`payTo`. The adapter uses Bruno's existing Ethereum REST API.

1. In `verify`, check the buyer's signed `exact` payment against the merchant's requirements,
   including USDC, the amount, the receipt address and the validity window. ABI-encode the
   authorization as a call to USDC's `transferWithAuthorization`.
2. Send the USDC call to `POST /ethereum/v1/quote` with `tokens: [USDC]` and the merchant's
   receipt address as `payer`. This prices the buyer's transfer and the merchant's signed
   fee transfer together. Check the live USDC quote against the merchant's fee cap and the
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

Demonstrate a merchant starting with zero USDC and a buyer using a stock client. Check that
the buyer's transfer and the fee transfer land in one transaction, that the merchant
receives the payment less the quoted fee, and that the settlement response names that
transaction. Explain that anyone who has the merchant's fee authorization can submit it on
its own until its `validBefore`. Link to the [direct settlement
design](docs/development/design/runner.md) and [REST request examples](docs/using/rest.md).
