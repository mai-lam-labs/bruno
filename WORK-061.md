# 0.6.1: what we're working on

A working note, kept out of the 0.6.0 branch and deleted before 0.6.1 merges. 0.6.0 (PR #18) is
the interface (the quote taking the call, the breakdown, the transfer to sign, refusals), the
funding as a bundle, the EntryPoint called directly, and calls spread across a chain's nodes.

This note, SPEC.md and the design docs are references kept by agents for agents. The user
doesn't review them, so whoever changes the code keeps them true, and nothing here waits on the
user reading a doc.

## The aim

Two things, which share one model and one contract:

- Sending for less. Almost every submission is sent as a runner, a contract created for that one
  transaction. Creating it costs 32,115 gas on Prague and 195,715 on Amsterdam, where EIP-8037
  charges 183,600 of it for the runner's account. Some submissions don't need one.
- Atomic sponsorship: paying the gas for transactions a user's own wallet signed
  ([sponsorship.md](docs/development/plans/sponsorship.md), designed and reviewed).

EntryPoint v0.9 is not part of 0.6.1 (the user, 2026-11-25: "we're not building that yet").
What was worked out for it is in [erc4337.md](docs/development/plans/erc4337.md).

## How a submission is sent

Bruno is paid because something reverts when it isn't, and the bundle drops what reverts. The
simulation before a submission is accepted confirms it would pay. It never stands in for that.

The relay runs whatever call the client gives it, with the gas the client states for it, and
prices its own overhead on top. Whatever makes the call has to keep to that.

One question decides how a call is made: is it on the whitelist?

- If it is, the concurrency account makes the call itself, as a transaction of its own. Its gas
  limit is the stated gas plus the transaction's intrinsic cost, so the EVM gives the call its
  gas and the overhead is exact.
- If it isn't, the call goes through a runner. A runner is created for that one transaction, so
  no address that outlives a transaction ever makes a client's call, and whatever a call sets up
  for its caller isn't there for a later call to use. This is isolation, and it protects the
  operator. It is the default.

A second question decides what else is needed: could the call succeed without paying?

- A listed call paid by a signed transfer could, taken alone. So the transfer is a transaction
  of its own, straight from the concurrency account to the token, and the two go in one bundle
  that lands whole or not at all.
- An op paying by its fees couldn't: the EntryPoint pays or reverts.
- A call that pays from inside could. Through a runner, the runner checks that the payee
  gained at least the amount, which is also what allows a tip. Made directly, it needs the
  forwarder, called in the account's next transaction, in the same all-or-nothing bundle.

| What is sent | How | What makes it revert unpaid | When |
|---|---|---|---|
| An op paying by its fees, EntryPoint v0.7 and v0.8 | `handleOps`, directly | the EntryPoint's own rules | built in 0.6 |
| A listed call paid by a signed transfer | the call, then the transfer, each a transaction, directly | the bundle, all or nothing | built |
| Any other call, however it pays; an op paying by its calls, v0.7 and v0.8 | the runner | the runner's balance check | as now |
| A sponsorship | the user's own transactions, between Bruno's pre-fund and the forwarder | the forwarder | built |

### The whitelist

`bruno/targets.json` lists the contracts a concurrency account may call directly. Each treats
every caller alike, so the account gains nothing there that a later call could use. An entry is
a whole contract (Arachnid's deployer, Multicall3) or a contract with the functions it may be
called with: each EntryPoint's `handleOps`; USDC's `transferWithAuthorization`,
`cancelAuthorization` and `permit`; DAI's `permit`. The EntryPoints are listed by function
because their deposit, stake and nonce functions act on the caller.

These are well-known contracts at their canonical addresses, taken as what they are. Being
listed adds no check of their code: no code hashes, nothing in `make preflight`, no live
verification, and no reading their code when a call is planned.

Being listed means a client's own call to that target may be sent directly. It never means the
relay sends calls through it. Multicall3 is listed, and the relay does not use Multicall3 to
make anyone's call. A client may wrap a call in Multicall3 itself to get the direct path, and
then that is its call.

### The forwarder

One contract with the payee (the treasury's address) in its code, for ETH and every ERC-20,
built in step 3 (`bruno/forwarder.py`, `worker/src/forwarder.ts`). Whoever is paying pays it,
where they would have paid the payee. Bruno's last transaction in the bundle calls it with the
amount: it reads the payee's balance, sends the payee what it holds, reads the balance again,
and reverts unless the payee gained the amount. It has no owner and no state, and anyone may
call it, since it pays only the payee.

It keeps one unit of each token back, so its balance slot in the token is never cleared and
paid for again. Its check allows for that unit: the payee gains the amount, or one base unit
less.

It checks that the payee gained enough, not that this bundle's transactions paid it in. That is
accepted: Bruno is paid either way. It relies on the bundle being all or nothing, which
everything here already relies on.

## The work, in order

### Where it stands, 2026-11-25: read this first

0.6.1 is the sponsorship release (the user's, 2026-11-24). PR #19 is its PR, a draft, titled
"0.6.1: atomic sponsorship and strict requests for clients, the payee's forwarder for
operators", its description brought up to date on 2026-11-25. The branch is `next-0.6.1`.

Committed and pushed, none deployed. Nothing is uncommitted:

- 05fe0e9: one planning path, the whitelist, a listed call sent directly (steps 1 and 2).
- 85b8c8e: the forwarder (step 3).
- 8b51cb6: a sponsorship planned (step 4, items 1 to 5).
- 40e85d5: a sponsorship quoted, taken and sent, a broken bundle rejected, the client's
  `sponsor` (step 4, items 6 to 10).
- 46938e8: a sponsorship's own methods, strict reading, `transactionHashes`, the runner's call
  before a key's transfer, an account that can't pay answered as the client's, the docs and the
  versions (the relay 0.6.1, the packages 0.6.1-dev.1). Its message says each.

The commit after 46938e8, from a third outside review (the user's, 2026-11-25, ahead of the
release), each point checked first:

- A running relay never read its forwarder's code: only `make preflight` did, which only
  `make worker-deploy` runs. A call to an address without code succeeds, so on a chain without
  the forwarder a sponsorship's last transaction would have checked nothing. Now the
  measurements read it (one more `eth_getCode`, both relays), and where it isn't deployed a
  sponsorship is neither quoted nor taken: 503, `no-forwarder`, the operator's
  (`Measurements.unsponsored`, `Measuring.sponsoring`). Calls and x402 are served as they were,
  and the root lists it with the chain's problems. Only whether code is there is checked (the
  user: it's deployed by Arachnid's deployer, "so either the code's there or it's not").
  Preflight reports it from the measurements, its own check gone. A forwarder measured
  missing is read again before a sponsorship is refused for it (`Measuring.sponsoring`, one
  `eth_getCode`), so one deployed since is found at once, where the refusal would have lasted
  until the next measurement, an hour by default (a fourth review's finding, 2026-11-25).
  Tests, both relays, seen failing with each rule off.
- The root shows the forwarder (the user, 2026-11-25): its address in each chain's entry
  (`forwarder`), and in the accounts table its ETH and what it holds of each token, read with
  the float, the accounts and the payee at the one block (`survey`, the treasury's read;
  `forwarderBalance`, `forwarderTokens`). The Worker reads a status 0.6.0 stored as none, which
  the next read replaces. The forwarder's tokens are read for the root alone
  (`shown_holdings`): a token that doesn't answer for the forwarder is left out, where it
  would have failed the survey every batch is built from, and the treasury's run, so stopped
  calls that use no forwarder (the fourth review's other finding). Tests, both relays, seen
  failing with the reads misplaced, and with them strict: a token not answering for the
  forwarder, a call still landing and the treasury still running.
- Docs: releasing.md's deploy step says the forwarder comes first; DEPLOYMENT.md, a chain
  without it takes no sponsorship; SPEC.md the same; the homepage says a builder keeps a
  sponsorship whole, not the chain; the README has the sponsorship's paragraph; Getting
  started no longer says MCP is planned (it's a project of its own, priorities.md), and says a
  sponsorship needs a wallet that signs without sending; the support page's client row has
  sponsorship.

Checks. `make check` passed on 46938e8: 484 tests a fork, both relays, with lint, types, the
Worker's checks (38), the client's (28); `make docs`, strict, with no issues; the sponsorship
script in a container against both relays (`make npm-e2e`), and `make npm-test`. On the
commit after it: `make check` passed (490 tests a fork, both relays), and `make docs`, strict.

Sepolia, 2026-11-25 (`make check-sepolia`, four runs, 5 min 24 s and the sweep), before the
sponsorship's methods were split out:

- The sponsorship landed: block 12203921, built by `builder.nflaig.eth`, quoted 25.59 USDC,
  the check 0x9f78a653a0651516b34b66cf66edd368e801611e909dd9c18dd1ca56c917dc1a, the funding
  0xd1f420605e1f4e585558030a108ba7b70da5ed21ebf9ef08b066e3bb7ed98ab1. So Sepolia's nodes
  answer the forwarder's measurement (an estimate made from a contract's address), and the
  ePBS builder takes a bundle of four holding two another account signed.
- The test wallet's forwarder is deployed there: 0x98c78a62529EBD601238718389A07fFb30c264cA
  (the payee is the float, 0x9e2E7d8F2C05307B2F707988B2A3e720AfCb7bA6).
- The bundle run passed. Both x402 runs failed, 503 `node-failed`: a node a block behind
  answered `eth_simulateV1` "-32000 block not found: 0xba3788", which wasn't read as unseen.
  Fixed since, not run again on Sepolia. Not from the sponsorship work.
- The sweep stopped on the sponsored account's dust (1,170,848 wei). Fixed since.

Next, in order. Each live or outward step is asked of the user first, one at a time:

1. Done: committed and pushed (the user, 2026-11-25). PR #19's description is to say the
   commit's points. The user is making docs updates of their own next.
2. `make check-sepolia`, when the user says (about 6 minutes, testnet gas only): the
   sponsorship by its own paths, the two x402 runs with the "block not found" fix, and the
   sweep to finish.
3. The production payee's forwarder, on Sepolia and on Ethereum (`make <net>-forwarder-deploy`,
   with `PAYEE=` the Worker's payee if it isn't `.secrets/payee.address`'s). The user's, and
   the user said (2026-11-25) they'll decide when, likely the morning of 2026-11-26. It needs
   no tag and no merge: the forwarder follows from the payee alone, and 0.6.0's Worker doesn't
   use it. Deployed before the Worker, nothing depends on the order of the rest.
4. The release, a step at a time (releasing.md): this note deleted, the merge, the tag, the
   Worker (`make worker-deploy`, whose preflight refuses a chain without the forwarder), the
   verify run.
5. Once 0.6.1 is deployed: the REST and JSON-RPC pages' sponsorship example is a real one
   from a local chain (chain id 31337), request and answer, and says so; take it again from
   Sepolia. Their `postpaid_config` examples show Sepolia's `forwarder` as computed for its
   payee (`forwarder_of`, 0x2Dc27cb0fbce57bD1122b2CAa61D05d9E6B23838), which the deployed
   relay doesn't answer until then.
6. What's left to build of step 4, below ("Not built yet").

Not done, and said to the user: a quote still offers an ETH payment to an account that holds
no ETH. It can't know an earlier transaction won't bring it; the pages and the refusal
(`sponsored-unfunded`) say what's needed. Nothing sends on what a forwarder holds after a
broken bundle: anyone may call it, and the root now shows what's there.

### Open, as of 2026-11-25

Nothing is open. The outside review of the interface the user brought on 2026-11-25 had its
findings each checked against the code, and all held; all are closed, by the separate methods
and the strict reading (above): an account with code quoted no gas; only the first `from`
naming the account; fields ignored, a call's `value` and a quote's `maxBlockNumber` among
them; one object of optional fields in the documents where a request is one of two kinds.

Settled, so not to be asked again:

- A sponsorship lands whole or not at all. After a bundle breaks nothing more of it is sent
  ("Why keep recovery? It's all or nothing.", 2026-11-24). Bruno pays the gas and Bruno is
  paid; a guarantee isn't traded for recovering Bruno's funds.
- What a sponsorship is charged doesn't change with the bytes of what it pays: calldata that may
  hold the amount is priced as if no byte were zero, the customer paying that assumption.
- The account's balance isn't Bruno's concern: the funding is what its transactions need,
  whatever it holds. It may pay out of ETH Bruno sent it, the check asking for more than was
  sent.
- The wire: a sponsorship's own methods; its submission `{account, transactions?,
  authorizationList?, call?, gas?, payment, maxBlockNumber}`, the forwarder the payment's
  `payee`. `payment` keeps its name.
- A payment left in the forwarder is accepted (the user, 2026-11-25). With no transaction of
  the account's, its code pays in the relay's first call. If a builder lands that call alone,
  the bundle is broken and the relay stops, so its check never runs and the payment stays in
  the forwarder until the next forwarding of that token, which may then pay that much less.
  The builder didn't do what it was to. Nothing is built for it.
- EntryPoint v0.9 is not in 0.6.1.

Known and not traced:

- `make burn-in` fails on both relays, and fails the same way at the commit before this work
  (eb278dd), with the same seed (16136353). Every violation is the relay asking its one node
  about a block the node doesn't have yet (`BlockOutOfRangeError`). No transaction reverts and
  no status is wrong.
- The headroom fix for the transfer (step 2) has no local test that fails without it. Mainnet's
  USDC, which anvil has, leaves 267 gas to spare. Only the Sepolia run shows it.

### Done before the steps

- One planning path. A quote, the bundler's estimates and a submission are each described as a
  `Draft`, given their proof by `proof_of`, and planned by `plan_drafts`. Nothing else builds a
  submission's transaction.
- The whitelist, read by both relays through `targets.direct`. `proof_of` asks it, for a quote
  and a submission alike.
- The quoting members (`estimating`, `call_gas_of`, `call_part`) on a narrower type,
  `QuotedProof`, which the runner's proof and the transfer's implement. The EntryPoint's proof
  has none, since an op paying by its fees is never quoted.
- Multicall3 vendored onto the local chain, and USDC listed for it (31337).

### Step 1: a plan of several transactions, in a bundle of its own (built)

The batch loop signs every step of a plan at its account's consecutive nonces. A plan of one
transaction goes in the block's bundle as before. A plan of several goes as a bundle of its
own with nothing a builder may leave out, so it lands whole or not at all.

- A step carries the authorizations it sends, the submission's or none, and says whether its
  gas limit is its estimate. Before, both were the plan's, which only fits one transaction.
- A send is tracked as its transactions' hashes, as many as the plan has steps. It is included
  only if every one landed and succeeded.
- A submission's transaction hash is its last step's. That one lands only after the others, and
  for a payment by transfer it is the transfer, which is what x402 calls the settlement.
- The funding needed nothing new: it already travels as a bundle of its own ahead of the
  submissions it funds.

### Step 2: a listed call paid by a signed transfer, sent directly (built)

`proof_of` asks the whitelist about the client's call, and about the token's
`transferWithAuthorization`. If both are listed and the submission deploys no account, the plan
is two transactions from the concurrency account (`PaidByTransfer`): the call, at its stated gas
plus the transaction's own, then the transfer to the token. Any other submission keeps the
runner. REST, JSON-RPC and x402 all go this way.

What it saves, measured on anvil for the same 36-byte call given 50,000 gas and paid by a
key's USDC transfer, once to a listed target and once to one that isn't:

| | Through the runner | Sent directly, call + transfer |
|---|---|---|
| Prague, gas limit | 204,583 | 71,432 + 95,848 = 167,280 |
| Prague, gas used | 139,310 | 24,353 + 90,781 = 115,134 |
| Amsterdam, gas limit | 553,155 | 65,432 + 274,120 = 339,552 |
| Amsterdam, gas used | 474,778 | 18,753 + 262,217 = 280,970 |

The quote follows the gas limit: 18% less on Prague and 39% less on Amsterdam. These figures
are from before the transfer was given its headroom (below), which adds about a 63rd of the
transfer's gas to the direct column. On Sepolia the same call was quoted 24.24 USDC sent
directly and 45.56 through a runner.

Checked on Sepolia (2026-11-24, `make check-sepolia`, now three runs): a listed call paid by a
key's transfer landed through the nflaig ePBS builder as two transactions in one block, and
the two existing runs pass unchanged.

What Sepolia found that anvil couldn't. The first live run was refused: the relay's simulation
of the transfer, as a transaction of its own, ran out of gas by about 120. A transfer's bound
is a measured zero-value transfer with the cost of the two balance writes added. A token
spends those writes below its own frame, in its proxy's implementation, and a nested call is
given all but a 64th of what is left (EIP-150), so the bound needs that much more. The runner
has always used the same bound and gets away with it, because its balance read warms the
implementation and the payee's balance first. Mainnet's USDC, which anvil has, happens to
leave 267 gas to spare; Sepolia's doesn't. The transfer sent directly is now given a 63rd more
than its bound. The runner's cap is unchanged.

How its details came out:

- The call comes first, so it can earn what pays for it. SPEC.md now says a transfer is made
  before the call or after it, and that which isn't guaranteed. The runner still makes the
  transfer first, and is to make the call first too (ruled 2026-11-25, not built: "Open",
  above).
- A contract payer's transfer is its own transaction, so its gas is an estimate of that
  transaction alone, as the chain stands before the call. A contract payer whose call is what
  funds it is therefore refused. A key's transfer is bounded, not estimated, so it isn't.
- A quote's stand-in transfer now starts with the function it will call, so a quote is matched
  to the route by the same lookup as a submission. Its length and its count of zero bytes are
  unchanged.
- A quote that gives only the call's length, not the call, can't be matched to a listed target,
  so it is priced through the runner. That is at least what the submission then costs.

Tests, on both relays, each seen failing first: over REST, JSON-RPC and x402 a listed call paid
by a key's transfer lands as two transactions from one account, the call then the transfer,
quoted less than the same call to a target that isn't listed; a smart wallet's does too; a
payer holding nothing pays out of what its call moves to it; a transfer or a call that fails
in its block lands nothing, which fails if the pair is sent in the shared bundle; a call to a
target that isn't listed still goes through the runner. The vectors hold both relays to the
same two transactions, gas and route.

### Step 3: the forwarder (built)

The forwarder as described above, one contract where sponsorship.md first had two (the user's,
2026-11-24: one contract for ETH and ERC-20s, checking the payee's balance before and after).

- `forwarder_of(payee)` gives its code, initcode and address; `forwarding(asset, least)` the
  call; `token_gas` and `eth_gas` its own gas under a schedule. Assembled with the runner's
  assembler, which gained the opcodes it needs. The Worker's is the same, held to it by the
  `forwarders` vectors.
- Its own gas is 3,311 forwarding a token on Prague and 3,711 on Amsterdam, and matches
  receipts exactly on both. What the token's `balanceOf` (three reads) and `transfer` use is on
  top, and isn't measured yet: that's step 4's, for the check's gas limit, with the 63/64
  headroom at each call depth (the forwarder's call to the token, a proxy token's to its
  implementation).
- `make <net>-forwarder-deploy` deploys it through Arachnid's deployer, and preflight checks
  its code is at the address the payee gives it.
- Tests (`test_forwarder.py`, both forks): paid in USDC, USDT and WETH; the kept unit and its
  boundaries; a tip; a token that burns part of a transfer (Statera, vendored for this); a token
  refusing the transfer; an address that isn't a token; ETH, and a payee refusing it. Each was
  seen to fail with the rule it tests broken.
- Since, with step 4: each relay computes its payee's forwarder as it opens and gives it in
  `postpaid_config`, the forwarder's call and gas are functions of its address and the schedule
  (`forwarding`, `token_gas`, `eth_gas`), each token's transfer is measured as the forwarder
  makes it, and the local chain's setup deploys the test payee's.

### Step 4: atomic sponsorship

As [sponsorship.md](docs/development/plans/sponsorship.md) has it, whose checklist is the order
of work. Steps 1 and 3, both built, are its groundwork. What it still needs is a step that is
another account's signed transaction, a type for what is taken and stored beside `Submission`,
and the check's gas (a token's `transfer` and `balanceOf` measured as the forwarder calls
them).

Progress, in the order it's being built. Items 1 to 5 are built on both relays and
committed (8b51cb6):

1. Reading a signed transaction back from its bytes (`transactions.decoded`), EIP-1559 and
   EIP-7702 only, strictly, its sender recovered. Held to by the `decodedTransactions` vectors,
   and checked against what anvil says of a landed transaction.
2. `Sponsorship` (`sponsorship.py`, `sponsorship.ts`): what's taken, with its id and the ETH
   it's funded (`funding`). It holds its own invariant: every transaction and authorization is
   the account's, the call is to it, nonces are consecutive, and there's a call or a
   transaction. Three refusals: `sponsored-nothing`, `sponsored-not-accounts`,
   `sponsored-nonce`.
3. A plan's two kinds of step: `Step`, the relay's own, and `SignedStep`, another account's as
   signed. `Plan.own` is the relay's, which `basis`, `cost` and the sends' hashes count;
   `gas_limit` counts every step, so the budget (`max_gas`) covers the user's gas too.
   Simulation and signing take both kinds.
4. The proofs split in three: `Proof` (what's paid, how a simulation reads), `DraftedProof`
   (plans a submission's draft: the runner's, the EntryPoint's, the transfer's), and
   `CheckedByForwarder`, a sponsorship's, which plans nothing. A proof's `paying` takes the
   amount now, not the submission. `make_plans` takes submissions and sponsorships together and
   plans a sponsorship by `sandwich`: the relay's first transaction (the funding, the
   authorizations, the call given the sponsorship's gas, and what creating or delegating an
   empty account costs), the account's own, then the forwarder's call.
5. The check's gas (`check_gas`): the forwarder's own, and for a token three `balanceOf` reads
   and its transfer with a 63rd more. The transfer is measured as the forwarder makes it
   (`Erc20.forward_gas`, from `forwarder.measuring`), and the measurements say whether the
   payee's account is yet to be created (`payee_new`), which ETH forwarded to it pays for. A
   token that didn't measure is refused, `not-forwarded`.

Decided while building, and confirmed by the user on 2026-11-24 (the account's balance isn't
Bruno's concern: it funds the transactions, bundles them and is paid the agreed amount, and
what matters is that it never sends out more than it's paid for):

- The funding is every transaction's gas at its max fee, and its value, whatever the account
  already holds. The plan had "less what the account holds", which needs the account's balance
  read at every block. As built, a plan is a function of what was taken, and an account holding
  some ETH is sent more than it lacks. What it doesn't spend stays with it.
- Value a transaction sends the forwarder isn't funded. This is arithmetic, not a safeguard:
  funded, the payment would be part of its own price, which no amount covers. The account may
  pay out of any other ETH Bruno sent it (the user's, 2026-11-24): the check asks for more than
  was sent, whatever it's paid from.

Tests (`test_sponsorship.py`, both forks): both forms planned by the real measurements, simulated
as signed, then sent on anvil with the relay's transactions at exactly their planned gas. Each
of four rules was broken in turn and a test failed. The `sponsorships` vectors hold the Worker
to Python's ids, funding and plans.

Built since that commit, on both relays, and committed (40e85d5), items 6 to 10:

6. Taking one. Over REST and JSON-RPC a sponsorship is submitted by its own method
   (`postpaid_sponsor`, `POST /v1/sponsorships`): `{account, transactions?, authorizationList?,
   call?, gas?, payment, maxBlockNumber}`, its payment's `payee` the forwarder;
   `postpaid_config` gives the forwarder. `Relay.sponsor` checks it (the forwarder
   is the relay's, the authorizations and transactions are this chain's, each transaction's max
   fee is what a block may ask by the deadline, the account has no other pending), plans it,
   and takes it by the path a submission takes (`_take`: within `max_gas`, paying its charge,
   simulating). A tracked entry holds either kind; the Worker stores a sponsorship apart
   (`sponsorship` beside the entry, its transactions as signed).
   Refusals: `transaction-undecodable`, `transaction-chain`, `transaction-fee`,
   `sponsored-no-gas`, `sponsored-in-flight` (retry).
7. Sending one: nothing new was needed. The batches rank, fund, simulate as signed, sign and
   send it as the atomic bundle a plan of several steps already is, and reconcile it by the
   relay's own hashes. A sponsorship lands whole through the mock builder on both relays.

Found by the end-to-end tests, and fixed:

- The check's calldata holds the amount, so the price moved with the amount paid: a submission
  paying exactly what an underpaid refusal said was required was underpaid again. The check's
  intrinsic gas is now priced as if the amount's word had no zero byte.
- A transaction whose tip is over its max fee decoded, and then failed the node's simulation as
  the node's failure. `decoded` refuses it.
- A simulation before an account is chosen stands the sender in for it, and the sender doesn't
  hold the funding. It's given what the plan sends, by a state override.
- The local chain's setup deploys the test payee's forwarder (`PAYEE_ACCOUNT`, now
  `localchain.py`'s).

Not built yet, in order:

8. Built: a bundle broken. A sponsorship lands whole or not at all (the
   user's, 2026-11-24: "Why keep recovery? It's all or nothing."). `_reconcile_one` finds some
   of a send's transactions landed, each succeeding, and not the rest: it's rejected, its reason
   `bundle-broken` (a `Rejection`, in the OpenAPI document and the client's types), listed with
   the chain's problems with the wei those that landed sent (`Measuring.broken_bundle`), and
   nothing more of it is sent. The rule is any plan's, not a sponsorship's alone: a listed
   call landing without its transfer is the same. A transaction that landed and failed is
   still `reverted`.

   Sending the rest after a broken bundle was built first (a `funded` state on the proof, the
   plan of what was left by the account's nonce) and removed the same day: it recovered Bruno's
   payment by giving up the guarantee, and a builder breaking the next bundle too would have
   landed the account's transactions a piece at a time. Bruno's loss when a bundle breaks is
   the funding, bounded by `max_gas`.

   Test, both relays (the mock builder's `breaking` commits only the first transaction of a
   bundle of several): the sponsorship is rejected `bundle-broken`, listed with what it sent,
   and the blocks after hold nothing of it.
9. Built: the quote (`sponsoring.py`, `sponsoring.ts`;
   `Relay.quote_sponsorship`). `postpaid_quoteSponsorship` takes the account, each of its
   transactions signed already (raw) or to be (`{to?, data?, value?, gas?}`), and
   `maxBlockNumber`, the furthest a deadline may be if not given. Each unsigned one is given
   the account's next nonce (after its authorizations), the max fee a block may ask by the
   deadline, and the market's tip. Then a token at a time: with transactions of the account's,
   the payment in that token is added after them; the sequence is simulated once
   (`simulation.gas_used`, the sender given what it sends) for each one's gas, a tenth more
   where none was given, the payment's with what an amount's word may add; and it's planned and
   charged as a submission of it would be (a stand-in `Sponsorship`, its unsigned transactions
   drafted). The answer is `validUntilBlock`, `maxBlockNumber`, `forwarder`, `gas`?, the
   `transactions` to sign, and each token's `amount` and `payment`.

   With no transaction of the account's (its code pays, in the relay's call), no payment is
   offered: the answer gives the amount and the call's gas, and the account signs its call
   again paying it.

   The first transaction's calldata is priced as if no byte of it were zero
   (`funding_overhead`), as the check's amount is: the account's call may hold what it pays,
   and the quote's test found the price moving when it was signed again.

   Not in it: an unsigned transaction pinning its own nonce or fees (sign it and send it
   signed), and a breakdown of the price.
10. Built: the client (`clients/typescript/src/sponsored.ts`, `Bruno.sponsor`,
    `quoteSponsorship` and `submitSponsorship`, `viemTransactionSigner`). `sponsor` quotes, checks what it's given to
    sign is the calls given and a transfer of the price to the quote's forwarder and nothing
    else (`checkedSponsored`), has the wallet sign each without sending, and submits. Tested
    against a stand-in relay, what it submits read back by the Worker's `decoded`; seven ways
    a quote can differ from what was asked are each refused before anything is signed. The
    form with no transactions is quoted and submitted by `quoteSponsorship` and
    `submitSponsorship` (the account's delegate's batch is the caller's to build, as for a
    call that pays for itself); `sponsor` covers transactions. The README has a section.

Not built yet, in order:

11. Done: SPEC.md, the REST and JSON-RPC pages, Getting started, the overview, the support
    page and the client README (above), and a page of its own with a whole, tested script
    (`docs/using/sponsorship.md`). Left of it: the design pages (why the sandwich, why the payee's gain, why nothing is sent after a
    broken bundle), with sponsorship.md trimmed to what's left.
12. A relative deadline. A quote's `maxBlockNumber` is a block number, and a client that
    doesn't know the head gets the default, the furthest (`max_deadline_blocks`), which costs
    the most. Either the client reads the head itself, or the quote takes a count of blocks.
13. Done: the client's form with no transactions (a delegation and a call the account's code
    pays in), by `quoteSponsorship` and `submitSponsorship`, tested against the stand-in
    relay.
14. An unsigned transaction pinning its own nonce or fees in a quote; a breakdown of a
    sponsorship's price. Both left out of the quote.
15. A test that a sponsorship sits out a block whose fees its payment doesn't cover (`_covered`
    is a submission's, by the plan's cost, which includes the funding).
16. The client published: its JSR and npm versions stay `-dev`.

The user saw items 2 to 4 described on 2026-11-24 and said to proceed.

Nothing in the plan is left to decide. Settled on 2026-11-23 and 2026-11-24:

- All three forms are built together: the user's transactions in the middle, a call the pre-fund
  carries with the middle empty, and both.
- A sponsorship stores the forwarder its user paid, as a submission stores its payee.
- The deadline is bounded by `max_deadline_blocks`, as a submission's is. No new key.
- The wire format: a sponsorship's `payment` is `{token, payee, amount}` with the forwarder
  as `payee`, and its `gas` the gas the pre-fund's call into the account is given, stated by
  the client as a call's is. Its methods are its own (2026-11-25, above).

## Ruled out

- Sending a call with its payment only simulated. It was built on 2026-11-23 and backed out:
  nothing reverted when it didn't pay.
- A persistent contract that wraps any call, as the default in place of the runner.
- Multicall3, or a contract of ours like it, making the transfer and the call in one
  transaction. Built through Multicall3 on 2026-11-23 and removed the same day. Multicall3 has
  no gas argument, so a call gets whatever gas is left, and it copies what the call answers at
  a cost that isn't known until the call has run. So it can neither give a call its stated gas
  nor have its overhead priced on top. It also sent calls that aren't listed without a runner.
  The bundle already makes two transactions atomic, so no contract is needed for that.
- Permit2 payments (x402's `permit2` transfer method and its proxy's `settle`).
- Recognising accounts or proxies by their code hash.
- Routes specific to Safe (decoding a SafeTx, Safe's own rules). That is separate work, with a
  Safe by one signature.
- Listing a target that keys something on its caller: Permit2's signature transfers and
  allowance functions; a token's `approve`, `transfer` and `receiveWithAuthorization`;
  MetaMask's DelegationManager; CreateX's guarded salts; UniswapX and 1inch, where the filler
  pays; and relayer contracts that only registered relayers may call (Gelato's, GSN's RelayHub,
  CoW's settlement).

Any change to what proves payment is shown and agreed before it is built.

## Later

- **EntryPoint v0.9** ([erc4337.md](docs/development/plans/erc4337.md), "EntryPoint v0.9"):
  not in 0.6.1. Its `handleOps` refuses a caller with code, so an op paying by its calls would
  be sent directly with the forwarder's check after it.
- **The client without `-dev`.** Its API settled, its `.d.ts` written as the reference a model
  reads, OpenAPI and OpenRPC aligned with it. Once it's not `-dev`, the API is a promise.
- **The treasury leaving one unit behind.** It can convert the payee's whole balance of a token
  (`treasury.py`, `min(balance, …)`), which clears the payee's balance slot, and the next payment
  then pays to create it again (`eip3009.transfer_gas`'s `storage_set`, 107,920 gas on
  Amsterdam). Converting the balance less one unit avoids that, for every kind of submission.
  A change of its own, separate from sponsorship.
- **The funding ahead of the runs:** put in a dropping bundle, it would save a block.
- **A Safe by one signature** ([safe.md](docs/development/plans/safe.md)): Safe's contracts
  vendored, deployed on first use, the SafeTx with a payment leg. Its four-item verification
  list first.
- **Money hygiene:** the cold wallet; persistence for the Python relay's queue and treasury
  snapshot, which it forgets on restart.
- **Base** ([morechains.md](docs/development/plans/morechains.md)): set aside while Bruno
  sends only through `eth_sendBundle`.
