# 0.7.0: what's left

A holding place for what's still to do. 0.7.0 is x402: several calls in a submission first,
then the facilitator built on them. The rest is carried from 0.6.1.

## Where the work stands (2026-11-27)

Pushed on `next-0.7.0`, with a draft pull request (#20) whose description says what's done
and what's next and is kept up with each push: steps 1 to 4 below, and the AgentKit
provider's removal. Committed after that and not pushed yet: step 5's first part, the
runner's deployer. `make check` passed on it locally. `make npm-e2e` hasn't run on it: the
pull request's `npm` job does once it's pushed.

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
5. Next. Every runner through Arachnid's deployer, the runner sending ETH with a call.
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
   Part two is next, a call's `value`. Nothing of it is built. What it has to do, as far as
   it's thought through:
   - Runner: a call with a value is a `CALL` sending it (it's refused now, `RunnerError`, in
     `_runner`). Its worst case joins R: the value's charge, and a new account's if the
     target is empty, both the fork's (the funding initcode's sends, `value_call` in the
     assembler, are the pattern). A bounded call never carries value.
   - The transaction: its value is the calls' values together, by `runner_call(salt,
     initcode, value)`, which Arachnid's passes to the runner. What the runner doesn't send
     goes back to the sender at SELFDESTRUCT. A step's value is already signed with it
     (`step.transaction.value` in batches), and a plan's cost has a value part (`CostBasis`).
   - Pricing: the charge includes the value, one for one in ETH and at the token's price
     otherwise, as a sponsorship's does for what the account is sent. It has to: a call may
     send its value to the payee, and the payee's balance rising by Bruno's own ETH must
     never pay for the submission. A test for exactly that.
   - The cap: `within_cost` for every plan, gas and value together under what one `max_gas`
     transaction costs by the deadline (it's only a sponsorship's now, `sponsored-over`).
   - Never direct: a call with a value goes through a runner, whatever it calls. Check what
     `targets.direct` does with a value now.
   - Simulating and estimating: the sender must hold the value when the transaction is
     simulated. See how a sponsorship's first transaction, which sends ETH, is simulated,
     and do the same.
   - A quote's breakdown shows the value. The wire already carries `value`, and the id
     already encodes it.
   - Tests: on-chain, a runner sends a call its value and returns the rest; through both
     relays, a call sending ETH paid for in USDC; a value sent to the payee not paying for
     itself; a submission over the cap refused. Vectors for the runner with values.
   - Docs: SPEC.md (`value` is zero if given, and the Semantics), the REST and JSON-RPC
     pages, design/runner.md and gas.md.
6. A sponsorship planned as a submission's calls, then the account's transactions, then the
   forwarder's check. Its wire takes `calls` then.
7. Consecutive bounded calls grouped into one `aggregate3`, Multicall3's overhead measured.

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
