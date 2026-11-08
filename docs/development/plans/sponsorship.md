---
description: "Plan: atomic sponsorship, transactions the user's own wallet signed but can't pay gas for, sandwiched in one atomic bundle between Bruno's pre-fund of the account and a check that releases the payment to the payee, in any token or ETH"
---

# Plan: atomic sponsorship

A user's wallet signs transactions it can't pay the gas for: an EOA holding a token and no ETH.
Bruno sandwiches them in one atomic bundle, between a transaction pre-funding the account with
the ETH they need and a check that reverts the bundle unless Bruno was paid. The bundle lands
whole or not at all, so Bruno's ETH reaches the account only in the block where the user pays
for it. Recorded 2026-11-03, from the [agent wallets](agent-wallets.md) research: it's the
integration for every wallet that signs a transaction without sending it (Privy, Turnkey,
Dynamic, Openfort, an agent's own key). The design below settled 2026-11-08, to review in full
before building.

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
   U holds. It may carry U's EIP-7702 authorizations (type 4), and a call into U's code.
2. **The user's transactions, as signed,** at U's consecutive nonces: none, one or several. A
   plain EOA's transaction is one call, so a call and its payment are two transactions (a swap,
   then the payment), or one through an EIP-7702 delegate that batches.
3. **The check:** from A, at nonce n+1, that contract's release, for at least the amount.

Between the pre-fund and the check, U pays the release contract for the payment's asset, however
it pays: in one of its transactions, or in none of them. Sending ETH to an account can run code:
an account with code (an EIP-7702 delegate, a smart account) runs it when the pre-fund reaches
it, so its code may make the payment, and the pre-fund may carry the call it's to make, and the
authorization delegating U first. Then the sandwich is the pre-fund and the check alone, the
middle empty.

A is whichever concurrency account the batch picks: the pay-to address doesn't depend on it, so
nothing is allocated when quoting. A user transaction that reverts fails the bundle: nothing
lands, nobody pays.

## The release contracts

Two, each with the payee in its code, so whatever they release goes to the payee and nowhere
else. No owner, no storage of their own, no selector dispatch: each is assembled opcode by
opcode, as the deployer's runtime is (`deployer.py`), with no Solidity.

- **For ETH.** Empty calldata accepts ETH (how a user pays). Calldata of one word, `least`:
  requires its balance to be at least `least`, then sends all of it to the payee, reverting if
  that call fails. Its account has code and a nonce, so it's never empty (EIP-161): its balance
  going to zero creates or clears nothing, and no unit is kept.
- **For an ERC-20.** Calldata of two words, `token` and `least`; ETH sent to it reverts. It reads
  what it holds, and the payee's balance; calls `transfer(payee, held − 1)`, its return value
  ignored (USDT's is empty); reads the payee's balance again; and requires the payee's gain to be
  at least `least`. All in one call, so nothing else moves the payee's balance between the two
  reads: the runner's own rule, the payment as a balance difference. A token answering false
  moves nothing, a taxed token's gain is what arrived, a token that burns or misroutes shows no
  gain: each reverts unless the payee gained the amount.

The ERC-20 contract keeps one unit of each token, so its balance slot in the token is never
cleared and created again (on Amsterdam a new slot is 107,920 gas, `forks.py` `storage_set`).
It's never a payer's: Bruno seeds one unit of each configured token, and a quote for a token
the contract holds none of adds one unit to the amount.

**Where they are.** Deployed through Arachnid's deterministic deployer, salt 0, as Bruno's
deployer is. Their initcode holds the payee, so their addresses are a function of the payee and
the code: each relay computes its pair once per chain when it opens, beside the payee it holds,
and the vectors check both relays generate the same code. Nothing is configured: a new payee, or
new code, is a new pair. The deployer CLI (`make <network>-deploy`, by hand) deploys the pair
for the configured payee, a pair already there left alone; `make preflight` checks the code at
the computed addresses is the generated runtime, so a relay whose pair isn't on a chain isn't
deployed.

**Switching over.** The release contract a user pays is part of what they sign, so each
sponsorship keeps it: the submission names it (its quote gave it), the relay takes it only if
it's the current pair's, and stores it with the sponsorship (the Worker in its stored form).
Each block's check calls the stored contract, never the current one: a sponsorship taken before
a new pair is checked against the contract its user paid, until it lands or expires, while new
quotes name the new pair. A quote from before the switch, submitted after it, is refused, to
quote again; nothing's paid or sent by then. An old pair stays deployed and needs nothing: it's
bound to its payee.

## Why it's sound

Bruno's own signed transaction is the condition: its bundle lands only if its release sent the
payee at least the amount. Anyone may call a release, but it pays only the payee. Each release
takes everything it counted, and the contract never goes below its unit, so in any block as
built the releases take out no more than was paid in: every bundle that lands was paid its
amount. That holds even if a builder doesn't keep a bundle's transactions contiguous, which no
bundle spec promises. No permissions are needed, and nothing binds a contract to an account.

| Who calls the release | When | What follows |
|---|---|---|
| any concurrency account of Bruno's | in its sandwich | the check |
| anyone else | between a user's payment and Bruno's release | the payment goes to the payee early; Bruno's release then finds too little and reverts, so the bundle fails, the user's payment with it |
| the user, in their own transaction | after paying in | the same |
| anyone | between blocks | sends the payee what was left there by mistake |

Anyone could already send the payee tokens directly; nothing in Bruno relies on the payee
receiving only what it expects (the runner and the release each measure within one call; the
treasury converts only configured tokens).

**Tips reach the payee.** A release forwards everything, so a payer paying more than the quote
pays the payee more, as with any submission; nothing is stranded.

**Paid back with Bruno's own ETH.** The pre-fund includes the transactions' `value`, and what U
doesn't spend stays with U, so a user could pay in ETH from what Bruno sent. That's still sound:
the check passed only if the release got the amount, which is priced above everything Bruno
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
- **Simulated** with `eth_simulateV1` (validation on) when taken and each block, as anvil, geth
  and reth answer it; preflight checks the chain's nodes, as it checks what else the relay
  relies on.
- **The pre-fund's gas** is the node's estimate of that transaction, to that account, as the
  sandwich runs it: one path, whether the account has code to run, or is empty and created by
  it. **The check's** is the release contract's run, from the gas schedule as the runner's is.
- **Authorizations and nonces:** carried by the pre-fund, an authorization is signed at U's
  current nonce, and U's transactions start after it; one in a type-4 transaction of U's own is
  signed at that transaction's nonce plus one, the nonce being used first. The quote says which
  nonce each takes.
- **One sponsorship in flight per account,** its nonces fixed when signed: a second is refused
  until the first lands or expires.
- **Priced:** the pre-fund's and the check's gas, all the ETH sent to U, Bruno's fee and the
  margins, in whatever the payment is made in.

## The interface

No new endpoints: a sponsorship answers the question the API already asks, run this call, paid
in this token. Only who makes the call differs.

**What a client can ask for:**

| | The call made by | Paid by | Proven by |
|---|---|---|---|
| a call (today) | Bruno's transaction, through the runner | a signed EIP-3009 transfer, made by the runner | the runner |
| a call paying for itself (today) | Bruno's transaction, through the runner | the call, paying the payee | the runner |
| a sponsorship, U's transactions in the middle | U's own transactions | them, paying a release contract | the check |
| a sponsorship, the middle empty | Bruno's pre-fund, calling into U's code | U's code, paying a release contract | the check |
| both | the pre-fund's call, then U's transactions | either | the check |

User operations keep their own endpoints: ERC-7769 fixes them.

**The request.** A sponsorship is one nested object, so one field says which kind it is, and
everything in it is its own; nothing outside it changes meaning:

```
quote:  {tokens, call, gas?, authorizationList? | authority?, payer?, deployment?, verifyGas?}  a call
        {tokens, sponsored: {authorizationList?, call?, transactions?: [...]}}                  a sponsorship
submit: {call, gas, payment, …}                                                                  a call (as now)
        {sponsored: {authorizationList?, call?, transactions: [signed]}, token, release}         a sponsorship
```

Inside `sponsored`, each field means one thing, whatever else is there:

- **`authorizationList`:** U's signed EIP-7702 authorizations, carried by the pre-fund (type 4).
  U isn't the pre-fund's sender, so each is signed at U's current nonce, which it then
  increments: U's own transactions start after them. A plain EOA can be delegated and its new
  code called in the pre-fund, with no transaction of its own.
- **`call`:** what the pre-fund carries into U's code (`to` is U). U's delegate authenticates
  it, the call being made by Bruno's account, with U's own signature in it, as a call to an
  `authority` is now.
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
- per token, its release contract, and a payment transaction offered, ready to sign after U's
  own: `transfer(release, amount)` to the token, or `value: amount` to the ETH contract, its own
  gas in the amount. Offered, not required: U pays however it likes between the pre-fund and the
  check, by its code in the pre-fund's call among them.

**Taking it.** A submission is taken if its transactions are all U's, at consecutive nonces from
U's current one (after its authorizations), on this chain, at fees at least the quote's (more is
a tip), within the budget, its `release` the current pair's, and the sandwich simulates; then
priced (underpaid says what's required) and taken as any submission. Its status is a
submission's (`GET /v1/submissions/{id}`, `postpaid_getStatus`), its id the hash of what it was
taken with, with the same stages.

**The pre-fund's call is leaner sending too.** A call into a delegated account, made directly
from Bruno's account and proven by the check, is what the runner does for a call to an
`authority` today, at the deployer's overhead (195,715 gas on Amsterdam). The client sees where
the call pays (the payee, measured by the runner, or the release contract, checked after), so
the request chooses, by `sponsored`; the relay doesn't change the path behind it.

The client keeps one flow (quote, sign, submit), signing typed data for a transfer, or the
answer's transactions and authorizations for a sponsorship, with any wallet that signs without
sending (viem, ethers, Privy, Turnkey). SPEC.md, OpenAPI and OpenRPC each gain the variant; the
refusals catalogue the sponsorship's own (one in flight for the account, over the budget, a
release contract not the current one's, a transaction or authorization not U's).

## Structure: orders

What's taken is the user's signed transactions, an asset, an amount and its release contract,
sent as an atomic group of three or more transactions. The proof of payment (`Proof`, plan.py)
covers one transaction, so this is an abstraction a level above it: the order, what was taken
and how it's sent. Each kind of order says what it pays at a block's base fee; the transactions
it sends (Bruno's to sign, and those given signed); whether they go droppable, as runs do, or as
an atomic bundle of their own, as the funding does; and how it simulates (`eth_call` of one
transaction, or `eth_simulateV1` of the sequence). Today's submission, with its proof, is one
kind; a sponsorship another. Batches, the queue and pricing handle orders alike.

## What each side risks

- **Bruno.** The pre-fund comes first, the user having no gas to pay before it. A builder that
  breaks a bundle's atomicity could land it alone: atomicity is the premise, as it is for the
  treasury's runs and the funding. Bounded by the budget.

**A pre-fund landing without its check.** Reconciling each block, the relay reads the receipts
of what it sent (as for any submission) and A's nonce. A pre-fund that landed in a block where
its check didn't means a builder broke the bundle: A's nonce n is used, and U holds Bruno's ETH.
The sponsorship then finishes, rejected for that reason, and is reported as an error with what
the pre-fund cost; it's never funded again, and A's next transaction is signed at its nonce as
read. A payment that landed in the release contract without its check isn't lost: the next
release of that contract, anyone's, forwards it to the payee.
- **The user.** A payment into a release contract counts only inside the bundle that releases
  it. Were a user's transaction landed outside it, by a builder leaking or unbundling it while
  the account held the ETH for it, its payment would go to the payee, unsponsored. The spec says
  so.

## Testing

The whole sandwich is the unit, for simulating and for testing: one `eth_simulateV1` of the
pre-fund, U's transactions and the check, in order, validation on, whenever the relay
simulates (estimating, taking, each block). No part is simulated alone: each depends on the
state the one before leaves (U's balance, the nonces, the payment in the release contract). On
anvil, through the mock builder, against both relays, on Prague and Amsterdam; each new test
seen failing first, bodies asserted; amounts measured, no tuned constants.

1. **The release contracts:** their generated code deployed through Arachnid on anvil, against
   vendored tokens. ERC-20: paid at least `least`, the payee gains all less the unit, a tip
   with it; short, it reverts; a token answering false, a short payment reverts (a vendored
   mainnet token that does); a taxed token, the gain counted is what arrived (a vendored
   fee-on-transfer token); anyone may call it, only the payee gains. ETH: empty calldata takes
   a payment; `least` releases all, or reverts short. The check's gas from the schedule matches
   receipts exactly, on both forks. The vectors hold the code and addresses for a fixed payee.
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
   all of it to the payee; fees above the quote's.
4. **Refused,** with what the refusal says: a transaction or authorization not U's (invalid
   params naming it); fees below the quote's; over the budget; a second sponsorship in flight;
   a release contract not the current pair's; unpaid or short (simulation failed).
5. **Taken, then the chain changes before its block:** U's tokens drained, U's nonce used by a
   transaction of its own, the user's transaction made to revert, someone else's release
   first: the block holds nothing of Bruno's, A's nonce unmoved, U unfunded, and the relay
   drops it reconciling.
6. **A builder breaking atomicity:** a mock builder fault landing a bundle's first transaction
   alone; the relay finishes the sponsorship, reports what it cost (at most the budget), never
   funds it again, and its next transaction from A is signed at A's nonce as read.
7. **Storage and switchover:** the Worker's stored sponsorship survives an eviction, its release
   contract with it; one stored with a previous pair's contract is checked against that one.
8. **Preflight:** a pair missing, a pair whose code isn't the generated runtime, a node refusing
   `eth_simulateV1` (`refused_once`), each reported.
9. **Live,** smoke only, when asked: one sponsorship landing on Sepolia.

## Checklist

Each on both relays, in step, unless it's Python's alone.

- [ ] **Orders:** the abstraction above `Proof`, today's submission its first kind; batches, the
      queue and pricing through it, nothing else changed (the existing suite passing).
- [ ] **The release contracts:** assembled from the payee (Python and the Worker), their addresses
      computed once per chain when the relay opens; in the vectors.
- [ ] **The deployer CLI** deploys the pair for the configured payee; **preflight** checks the
      pair's code and that the nodes answer `eth_simulateV1` (Python's alone).
- [ ] **The treasury** converts a token's balance less one unit.
- [ ] **The sponsorship order:** its transactions, asset, amount and release contract; the
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
- [ ] **SPEC.md:** the sponsorship variant, the release contracts, what the user risks.
- [ ] **The user docs:** a guide page for sponsorship with a whole, tested script; the REST and
      JSON-RPC guides; `llms.txt`'s routes.
- [ ] **DEPLOYMENT.md:** deploying the pair, and what preflight checks.
- [ ] **The design pages,** once built: the decisions only nothing else says (why the sandwich,
      why the payee's gain, why no permissions); this plan trimmed to what's left.

## Where it works

Chains whose builders take atomic bundles of several transactions (Ethereum, Sepolia): the
treasury's runs and the funding go there already. Unichain's builder took only single-transaction
bundles, and Unichain is no longer served (OP Enterprise: [More chains](morechains.md)).
Elsewhere the pre-fund could land without the payment.

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
  answering false passes; and it can't release ETH: it has no `receive`, and `aggregate3Value`
  requires `msg.value` to equal what it sends
  ([Multicall3.sol](https://raw.githubusercontent.com/mds1/multicall3/main/src/Multicall3.sol)).

## The treasury's one unit

Settled alongside: the treasury converts a token's balance less one unit, so the payee's balance
slot in it is never cleared, and the next payment doesn't create it again (107,920 gas on
Amsterdam). Today it converts `min(balance, …)` (`treasury.py`), and
the relay prices the slot's creation when the payee holds none (`transfer_gas`'s
`payee_holds`). For every kind of submission, not only sponsorship.
