# 0.6.1: notes, and what's left

A note kept on the `next-0.6.1` branch and deleted before it merges
([releasing](docs/development/releasing.md), step 1). What 0.6.1 built is in PR #19's
description and the commits. What's still open here is carried into `WORK-062.md` after the
release.

## Where it stands

Everything is committed and pushed, and nothing is deployed. PR #19 is a draft.

## Before merging

Each is the user's to start, one at a time.

1. `make check-sepolia`, about 6 minutes, testnet gas only. A sponsorship landed on Sepolia on
   2026-11-25, before it had methods of its own, so its own paths haven't run live. The same
   run had both x402 runs fail on a node a block behind ("block not found"), fixed since and
   not run there again.
2. The production payee's forwarder, on Sepolia and on Ethereum, with
   `make <net>-forwarder-deploy`. Give `PAYEE=` if the Worker's payee isn't the one in
   `.secrets/payee.address`. It needs no tag and no merge: the forwarder's address follows from
   the payee alone, and 0.6.0's Worker doesn't use it.
3. The release, a step at a time ([releasing](docs/development/releasing.md)): this note
   deleted, the merge, the tag, `make worker-deploy`, the verify run.

## After 0.6.1 is deployed

- The REST and JSON-RPC pages' sponsorship example is from a local chain (chain id 31337), and
  says so. Take it again from Sepolia.
- Those pages' `postpaid_config` examples show Sepolia's `forwarder` as computed for its payee,
  0x2Dc27cb0fbce57bD1122b2CAa61D05d9E6B23838. Check the deployed relay answers the same.

## Open

Of sponsorship, also listed in [its plan](docs/development/plans/sponsorship.md):

- A quote's deadline can't be given as a count of blocks, so a client that doesn't know the
  head gets the furthest deadline, which costs the most.
- A transaction to sign can't pin its own nonce or fees, and a quote takes only authorizations
  signed already.
- A sponsorship's quote has no breakdown of its price.
- No test shows a sponsorship sitting out a block whose fees its payment doesn't cover.
- Sponsorship isn't ready for an OP-stack chain: the account isn't sent its own transactions'
  data fees.
- A quote offers an ETH payment to an account that holds no ETH. Accepted as it is: the
  submission is refused, `sponsored-unfunded`.
- Nothing sends on what the forwarder holds after a broken bundle. Accepted as it is: anyone
  may call it, and the root shows what's there.

Of the rest:

- The client published without `-dev`: its API settled, and its declarations written as the
  reference a model reads.
- EntryPoint v0.9 ([erc4337.md](docs/development/plans/erc4337.md)). Its `handleOps` refuses a
  caller with code, so an op paying by its calls would be sent directly, with the forwarder's
  check after it.
- The treasury leaving one unit of each token with the payee. It can convert the payee's whole
  balance, which clears the balance slot, and the next payment pays to set it again.
- The funding in a dropping bundle ahead of the runs, which would save a block.
- A Safe by one signature ([safe.md](docs/development/plans/safe.md)).
- Money hygiene: the cold wallet, and persistence for the Python relay's queue and treasury
  snapshot.

Known and not traced:

- `make burn-in` fails on both relays, and failed the same way before this branch (eb278dd,
  seed 16136353). Every violation is the relay asking its one node about a block the node
  doesn't have yet. No transaction reverts and no status is wrong.
- The headroom a transfer sent directly is given (a 63rd over its bound) has no local test
  that fails without it. Mainnet's USDC, which anvil has, leaves 267 gas to spare; only
  Sepolia's showed it.

Refactors a review suggested for 0.7, none decided:

- The measurements split into what's measured of a chain, what's reported as wrong with it,
  and what the root shows of the accounts.
- The accounts' state a batch is built from kept apart from what's read for the root alone.
- The Worker's stored forms and its readers of another version's rows moved out of its
  tracked module.
- The Python relay's clock moved out of the relay.
- One type for what's taken, a submission or a sponsorship.

## Settled, not to be asked again

- A sponsorship lands whole or not at all. After a bundle breaks nothing more of it is sent
  (the user, 2026-11-24: "Why keep recovery? It's all or nothing.").
- What a sponsorship may cost is capped where the client chooses it, when it's quoted and
  taken. Bruno's own gas floats with the market each block, as a call's does. The hard
  constraint is that what's paid back is worth more than what it cost, which each block's
  check of the payment against its charge holds to. No absolute figure is stored or enforced
  each block (the user, 2026-11-25).
- What a sponsorship is charged doesn't change with the bytes of what it pays.
- The account's balance isn't Bruno's concern. It's sent what its transactions need, whatever
  it holds, and may pay out of that.
- A sponsorship has methods of its own, and a status has `transactionHashes` and no
  `transactionHash`.
- A payment left in the forwarder by a broken bundle is accepted.
- The forwarder is checked only for whether code is there: its address is its initcode's.
- EntryPoint v0.9 is not in 0.6.1.
- Any change to what proves payment is shown and agreed before it's built.

## Ruled out

- Sending a call with its payment only simulated. Nothing reverted when it didn't pay.
- A persistent contract that wraps any call, as the default in place of the runner.
- Multicall3, or a contract like it, making the transfer and the call in one transaction. It
  has no gas argument, so it can't give a call its stated gas or have its overhead priced on
  top. The bundle already makes two transactions atomic.
- Permit2 payments.
- Recognising accounts or proxies by their code hash.
- Routes specific to Safe.
- Listing a target that keys something on its caller: Permit2's signature transfers and
  allowances; a token's `approve`, `transfer` and `receiveWithAuthorization`; MetaMask's
  DelegationManager; CreateX's guarded salts; UniswapX and 1inch; relayer contracts only
  registered relayers may call.

## Sepolia

- The test wallet's forwarder is deployed at 0x98c78a62529EBD601238718389A07fFb30c264cA. Its
  payee is the float, 0x9e2E7d8F2C05307B2F707988B2A3e720AfCb7bA6.
- The sponsorship that landed on 2026-11-25: block 12203921, built by `builder.nflaig.eth`,
  quoted 25.59 USDC. So Sepolia's nodes answer the forwarder's measurement, and the ePBS
  builder takes a bundle of four holding two that another account signed.
