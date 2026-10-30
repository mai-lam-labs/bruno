---
description: "Plan, not started: delegated permissions (ERC-7710 delegations, ERC-7715 Advanced Permissions), a user's account letting an agent spend within limits; where Bruno pays the gas of redeeming them, and what to research before building"
---

# Plan: delegated permissions (ERC-7710, ERC-7715)

On the table, not started (2026-10-30): pursued when the strategy calls for it, and once an
integration that uses it comes along. The research so far, so it's ready then.

## The standards

Both are Draft ERCs.

- **[ERC-7710](https://eips.ethereum.org/EIPS/eip-7710), smart contract delegation:** an
  account (the delegator) signs, off-chain, a delegation giving another (the delegate) the right
  to act for it, narrowed by caveats: contracts enforcing terms on chain, such as a USDC
  allowance a day, the addresses it may pay, an expiry. The delegate redeems it by calling
  `redeemDelegations` on a delegation manager, which checks the chain of signatures and every
  caveat, then has the delegator's account make the calls. It requires ERC-1271 and ERC-7579.
  Authors from MetaMask and ZeroDev.
- **[ERC-7715](https://eips.ethereum.org/EIPS/eip-7715), requesting permissions from wallets:**
  the JSON-RPC an app or agent uses to ask a wallet for such a permission
  (`wallet_requestExecutionPermissions`, and to list and revoke them). The user approves it in
  the wallet, which answers with the delegation's context and its manager's address; the
  requester then redeems it by ERC-7710. It requires ERC-4337 and ERC-7710. Authors from
  MetaMask, Coinbase, WalletConnect, ZeroDev and Biconomy.

## MetaMask's implementation

- **The contracts:** the [delegation framework](https://github.com/MetaMask/delegation-framework).
  Its DelegationManager, `0xdb9B1e94B5b69Df7e401DDbedE43491141047dB3` on Ethereum and Sepolia
  (read from the Stateless7702 delegate's `delegationManager()`, 2026-10-30), and caveat
  enforcers, one contract a kind of term. MetaMask's smart accounts (Stateless7702, Hybrid,
  MultiSig) are delegators.
- **The kit** (`@metamask/smart-accounts-kit`, which Bruno already tests for Stateless7702):
  - `erc7710BundlerActions`, `sendUserOperationWithDelegation`: a delegate that's a smart
    account redeems in a user op, through any bundler;
  - `erc7710WalletActions`, `sendTransactionWithDelegation`: a delegate that's an EOA redeems in
    a transaction of its own, paying its gas in ETH;
  - `erc7715ProviderActions`, `requestExecutionPermissions`: asks the user's MetaMask for a
    permission ("Advanced Permissions").
- **In use:** [Revoke.cash's auto-revoking](https://revoke.cash/blog/2026/how-auto-revoking-works-under-the-hood)
  works this way with MetaMask, on Ethereum, Base, Unichain and others; TokenPocket documents
  [ERC-7715 for agents](https://help.tokenpocket.pro/developer-en/agentic-wallet/eip-7715-account-with-permissions).
  The MetaMask extension's own smart account upgrade delegates an EOA to the same
  Stateless7702 ([GoPlus](https://blog.gopluslabs.io/2025/06/03/financing/2025-06-03-Understanding-EIP-7702-Phishing-Attacks-A-Comprehensive-Guide-to-Protection-Strategies-for-Wallets);
  unverified), so its users could be delegators.

## Where Bruno fits

The delegate redeeming a permission sends a transaction, and pays its gas. An agent given a
user's USDC to spend within limits holds no ETH: that's Bruno's case.

1. **The delegate a smart account, redeeming in a user op through Bruno as its bundler:** the op
   calls `redeemDelegations`, its fees zero, and pays the relay by a call, quoted before it's
   signed (`postpaid_quoteUserOperation`). The payment can come from the user's USDC under the
   same delegation, if its caveats allow paying the relay's payee, or from the delegate's own.
   Nothing new in the relay, if it works as it reads.
2. **An open delegation, redeemed through the runner:** the framework allows a delegation any
   account may redeem (to verify: its "any delegate" form). Then the relay's own call could
   redeem it, the payment one of the calls it makes, with nothing held by the agent at all.
   Bounded by the caveats, since anyone holding the delegation could redeem it.
3. **AgentKit:** an agent acting on a user's delegated USDC needs no wallet of its own to fund,
   which bears on the wrapping wallet in [AgentKit's actions](agentkit.md).

## Before building

- Read ERC-7710 and ERC-7715 as written, and the framework's DelegationManager and the caveat
  enforcers a USDC allowance uses: what a redemption costs, and whether it can be priced as the
  runner prices a call.
- Vendor the DelegationManager and those enforcers onto anvil; redeem a delegation from a
  Stateless7702 account, in a user op through both relays (route 1).
- Settle whether a caveat can limit paying the relay's payee to what the quote asks, so a
  delegation doesn't let the relay take more.
- Check route 2's open delegations, and whether the runner can redeem one.
- Which wallets answer `wallet_requestExecutionPermissions` beyond MetaMask's (TokenPocket's),
  and which agent frameworks request permissions.
