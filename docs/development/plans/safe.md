---
description: "Plan: a user's own 1-of-1 Safe, by one EIP-712 signature: the user signs a batch, Bruno deploys the Safe if it isn't yet, runs the batch and is paid, all in one transaction"
---

# Plan: a Safe by one signature

The user signs one thing, a batch of calls, as EIP-712 typed data, which every wallet signs.
Bruno deploys the user's Safe if it isn't deployed yet, runs the batch from it, and is paid from
it, in one transaction. Recorded 2026-11-03, from the [agent wallets](agent-wallets.md) research:
the wallets agents use sign typed data for anyone, but almost none signs an EIP-7702
authorization or hands back a transaction to send, so a Safe of the user's own is how any of them
acts as an account through Bruno.

## The account

A 1-of-1 Safe owned by the user's EOA, through Safe's own proxy factory and contracts, used as
they are (vendored onto anvil for the tests, never forked):

- **The same address everywhere.** It's a function of the factory, the singleton, the setup (one
  owner, threshold 1, Safe's standard fallback handler) and a salt. With that setup fixed and
  documented, it's the same on every chain Safe's contracts are on, and anyone can recompute it.
- **Funded before it exists.** An exchange's withdrawal, or an EIP-3009 transfer sent through
  Bruno, reaches it before it's deployed.
- **Deployed by its first transaction.** The submission's deployment is `createProxyWithNonce`,
  its call the Safe's `execTransaction`.
- **Persistent, Bruno one way in.** Once it's deployed, the user connects their EOA in Safe's web
  app and finds it there, usable from any Safe-compatible app or relayer, or with their own gas.

## The batch

The user's wallet signs a SafeTx (EIP-712): a MultiSend of the calls and a transfer of the
quoted amount to Bruno's payee, which the runner measures as it measures any payment. The calls'
`msg.sender` is the user's Safe.

- **Safes that exist already** work the same way, n-of-m included, once enough owners have
  signed.
- **Spending limits, onchain.** A person owns the Safe; an agent is a delegate in Safe's
  AllowanceModule, with a per-token allowance that can reset daily, and signs only its allowance
  transfers.
- **Without the runner.** Safe's own rules make an unpaid SafeTx revert, so it can be sent as a
  [trusted target](erc4337.md#trusted-targets-sending-without-the-runner-proposed-2026-11-03-to-ponder),
  saving the runner's overhead (32k gas a transaction on Prague, 196k on Amsterdam).

## Interface

On REST and JSON-RPC, and in the client: the Safe's address and the typed data to sign (its
deployment included if it needs one), then a submission with the signature. The client's
declarations name both, for a model writing code against them as much as for a developer.

## To verify before building

- Whether Safe's web app lists a Safe only once it's deployed, and whether its own address
  prediction gives the same address for the chosen setup and salt.
- A MultiSend with the payment leg in one `execTransaction`, measured by the runner, on anvil
  with Safe's contracts vendored.
- A smart wallet as the owner (Coinbase's): its ERC-1271 signature in `execTransaction`.
- Safe's contracts on each chain Bruno serves, checked by `make preflight`.
