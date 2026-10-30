---
description: "Plan: AgentKit's own actions on Ethereum without ETH, through a wallet provider wrapping the agent's own and sending through Bruno; batch payments; what upstream accepts, and the design and UX questions the wallet raises"
---

# Plan: AgentKit's actions without ETH

`@mailam/bruno-agentkit` gives an agent three actions of its own: `send_usdc_gasless`,
`send_call_gasless` and `get_gasless_price`. AgentKit's other actions (ERC-20 transfers, swaps,
bridges, ERC-8004 registration) still send from the agent's wallet and need ETH. This plan is
about making those actions work for an agent that holds only USDC, without a pull request to
AgentKit, and about what our own actions should add, batch payments first.

## Upstream

As seen on 2026-10-29, `coinbase/agentkit` is effectively not merging outside contributions:

- **The queue.** There are 329 open PRs. Of the 254 not from dependabot, about 158 add a
  provider, and 81 of those sell an x402 service on Base. The oldest is from February 2025.
- **What's merged.** In the last four months, five PRs were merged, all maintainer fixes
  (security, docs, analytics). The last third-party provider merged was in March 2026, which is
  also when the last release came out. No maintainer has commented on a PR or issue since April.
- **No policy.** `CONTRIBUTING.md` still promises a review within a day, and nothing describes
  publishing a provider as a package of its own. The README's "Creating an Action Provider" is
  the route in practice: a class in the user's code, passed to `AgentKit.from`.

The open PRs fall into a few genres:

- checks an agent runs before it pays or signs (about 48);
- paid data APIs (about 34);
- DeFi on Base (about 25);
- payments and marketplaces (about 25);
- identity (about 8);
- seven wallet providers, none reviewed.

Two gaps matter for Bruno:

- **Ethereum is nearly empty.** Only four open PRs send transactions on Ethereum mainnet or
  Sepolia. Everything else targets Base or Solana.
- **Nothing pays gas for arbitrary calls.** Nobody offers a relay of this kind, an ERC-4337
  bundler paid over x402, or a token paymaster.
  - The closest, BasePay (#1333), relays EIP-3009 USDC transfers on Base. That covers what
    `send_usdc_gasless` does and nothing more.
  - KeeperHub (#1504) routes transactions through a hosted API, but for reliability, not gas.

So a PR would go unreviewed. The package stays on JSR, and discovery has to come from the docs
and llms.txt.

## Reaching AgentKit's actions: wrapping the wallet

AgentKit has no way for a provider to wrap or intercept another provider's actions.

- **No interception.** `AgentKit.from` joins each provider's `getActions()` together.
- **No shadowing.** Action names are prefixed with the provider's class name, so one provider
  can't replace another's action.
- **Every transaction goes through the wallet provider.** Each action that sends one calls
  `walletProvider.sendTransaction` or `nativeTransfer`, or calls `toEip1193Provider()`, whose
  `eth_sendTransaction` calls `sendTransaction` and then `waitForTransactionReceipt`.

So a wallet provider that wraps the agent's own makes AgentKit's existing actions work without
ETH, and none of them has to know about Bruno. It hands signing, the address and balances to the
wallet provider it wraps, and sends each transaction through Bruno. On Ethereum mainnet or Sepolia
these actions are:

- `erc20` and `erc721`;
- `weth`;
- `across` bridging;
- `zeroX`, `sushi` and `enso` swaps;
- `vaultsfyi` and `yelay` vaults;
- `erc8004`.

AgentKit's x402 action provider is not a way in. Its networks are Base, Base Sepolia and Solana
only, and its token table has no Sepolia USDC.

The existing smart wallet providers can't simply be pointed at Bruno:

- **`ZeroDevWalletProvider`** hard-codes ZeroDev's bundler URL and sends ZeroDev intents.
- **`CdpSmartWalletProvider`** sends through CDP's API. Only its paymaster URL can be set.
- **`ViemWalletProvider`** estimates gas and fees for an ordinary transaction before it sends,
  so a smart account client doesn't fit behind it.

A Bruno wallet is therefore a subclass of `EvmWalletProvider` of its own. AgentKit has no pattern
for one provider delegating to another. The nearest is ZeroDev taking another provider's
`toSigner()` as its signer.

**Recording is ruled out.** A wallet whose `sendTransaction` records the call instead of sending
it could, in principle, turn AgentKit's actions into lists of calls to price, check or batch
before anything is sent. But actions are written step by step: they send, wait for the receipt,
then read the chain to build the next transaction (an approval, then the allowance, then the
swap). Recorded rather than sent, the second step is built on a first that never happened. The
wrapping wallet sends each transaction for real, so these actions work as written.

## The wallet: who sends, who pays

**When Bruno is used.** Whether to send through Bruno turns on whether the account pays its gas in
ETH, not on what kind of account it is. A key holding ETH sends its own transactions. A key or a
smart account holding only USDC goes through Bruno. That's best set by the user when the wallet is
made ("pay gas in USDC"), not decided from a balance, which can pass and still leave too little
for the next transaction.

**What kind of account.** The wrapped wallet provider says: `ViemWalletProvider` and
`CdpEvmWalletProvider` hold keys, `CdpSmartWalletProvider` and `ZeroDevWalletProvider` hold smart
accounts. The chain can't tell. An address with no code may be a key or a smart wallet not yet
deployed, and a contract's code says which wallet it is only once it's been measured into the
wallet gas schedule.

**Who sends.** `send_call_gasless` runs the call from Bruno's contract, so it can't stand behind
`sendTransaction`: the actions above need the call to come from the agent's own address. That
account could be either of these:

- **The agent's key, upgraded by EIP-7702**, sending through Bruno's runner, or through its
  bundler as an EIP-7702 account. Its address, balances and history stay the same. It
  needs an authorization signed once. `EvmWalletProvider` has no `signAuthorization`, but an
  authorization is a signature over a hash, and `sign(hash)` signs raw hashes in
  `ViemWalletProvider` and `CdpEvmWalletProvider`. Privy's delegated provider signs with
  `personal_sign`, which adds a prefix, so it can't sign one.
- **A smart account (Kernel, Coinbase Smart Wallet)**, sending UserOps through Bruno's bundler.
  A new one has a new address, so funds have to move to it, unless the agent already uses
  `ZeroDevWalletProvider` or `CdpSmartWalletProvider`, and this wallet takes their account over.

**Who pays.** For a UserOp, with Bruno staying a bundler and never a paymaster, the gas is paid in
USDC by either:

- **A token paymaster** (Circle's or Pimlico's) pays the gas and takes USDC from the account.
- **The op pays Bruno itself**, the call-paid design still open in [ERC-4337](erc4337.md).

The rest of this wallet's design depends on that choice.

## UX questions

These are what makes the wallet seamless or not. All are open.

- **Where it lives.** Not in `@mailam/bruno-agentkit`, which is action providers. The candidates:
  - a package of its own;
  - a viem-level core in `@mailam/bruno` (an account sending through Bruno), with a thin AgentKit
    adapter, so GOAT, ElizaOS and the Vercel AI SDK can take the same core later.
- **What the user passes in.**
  - An existing wallet provider to wrap, keeping its signing: viem, CDP's EVM wallet, ZeroDev or
    CDP's smart wallet.
  - Or a key.
  - For EIP-7702, which delegate the account takes: Simple7702, Kernel's or MetaMask's.
- **First use.**
  - The authorization, and any approval to a paymaster, happen on the first transaction or at
    setup. The agent shouldn't have to know they exist.
  - Circle's paymaster takes an EIP-2612 permit in `paymasterAndData`, so it needs no separate
    approval.
- **The fee cap.** The actions carry no fee parameter, so the cap on what one transaction may
  pay is set when the wallet is made. Open: how an agent sees what it paid, and what a refusal
  looks like.
- **The ETH balance.** `getBalance` and `get_wallet_details` report ETH. An agent that sees
  0 ETH may decide it can't send anything. The wallet's name, or its details, should say that gas
  is paid in USDC.
- **Hashes.** `sendTransaction` returns a hash, and actions print it, often as an explorer link.
  - A UserOp's hash is not a transaction hash. CDP's smart wallet returns the UserOp hash and
    resolves it in `waitForTransactionReceipt`.
  - Returning the transaction hash instead means waiting for inclusion inside `sendTransaction`.
- **Errors.** A call that would revert is refused before it's sent and costs nothing. That
  refusal surfaces as `sendTransaction` throwing, and its message is what the agent reads.
- **Batching.** An action that approves and then swaps calls `sendTransaction` twice, so it pays
  twice. A UserOp could carry both calls, but nothing in AgentKit's interface groups them.
- **Overlap.** Once the wallet exists, `send_usdc_gasless` and AgentKit's own ERC-20 transfer
  through this wallet do the same thing. The docs should say which to use when.
- **Telemetry.** AgentKit's wallet providers report themselves to Coinbase when they're made. A
  wrapped provider still does, and ours needn't.

## Batch payments

Paying several recipients at once needs no change to the relay. A submission carries one call,
and anyone may submit a signed `transferWithAuthorization`, so that call can be Multicall3's
`aggregate3` (`0xcA11bde05977b3631167028862bE2a173976CA11` on mainnet and Sepolia) making each of
the agent's signed transfers in turn:

- one transaction and one fee for all of them, its fixed costs paid once;
- EIP-3009's random nonces, so the transfers don't clash;
- `allowFailure` false throughout, so one bad transfer reverts the call, and Bruno refuses it
  before it's sent, at no cost;
- an undeployed smart wallet deployed once, ahead of the batch, as for one transfer now.

In AgentKit, `send_usdc_gasless` takes a list of recipients and amounts rather than gaining a
batch action beside it: one item is what it does now, and one action is easier for a model to
choose than two. The package is on `-dev`, so its API can still change.

## Building calls apart from sending them

AgentKit's actions build their calls and send them in one step, so how a transaction is sent is
fixed inside each action. Building calls in one place and sending them in another is how the MCP
tools are planned ([MCP](MCP.md)): builder MCPs give calldata, the agent's wallet MCP signs, and
Bruno sends. That isn't this plan's direction for AgentKit.

## The docs

- A page on AgentKit's own actions on Ethereum with only USDC: an ERC-20 transfer, a swap
  through `zeroX`, and ERC-8004 registration. Each comes with its tested script, for llms.txt to
  route to.
- ERC-8004 registration on Sepolia, by an agent holding only USDC, as the example to lead with.

## Tasks

- [ ] Batch payments: `send_usdc_gasless` taking a list, through Multicall3, vendored onto
      anvil and tested for keys and for smart wallets deployed and not.
- [ ] Settle how the gas is paid: a token paymaster, or the call-paid op.
- [ ] Settle where the wallet lives, and what the user passes in.
- [ ] Walk through the UX questions above.
- [ ] The wallet, EIP-7702 on a key first, tested on anvil through AgentKit's `erc20`, `zeroX`
      and `erc8004` actions.
- [ ] The docs page and its scripts.
