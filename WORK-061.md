# 0.6.1: what's left

A holding place for what's still to do. It's deleted before the branch merges
([releasing](docs/development/releasing.md), step 1), and whatever is still open then goes into
`WORK-062.md`.

## Before merging

- `make check-sepolia`, about 6 minutes, testnet gas only. A sponsorship's own methods haven't
  run on Sepolia, and neither has the fix for a node answering "block not found". The test
  wallet's forwarder is deployed there, at 0x98c78a62529EBD601238718389A07fFb30c264cA.
- The production payee's forwarder, on Sepolia and on Ethereum, with
  `make <net>-forwarder-deploy`. Give `PAYEE=` if the Worker's payee isn't the one in
  `.secrets/payee.address`. It can go at any time before the Worker is deployed.
- To decide: whether a sponsorship's quote runs the sequence once at the gas limits it's about
  to give, and refuses if it fails there. It wouldn't make a dependent sequence quote without
  its gas, but an answered quote could then be signed and submitted as it is.
- The release's own steps ([releasing](docs/development/releasing.md)).

## After deploying

- The REST and JSON-RPC pages' sponsorship example is from a local chain, and says so. Take it
  again from Sepolia.
- Those pages' `postpaid_config` examples show Sepolia's `forwarder` as computed for its payee,
  0x2Dc27cb0fbce57bD1122b2CAa61D05d9E6B23838. Check the deployed relay answers the same.

## Open

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

### The payee's one unit

Keeping one base unit of a token with the payee is for Amsterdam's gas schedule, where setting
a balance slot from zero costs far more than changing it. It applies to every token, and the
code applies it only to tokens taken over x402 (USDC), in three places on each relay:

- The measurement reads the payee's balance only of those tokens.
- The treasury reports the payee's holdings to the measurements only for those tokens.
- The treasury keeps one unit back only of those tokens, so it can convert the payee's whole
  balance of USDT, DAI or WETH.

So a sponsorship paid in another token is always priced as if the payee's slot had to be set
from zero. To do: the payee's holdings read and reported for every token, and one unit of
every token kept.

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
