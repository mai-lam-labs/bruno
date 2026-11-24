---
description: "Plan: atomic sponsorship, transactions the user's own wallet signed but can't pay gas for, sandwiched in one atomic bundle between Bruno's pre-fund of the account and a check that forwards the payment to the payee, in any token or ETH"
---

# Plan: atomic sponsorship

A user's wallet signs transactions it can't pay the gas for: an EOA holding a token and no ETH.
Bruno sandwiches them in one atomic bundle, between a transaction pre-funding the account with
the ETH they need and a check that reverts the bundle unless Bruno was paid. The bundle lands
whole or not at all, so Bruno's ETH reaches the account only in the block where the user pays
for it. Recorded 2026-11-03, from the [agent wallets](agent-wallets.md) research: it's the
integration for every wallet that signs a transaction without sending it (Privy, Turnkey,
Dynamic, Openfort, an agent's own key). The design below settled 2026-11-08 and was reviewed
2026-11-10.

## Why

- **`msg.sender` is the user's.** The transactions are the user's own, signed by their own
  wallet: any call, to any contract, as the account itself. No delegation, no smart account,
  no runner standing in for the sender.
- **Nothing to set up.** The account needs no code and no prior transaction; a type-4
  transaction delegating it by EIP-7702 is sponsored the same way.
- **Any token, or ETH.** The payment is whatever Bruno prices, made however the user's
  transactions make it.
- **Bruno's guarantee holds.** A bundle that wouldn't pay never lands, so a failed attempt costs
  nothing.

## Titan's sponsored bundles, for comparison

Titan's builder already sponsors searchers' bundles: one that fails with
`LackOfFundForGasLimit` has an ETH transfer of Titan's prepended, if the bundle "increase[s] the
builder balance" ([Titan's docs](https://docs.titanbuilder.xyz/llms-full.txt),
[Titan Tech Teatime](https://titanbuilder.substack.com/p/titan-tech-teatime-1)). A Flashbots
[proposal](https://collective.flashbots.net/t/proposal-builder-funded-gas-sponsorship-for-bundles/5820)
would bring the same to BuilderNet. Bruno's differs:

| | Titan | Bruno |
|---|---|---|
| Who sponsors | one builder, in its own blocks | any builder that takes an atomic bundle |
| Repaid in | ETH, to the builder | any token Bruno prices, or ETH, to its payee |
| Payment enforced by | the builder's own simulation | the bundle itself, onchain |
| For | searchers' bundles | any user's transactions |

## The bundle

An atomic bundle of its own (no `droppingTxHashes`, no `revertingTxHashes`: `atomic_params`, as
the treasury's runs and the funding go), in either mode, to every builder, for the next block,
signed again each block:

1. **The pre-fund:** from a concurrency account A, at nonce n, the ETH the user's account U
   lacks for its transactions: each one's `gasLimit × maxFeePerGas + value`, summed, less what
   U holds. It may carry U's EIP-7702 authorizations (type 4), and a call into U's code. A
   transaction's gas limit is the one given, anything up to the budget, or else the
   simulation's estimate. The one value never covered is the offered ETH payment's own (see
   [its answer](#the-interface)): U must hold that by then, already or from an earlier
   transaction. Funding it couldn't be priced, because the amount would have to cover itself.
2. **The user's transactions, as signed,** at U's consecutive nonces: none, one or several. A
   plain EOA's transaction is one call, so a call and its payment are two transactions (a swap,
   then the payment), or one through an EIP-7702 delegate that batches.
3. **The check:** from A, at nonce n+1, the forwarder's call for that asset, for at least the
   amount.

Between the pre-fund and the check, U pays the forwarder in the payment's asset, however
it pays: in one of its transactions, or in none of them. Sending ETH to an account can run code:
an account with code (an EIP-7702 delegate, a smart account) runs it when the pre-fund reaches
it, so its code may make the payment, and the pre-fund may carry the call it's to make, and the
authorization delegating U first. Then the sandwich is the pre-fund and the check alone, the
middle empty.

A is whichever concurrency account the batch picks: the pay-to address doesn't depend on it, so
nothing is allocated when quoting. A user transaction that reverts fails the bundle: nothing
lands, nobody pays.

## The forwarder

One contract with the payee in its code, for ETH and every ERC-20, built
(`bruno/forwarder.py`, `worker/src/forwarder.ts`; its docstring has what it does, step by step).
Whatever it forwards goes to the payee and nowhere else. No owner, no storage of its own: it's
assembled opcode by opcode, as the runner is, with no Solidity. It measures a payment as the
runner does, by the payee's gain across its own call, for ETH as for a token.

It always sends one unit of a token less than it holds, so its balance slot in the token is
never cleared and created again (on Amsterdam a new slot is 107,920 gas, `forks.py`
`storage_set`). The unit is the contract's own rule, and nothing else accounts for it: the relay
asks for the amount, and the check allows for the unit that stays behind. The payee may so gain
one base unit less than the amount, and never less than that.

The first payment of a token into the forwarder creates its slot. That gas is in the payer's own
transaction, estimated with the rest of the sandwich, so nothing treats it specially. An
operator may send the forwarder one unit of each token ahead of time so that no payer pays for
the slot, but nothing depends on it.

**Where it is.** Deployed through Arachnid's deterministic deployer, salt 0, as Bruno's deployer
is. Its initcode holds the payee, so its address is a function of the payee and the code: each
relay computes it once per chain when it opens, beside the payee it holds, and the vectors check
both relays generate the same code. Nothing is configured: a new payee, or new code, is a new
forwarder. `make <network>-forwarder-deploy`, by hand, deploys it for the configured payee, one
already there left alone; `make preflight` checks the code at the computed address is the
generated runtime, so a relay whose forwarder isn't on a chain isn't deployed.

**Switching over.** The forwarder a user pays is part of what they sign, so each sponsorship
keeps it: the submission names it (its quote gave it), the relay takes it only if it's the
current one, and stores it with the sponsorship (the Worker in its stored form). Each block's
check calls the stored forwarder, never the current one: a sponsorship taken before a new
forwarder is checked against the one its user paid, until it lands or expires, while new quotes
name the new one. A quote from before the switch, submitted after it, is refused, to quote
again; nothing's paid or sent by then. An old forwarder stays deployed and needs nothing: it's
bound to its payee.

## Why it's sound

Bruno's own signed transaction is the condition: its bundle lands only if its call to the
forwarder sent the payee at least the amount. Anyone may call the forwarder, but it pays only
the payee. Each call
takes everything it counted, and the contract never goes below its unit, so in any block as
built the calls take out no more than was paid in: every bundle that lands was paid its
amount. That holds even if a builder doesn't keep a bundle's transactions contiguous, which no
bundle spec promises. No permissions are needed, and nothing binds a contract to an account.

| Who calls the forwarder | When | What follows |
|---|---|---|
| any concurrency account of Bruno's | in its sandwich | the check |
| anyone else | between a user's payment and Bruno's check | the payment goes to the payee early; Bruno's check then finds too little and reverts, so the bundle fails, the user's payment with it |
| the user, in their own transaction | after paying in | the same |
| anyone | between blocks | sends the payee what was left there by mistake |

Anyone could already send the payee tokens directly; nothing in Bruno relies on the payee
receiving only what it expects (the runner and the forwarder each measure within one call; the
treasury converts only configured tokens).

**Tips reach the payee.** The forwarder sends on everything, so a payer paying more than the quote
pays the payee more, as with any submission; nothing is stranded.

**Paid back with Bruno's own ETH.** The pre-fund includes the transactions' `value`, and what U
doesn't spend stays with U, so a user could pay in ETH from what Bruno sent. That's still sound:
the check passed only if the payee gained the amount, which is priced above everything Bruno
sends out, so Bruno nets its fee whatever the money's source.

## The budget

A sponsorship has `max_gas`, less Bruno's overhead (the pre-fund, the check and its transfer):
what's left is the gas the user's transactions may use between them. The same bound in wei
covers `value`: the pre-fund is at most `max_gas × the fee ceiling`. No new configuration: the
cap on what one sponsorship may draw is the cap on a transaction's gas.

## Fees, gas and simulation

- **Fees are fixed when the user signs.** A user who signs once can't sign again each block, so
  each transaction's max fee is the fee ceiling to its deadline, and its tip the relay's: the
  same on every transaction. The deadline is short, so the cap stays close. Fees above the
  quote's are taken, as any payment above the quote is a tip.
- **What the pre-fund leaves behind.** U is funded at the fee cap; what its transactions don't
  spend (their gas unused, and the cap above the block's price) stays with U. The quote prices
  the pre-fund whole: Bruno's cost is all of it.
- **Gas, where the request has none:** each transaction estimated by `eth_simulateV1` of the
  whole sandwich in order, the pre-fund applied, a tenth more as quotes add now. A transaction
  that reverts is refused at the quote.
- **What a quote simulates.** The check passes only if the payment is in the sandwich, and a
  client asking for a price hasn't made its payment yet. So the relay adds the payment it
  offers, and simulates the sandwich once for each token quoted, because each token's payment
  is a different transaction.
- **Simulated** with `eth_simulateV1` (validation on) when taken and each block, as anvil, geth
  and reth answer it. The relay's measurement already checks that the chain's nodes answer it
  (`measurements.py`), so preflight does too and a running relay reports it.
- **The pre-fund's gas** is the node's estimate of that transaction, to that account, as the
  sandwich runs it: one path, whether the account has code to run, or is empty and created by
  it. **The check's** is the forwarder's own run, from the gas schedule as the runner's is
  (`token_gas`, `eth_gas`), and the token's `transfer` and three `balanceOf` reads, measured for
  each configured token as its `transferWithAuthorization` is now, with what EIP-150's 63/64
  rule keeps back at each call below the transaction (the forwarder's call to the token, and a
  proxy token's to its implementation).
- **Authorizations and nonces:** carried by the pre-fund, an authorization is signed at U's
  current nonce, and U's transactions start after it; one in a type-4 transaction of U's own is
  signed at that transaction's nonce plus one, the nonce being used first. The quote says which
  nonce each takes.
- **One sponsorship in flight per account,** its nonces fixed when signed: a second is refused
  until the first lands or expires.
- **Priced:** the pre-fund's and the check's gas, all the ETH sent to U, Bruno's fee and the
  margins, in whatever the payment is made in.
- **A block it would lose in.** As for any submission, a sponsorship is sent for a block only if
  what it pays covers its charge there: Bruno's two transactions at that block's fees, the ETH
  sent to U, and the fee, at the pools' price then. One that doesn't cover it sits out the
  block, still pending, and goes in a later block that it does cover, or expires. Two things
  differ from a submission. The ETH sent to U is the larger part of the cost and is fixed in
  wei, so it's the token's price that moves the result, more than the base fee. And a block
  whose base fee is over the max fee U signed can't take U's transactions at all: the relay
  knows both numbers, so it sits that block out without simulating, and doesn't take the
  heavier reconciling path for it.
- **After a pre-fund has landed alone,** that ETH is spent whatever happens next. The rest is
  then worth sending whenever the payment covers the check's own gas.

## The interface

No new endpoints: a sponsorship answers the question the API already asks, run this call, paid
in this token. Only who makes the call differs.

**What a client can ask for:**

| | The call made by | Paid by | Proven by |
|---|---|---|---|
| a call (today) | Bruno's transaction, through the runner | a signed EIP-3009 transfer, made by the runner | the runner |
| a call paying for itself (today) | Bruno's transaction, through the runner | the call, paying the payee | the runner |
| a sponsorship, U's transactions in the middle | U's own transactions | them, paying the forwarder | the check |
| a sponsorship, the middle empty | Bruno's pre-fund, calling into U's code | U's code, paying the forwarder | the check |
| both | the pre-fund's call, then U's transactions | either | the check |

User operations keep their own endpoints: ERC-7769 fixes them.

**The request.** A sponsorship is one nested object, so one field says which kind it is, and
everything in it is its own; nothing outside it changes meaning:

```
quote:  {tokens, call, gas?, authorizationList? | authority?, payer?, deployment?, verifyGas?}  a call
        {tokens, sponsored: {authorizationList?, call?, gas?, transactions?: [...]}}            a sponsorship
submit: {call, gas, payment, …}                                                                  a call (as now)
        {sponsored: {authorizationList?, call?, gas, transactions: [signed]}, payment, …}       a sponsorship
```

A sponsorship's `payment` is a call's, `{token, payee, amount}`, and `maxBlockNumber` beside it:
its `payee` is the forwarder the quote named, and its `amount` what the check requires of it. It
carries no `transfer`: the payment is made by U's transactions or code.

Inside `sponsored`, each field means one thing, whatever else is there:

- **`authorizationList`:** U's signed EIP-7702 authorizations, carried by the pre-fund (type 4).
  U isn't the pre-fund's sender, so each is signed at U's current nonce, which it then
  increments: U's own transactions start after them. A plain EOA can be delegated and its new
  code called in the pre-fund, with no transaction of its own.
- **`call`:** what the pre-fund carries into U's code (`to` is U). U's delegate authenticates
  it, the call being made by Bruno's account, with U's own signature in it, as a call to an
  `authority` is now.
- **`gas`:** the gas the pre-fund's call into U is given, as a call's `gas` is: the client
  states it and Bruno prices its own overhead on top. The quote estimates it where it's not
  given (by `eth_simulateV1` of the sandwich, a tenth more) and answers it; the submission
  states it. An account with code runs it on receiving the pre-fund's ETH, `call` or no `call`,
  so it's needed whenever U has code, or is delegated by the pre-fund.
- **`transactions`:** what U sends, in order. In the quote, each is either signed (raw hex), or a
  transaction object as `eth_estimateGas` and `eth_simulateV1` take it,
  `{from, to, data?, value?, gas?, nonce?, maxFeePerGas?, maxPriorityFeePerGas?, authorizationList?}`;
  a list may mix the two. In the submission, all are signed.

**U, the account sponsored,** has no field of its own: it's every transaction's `from` (given, or
recovered from its signature), all the same; with no transactions, the call's `to`. Each
authorization's authority, recovered, must be U too. At least one of `call` and `transactions`
is given. `sponsored` beside a top-level `call`, or a transaction from another account, is
invalid params naming the field, as `authorizationList` beside `authority` is now.

**Its answer.** As a call's (`validUntilBlock`, `quotes: [{token, amount, breakdown}]`), with:
- for an unsigned transaction, what the quote filled in, to sign: `chainId`, `nonce`, `gas`
  (estimated by `eth_simulateV1` of the sandwich, a tenth more, if not given), `maxFeePerGas`
  (the fee ceiling to the deadline), `maxPriorityFeePerGas` (the relay's tip), `type`; what the
  request gave is used, its fees at least what's needed;
- for a signed one, nothing: it's priced as signed, refused if its max fee doesn't cover the fee
  ceiling to the deadline, saying the max fee needed;
- each authorization's nonce to sign at, for an authorization not yet signed;
- the forwarder, and per token a payment transaction offered, ready to sign after U's own:
  `transfer(forwarder, amount)` to the token, or `value: amount` to the forwarder, its own gas
  in the amount. Offered, not required: U pays however it likes between the pre-fund and the
  check, by its code in the pre-fund's call among them.

**Taking it.** No quote is kept, so a submission is never compared with one: it's planned and
priced as it was submitted, as a call's is. A submission is taken if its transactions are all
U's, at consecutive nonces from U's current one (after its authorizations), on this chain, each
at a max fee covering the fee ceiling to its deadline as of now (more is a tip), within the
budget, its payment's `payee` the current forwarder, and the sandwich simulates; then priced
(underpaid says what's required) and taken as any submission. Its status is a submission's
(`GET /v1/submissions/{id}`, `postpaid_getStatus`), its id the hash of what it was taken with,
with the same stages.

**The pre-fund's call is leaner sending too.** A call into a delegated account, made directly
from Bruno's account and proven by the check, is what the runner does for a call to an
`authority` today, at the deployer's overhead (195,715 gas on Amsterdam). The client sees where
the call pays (the payee, measured by the runner, or the forwarder, checked after), so
the request chooses, by `sponsored`; the relay doesn't change the path behind it.

The client keeps one flow (quote, sign, submit), signing typed data for a transfer, or the
answer's transactions and authorizations for a sponsorship, with any wallet that signs without
sending (viem, ethers, Privy, Turnkey). SPEC.md, OpenAPI and OpenRPC each gain the variant; the
refusals catalogue the sponsorship's own (one in flight for the account, over the budget, a
forwarder not the current one, a transaction or authorization not U's).

## Structure: what the code still needs

This section first proposed a new layer, the order, to be built before anything else. Most of it
has been built since for other reasons: a plan holds several transactions (`Plan.steps`, each
with its own proof), they're simulated together in order by one `eth_simulateV1`
(`SignedSimulation`), a transaction's cost includes the wei it sends (`CostBasis`), and the
funding and the treasury already send atomic bundles (`atomic_params`). The batches sign every step of a
plan at the account's consecutive nonces, and a plan of several steps goes as an atomic bundle
of its own. A sponsorship needs two more things.

- **A step that isn't Bruno's.** Every step is a transaction a concurrency account signs. The
  middle of a sandwich is U's transactions, already signed, from another sender at its own
  nonces and fees.
- **Something to store.** The queue, a tracked submission and the wire all hold a `Submission`,
  which is one call with an amount and a deadline. A sponsorship isn't one call, so what's
  taken and stored needs a type of its own, beside `Submission`. This is the part still to
  design.

## What each side risks

- **Bruno.** The pre-fund comes first, the user having no gas to pay before it. A builder that
  breaks a bundle's atomicity could land it alone: atomicity is the premise, as it is for the
  treasury's runs and the funding. Bounded by the budget.

**Reconciling, and what it reads.** Nodes count every call against a rate limit, in a batch or
not, so a sponsorship in flight reads about what a submission does. Each block the relay looks
for the check's receipts, as it looks for any submission's transaction, and simulates the
sandwich, validation on. A check that landed means everything before it landed. A sandwich that
still simulates means nothing has changed: U's nonce and balance are what they were, because the
simulation validates them, so the relay reads nothing of U's.

Only when the sandwich stops simulating does the relay take the heavier path: it reads U's nonce
and A's, and the receipts of the pre-fund and of U's transactions, to learn what landed. Those
reads cost calls only in the blocks where something has gone wrong.

**A pre-fund landing without its check.** The heavier path finding the pre-fund landed, in a
block where its check didn't, means the bundle was broken: A's nonce n is used, and U holds
Bruno's ETH. It's reported as an error with what the pre-fund cost, and it's never funded again.
U's transactions are still signed, and U can now pay for them, so until the deadline the relay
keeps sending the rest as an atomic bundle. The bundle is whatever hasn't landed: those of U's
transactions still ahead of U's nonce, and the check, signed at A's nonce as read. A transaction
that has landed is left out, because a builder refuses a whole bundle over one transaction whose
nonce is used. If the rest lands, Bruno is paid as the sandwich would have paid it. The
sponsorship is rejected only when the deadline passes or U's nonce moves some other way. A
payment that landed in the forwarder without its check isn't lost: the next call to it for
that asset, anyone's, forwards it to the payee.

**Reorgs.** An orphaned block was gossiped before it was orphaned, so its transactions are
public and anyone may land one of them alone. Every other transaction of Bruno's checks its own
payment, so this is the first it matters for. On mainnet 903 of 805,546 slots were re-orged
between Pectra and 27 August 2025, about 0.11%
([Decipher, on ethresear.ch](https://ethresear.ch/t/post-pectra-effects-on-ethereum-reorg-rate-propagation-and-block-size/23018)).
A block's contents have also left a relay without the block landing: in April 2023 a proposer
took them with an invalid header and unbundled about $20M of searchers' bundles
([Flashbots' post-mortem](https://collective.flashbots.net/t/post-mortem-april-3rd-2023-mev-boost-relay-incident-and-related-timing-issue/1540)).
Nobody can cause a reorg when they choose, and only U gains from a pre-fund landing alone. So
the risk is accepted: about a thousandth of a pre-fund, to a user who is both hostile and quick,
bounded by the budget. How ePBS treats a payload revealed late isn't checked here.
- **The user.** A payment into the forwarder counts only inside the bundle that forwards
  it. Were a user's transaction landed outside it, by a builder leaking or unbundling it while
  the account held the ETH for it, its payment would go to the payee, unsponsored. A signed
  transaction also doesn't expire: one that didn't land stays valid at its nonce until U's nonce
  moves, which U's next sponsorship does. The spec says both.

## Testing

The whole sandwich is the unit, for simulating and for testing: one `eth_simulateV1` of the
pre-fund, U's transactions and the check, in order, validation on, whenever the relay
simulates (estimating, taking, each block). No part is simulated alone: each depends on the
state the one before leaves (U's balance, the nonces, the payment in the forwarder). On
anvil, through the mock builder, against both relays, on Prague and Amsterdam; each new test
seen failing first, bodies asserted; amounts measured, no tuned constants.

1. **The forwarder,** built (`test_forwarder.py`): its generated code deployed through
   Arachnid on anvil, against vendored tokens, and its own gas matching receipts exactly on
   both forks. A payer's own transfer answering false leaves the forwarder holding nothing,
   which is its Underpaid(): a token can't answer false to the forwarder's own transfer of what
   it holds.
2. **The estimates are the sandwich's:** a sequence whose second transaction needs the first
   (an approve, then a swap using it), its quoted gas signed as is, headroom off, lands, which
   it can't if any transaction were estimated alone; a pre-fund to an account whose code works
   on receiving ETH, signed at the node's estimate, lands. A sponsorship's simulation is one
   `eth_simulateV1` request (`served.node`).
3. **Sandwiches that land,** quoted, signed (`eth_account`), taken, built, included; the
   payee's gain, less all A spent (the pre-fund's value, both transactions' gas), at least
   Bruno's fee: a plain EOA's action and the offered payment; the middle empty, the pre-fund
   delegating U and calling a vendored delegate (Calibur) whose code pays; both; paid in ETH,
   U's transaction producing it; transactions signed in the quote, and a mixed list; a tip,
   all of it to the payee; fees above the ceiling.
4. **Refused,** with what the refusal says: a transaction or authorization not U's (invalid
   params naming it); a max fee under the ceiling; over the budget; a second sponsorship in flight;
   a forwarder not the current one; unpaid or short (simulation failed).
5. **Taken, then the chain changes before its block:** U's tokens drained, U's nonce used by a
   transaction of its own, the user's transaction made to revert, someone else's call to the
   forwarder first: the block holds nothing of Bruno's, A's nonce unmoved, U unfunded, and the relay
   drops it reconciling.
6. **A builder breaking atomicity:** a mock builder fault landing a bundle's first transaction
   alone; the relay reports what it cost (at most the budget), never funds it again, and lands
   the rest (U's transactions, and the check at A's nonce as read), paid as the sandwich would
   have been; past the deadline, it's rejected. A sponsorship that still simulates asks the
   node for nothing of U's (`served.node`); one that stops simulating reads U's nonce and the
   receipts.
7. **Storage and switchover:** the Worker's stored sponsorship survives an eviction, its
   forwarder with it; one stored with a previous forwarder is checked against that one.
8. **Preflight:** a node refusing `eth_simulateV1` (`refused_once`), reported.
9. **Live,** smoke only, when asked: one sponsorship landing on Sepolia.

## Checklist

Each on both relays, in step, unless it's Python's alone.

- [ ] **What the code still needs** ([above](#structure-what-the-code-still-needs)): a step
      that's another account's signed transaction; a type for what's taken and stored, beside
      `Submission`. Nothing else changed (the existing suite passing).
- [x] **The forwarder:** assembled from the payee (Python and the Worker), in the vectors; its
      tests. Its address computed once per chain when the relay opens is still to do, with the
      sponsorship order.
- [x] **`make <network>-forwarder-deploy`** deploys it for the configured payee; **preflight**
      checks its code (Python's alone).
- [x] **`eth_simulateV1` checked:** by the relay's measurement, which preflight runs, and asked
      of each node by `make rpc-check`.
- [ ] **A token's `transfer` and `balanceOf` measured,** for the check's gas, beside its
      `transferWithAuthorization`.
- [ ] **The sponsorship order:** its transactions, asset, amount and forwarder; the
      pre-fund (authorizations and call included) and the check signed each block, the
      sandwich as one atomic bundle of its own; one `eth_simulateV1`; one in flight per
      account; the budget; reconciling, a pre-fund landing alone included. The Worker's stored
      form.
- [ ] **The wire:** `sponsored` in the quote and the submission (`wire.py`'s Structs, the JSON-RPC
      methods' params), the answer's fields to sign; the refusals in the catalogue
      (`refusals.py`, exported).
- [ ] **`make vectors`:** OpenAPI and OpenRPC regenerated from the Python, the Worker's
      `refusals.json` and test vectors with them.
- [ ] **The tests** above.
- [ ] **The client** (`@mailam/bruno`): quote, sign the answer's transactions and authorizations
      with the wallet given, submit; its tests in Docker (`make`).
- [ ] **SPEC.md:** the sponsorship variant, the forwarder, what the user risks.
- [ ] **The user docs:** a guide page for sponsorship with a whole, tested script; the REST and
      JSON-RPC guides; `llms.txt`'s routes.
- [x] **DEPLOYMENT.md:** deploying the forwarder, and what preflight checks.
- [ ] **The design pages,** once built: the decisions only nothing else says (why the sandwich,
      why the payee's gain, why no permissions); this plan trimmed to what's left.

## Where it works

Chains whose builders take atomic bundles of several transactions (Ethereum, Sepolia): the
treasury's runs and the funding go there already. Unichain's builder took only single-transaction
bundles, and Unichain is no longer served (OP Enterprise: [More chains](morechains.md)).
Elsewhere the pre-fund could land without the payment.

## The deadline

A sponsorship's deadline is the least of every deadline that applies to it: the one the request
gives, if it gives one (`maxBlockNumber`, as a submission's), any that what the user signed
carries, and `max_deadline_blocks` ahead, the furthest any submission's deadline may be. Nothing
new is configured. A plain transaction carries no deadline, so for `transactions` the sources
are the request's and `max_deadline_blocks`.

The deadline is fixed at the quote, because the quote fills in each transaction's max fee, the
fee ceiling to that deadline. So the quote's request takes the optional `maxBlockNumber`, its
answer says the deadline it used, and the submission gives it back, as a submission gives its
`maxBlockNumber` now.

A shorter deadline costs less, because a base fee can rise an eighth a block. The ceiling, and
with it the pre-fund and the price, is 1.42 times the base fee three blocks ahead and 3.25 times
ten ahead (`max_deadline_blocks` in the default configuration, two minutes at 12 s). A request
that wants the lower price gives a nearer `maxBlockNumber`.

## Set aside

- **A payment address for each submission** (a fresh CREATE2 address, its sweeper deployed by
  the check): a new balance slot every time (107,920 gas on Amsterdam) and the deployer's
  overhead (195,715) on every check.
- **A sponsor contract recording the payee's balance** for the check to compare: storage of its
  own, and sound only if a bundle's transactions are contiguous.
- **A deposit address for each user:** the payment swept out of the bundle, so not atomic, with
  gas to give each address and the addresses to keep.
- **The concurrency account as the pay-to address:** not allocated until the batch, after the
  user signs.
- **Multicall3** (anyone can move what it holds): it sends only the amount, so a tip stays
  behind for whoever sweeps it; `aggregate3` checks only that a call succeeded, so a token
  answering false passes; and it can't forward ETH: it has no `receive`, and `aggregate3Value`
  requires `msg.value` to equal what it sends
  ([Multicall3.sol](https://raw.githubusercontent.com/mds1/multicall3/main/src/Multicall3.sol)).
