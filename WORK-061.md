# 0.6.1: what we're working on

A working note, kept out of the 0.6.0 branch and deleted before 0.6.1 merges. 0.6.0 (PR #18) is
the interface (the quote taking the call, the breakdown, the transfer to sign, refusals), the
funding as a bundle, the EntryPoint called directly, and calls spread across a chain's nodes.

## The aim

Three things, which turn out to share one model and one contract:

- Sending for less. Almost every submission is sent as a runner, a contract created for that one
  transaction. Creating it costs 32,115 gas on Prague and 195,715 on Amsterdam, where EIP-8037
  charges 183,600 of it for the runner's account. Some submissions don't need one.
- Atomic sponsorship: paying the gas for transactions a user's own wallet signed
  ([sponsorship.md](docs/development/plans/sponsorship.md), designed and reviewed).
- EntryPoint v0.9, which only an account without code may call, so the runner can't.

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
| An op paying by its fees, v0.9 | `handleOps`, directly | the EntryPoint's own rules | step 4 |
| An op paying by its calls, v0.9 | `handleOps`, directly, then the forwarder | the forwarder | step 4 |
| A sponsorship | the user's own transactions, between Bruno's pre-fund and the forwarder | the forwarder | step 5 |

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

### The forwarder

The release contract of [sponsorship.md](docs/development/plans/sponsorship.md), where it is
designed in full. Whoever is paying pays it, where they would have paid the payee. Bruno's last
transaction in the bundle calls it with the amount: it sends everything it holds to the payee,
and reverts unless that is at least the amount. It has no owner and no state, and anyone may
call it, since it pays only the payee.

It checks that enough was there to collect, not that this bundle's call sent it. That is
accepted: Bruno is paid either way. It relies on the bundle being all or nothing, which
everything here already relies on.

## The work, in order

Nothing below is committed. The working tree holds everything up to step 2. Steps 3 to 5 are
not started.

### In the working tree

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
  transfer first. Whether the runner should change to match is not decided.
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

### Step 3: the forwarder

The two release contracts as sponsorship.md has them, one for ETH and one for ERC-20s: assembled
from the payee, at addresses each relay computes, deployed through Arachnid's deployer by the
deployer CLI, in the vectors. Deploying them on Ethereum and Sepolia is the user's to run.

### Step 4: EntryPoint v0.9

v0.9's `handleOps` refuses any caller that has code or isn't the transaction's origin
(`tx.origin == msg.sender && msg.sender.code.length == 0`). A concurrency account has no code,
so it can call it. Nothing with code can, so the runner can't wrap it.

- An op paying by its fees is sent directly, as on v0.7 and v0.8.
- An op paying by its calls is sent directly, with the forwarder called after, in a bundle of
  its own. The op pays the forwarder.
- A send finds what an op pays by simulating `handleOps` from a helper contract that reads the
  payee's balances either side (`sending`, `balances_code`). v0.9 would refuse the helper. The
  simulation becomes separate calls from an account without code, by `eth_simulateV1`.
- v0.9 added to `userops.json` and vendored onto the local chain.

Not designed yet: how a client learns that an op paying by its calls on v0.9 pays the forwarder
and not the payee.

Where the restriction came from. It arrived in the v0.9 release commit (86fcd84, 2025-09-15),
squashed into [#609](https://github.com/eth-infinitism/account-abstraction/pull/609) and merged
on 2025-11-12, replacing v0.8's OpenZeppelin `ReentrancyGuardTransient`. Neither the release
notes nor ERC-4337 mention it. An outside reviewer asked on #609 for it to be documented
([2025-12-08](https://github.com/eth-infinitism/account-abstraction/pull/609#issuecomment-3629285156))
and got no reply. The only reason on record is a maintainer's comment closing
[#623](https://github.com/eth-infinitism/account-abstraction/pull/623) (2026-08-10): bundlers
have pools of addresses and don't need EIP-7702, and "there are other vulnerabilities we wanted
to prevent when source address is not an EOA", which it doesn't name. We raised it in
[#633](https://github.com/eth-infinitism/account-abstraction/issues/633) (2026-11-23), tracked
in [upstream.md](docs/development/upstream.md).

### Step 5: atomic sponsorship

As [sponsorship.md](docs/development/plans/sponsorship.md) has it, whose checklist is the order
of work. Steps 1 and 3 are its groundwork. What it still needs beyond them is a step that is
another account's signed transaction, and a type for what is taken and stored beside
`Submission`.

Nothing in the plan is left to decide. Settled on 2026-11-23:

- All three forms are built together: the user's transactions in the middle, a call the pre-fund
  carries with the middle empty, and both.
- A sponsorship stores the release contract its user paid, as a submission stores its payee.
- The deadline is bounded by `max_deadline_blocks`, as a submission's is. No new key.

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

- **The client without `-dev`.** Its API settled, its `.d.ts` written as the reference a model
  reads, OpenAPI and OpenRPC aligned with it. Once it's not `-dev`, the API is a promise.
- **The funding ahead of the runs:** put in a dropping bundle, it would save a block.
- **A Safe by one signature** ([safe.md](docs/development/plans/safe.md)): Safe's contracts
  vendored, deployed on first use, the SafeTx with a payment leg. Its four-item verification
  list first.
- **Money hygiene:** the cold wallet; persistence for the Python relay's queue and treasury
  snapshot, which it forgets on restart.
- **Base** ([morechains.md](docs/development/plans/morechains.md)): set aside while Bruno
  sends only through `eth_sendBundle`.
