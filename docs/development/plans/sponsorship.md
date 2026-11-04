---
description: "Plan: atomic sponsorship, a transaction the user's own wallet signed but can't pay gas for, sent in one atomic bundle with Bruno's funding of its account and a check that Bruno was paid"
---

# Plan: atomic sponsorship

A user's wallet signs a transaction it can't pay the gas for: an EOA holding a token and no
ETH. Bruno sends it in one atomic bundle: a transaction funding the account with the ETH it
needs, the user's own transaction, and a check that Bruno was paid. The bundle lands whole or
not at all, so Bruno's ETH reaches the account only in the block where the user pays for it.
Recorded 2026-11-03, from the [agent wallets](agent-wallets.md) research: it's the integration
for every wallet that signs a transaction without sending it (Privy, Turnkey, Dynamic,
Openfort, an agent's own key).

## Why

- **`msg.sender` is the user's.** The transaction is the user's own, signed by their own
  wallet: any call, to any contract, as the account itself. No delegation, no smart account,
  no runner standing in for the sender.
- **Nothing to set up.** The account needs no code and no prior transaction; a type-4
  transaction delegating it by EIP-7702 is sponsored the same way.
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
| Repaid in | ETH, to the builder | any token Bruno prices, to its payee |
| Payment enforced by | the builder's own simulation | the bundle itself, onchain |
| For | searchers' bundles | any user's transaction |

## The bundle

Three transactions, sent as an atomic bundle (no `revertingTxHashes`, nothing droppable: the
treasury already sends its runs so, `atomic_params`):

1. **Funding.** From a concurrency account: the user's account gets
   `gasLimit × maxFeePerGas + value` of its transaction, what the EVM checks the sender holds
   before it runs one.
2. **The user's transaction,** as signed. Its calls include the payment to Bruno.
3. **The check,** which reverts unless Bruno was paid what the quote asked, so the whole bundle
   fails.

The check can't compare the payee's balance with what Bruno read before sending: another
payment to the payee in the same block would pass it. Two ways to make the payment this
transaction's alone, to settle:

- **A payment address for each submission (preferred).** The quote names a fresh address: the
  CREATE2 address, through Bruno's deployer, of initcode that sends whatever it holds of the
  token to the payee, and reverts if it's less than the amount. The user's transaction pays it;
  the check deploys it, which forwards the payment and self-destructs, as the runner does.
  The address held nothing before, so all it holds is this payment. No new contract to deploy
  and keep, and the check is the runner's own model: initcode through the deployer.
- **A sponsor contract.** The funding goes through a small persistent contract that records the
  payee's balance in storage; the check reads it, compares, and clears the slot. A contract to
  write, audit, deploy on each chain and check in `make preflight`.

## What to price and design

- **Fees are fixed when the user signs.** A user who signs once (an approval in their wallet)
  can't sign again each block, so the transaction's `maxFeePerGas` must cover the base fee
  rising until its deadline: `lasting_fees`, as a quote's. The deadline is short, so the cap
  stays close.
- **What the funding leaves behind.** The account is funded at the fee cap; what the
  transaction doesn't spend (its gas limit unused, and the cap above the block's price) stays
  in the user's account. The quote prices the funding whole: Bruno's cost is all of it.
- **One at a time per account.** The transaction's nonce is fixed when it's signed; a second
  from the same account waits until the first lands or expires.
- **Where it works:** chains whose builders take atomic bundles (Ethereum, Sepolia). Elsewhere
  the funding could land without the transaction that pays for it.
- **The submission.** A new kind of submission on REST, JSON-RPC and MCP: a signed transaction
  and its payment (token, amount, the payment address from the quote), beside the existing
  call and its payment. Quoting it: the transaction's gas as signed, the funding, the two
  transactions of Bruno's own, at the chain's price, plus the margin.
- **The account's own ETH.** An account that can pay its own gas needs none of this; one with
  some ETH, short of the cap, is funded only the difference.

## Open questions

1. The payment address or the sponsor contract (above).
2. Whether a token's transfer to a fresh address costs more (a new balance slot: EIP-8037's
   state cost on Amsterdam), and the check's own gas, priced exactly from the gas schedule.
3. Whether every builder Bruno sends to honours atomicity as rbuilder does, tested through the
   mock builder first.
4. Type-4 transactions: an account delegating itself in its own sponsored transaction signs
   its authorization at its nonce plus one, the transaction's own nonce being used first.
