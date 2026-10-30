---
description: "Plan: the smart wallets in wide use, what each needs from Bruno, and the analysis each one takes before it's priced exactly"
---

# Plan: smart wallets

0.5.0 lets a smart wallet pay with a signed EIP-3009 transfer, and lets its first payment deploy
it (ERC-6492; [plan](erc6492.md)). The protocol works for any wallet. What differs from wallet to
wallet is the analysis behind pricing it exactly and testing it, and each one has to be done on
its own. This plan records that analysis as it's done, one section per wallet.

So far, the wallet gas schedule (`wallets.py`, `wallets.ts`) has Coinbase Smart Wallet v1 and v1.1,
each with a key as its owner.

## Adoption

Measuring a wallet makes its price exact; it doesn't decide whether anyone uses it with Bruno.
That depends on where the wallets are and who chooses how their gas is paid. As seen on
2026-10-28:

- **What Bruno offers that the alternatives don't:** no API key, account or deposit; any token
  with EIP-3009, not only USDC; a call that would revert is never included, so it costs nothing;
  prices from on-chain pools. The first and third matter most where no person is watching.
- **The chain matters more than the wallet.** Smart wallets are mostly on Base: Coinbase's,
  AgentKit's CDP and ZeroDev accounts, and most apps on Privy. Bruno is on Ethereum and
  Sepolia, where most USDC is held by keys, which EIP-7702 already serves. So the largest step
  for smart wallets is Base, through `upfront` x402 ([plan](x402-async.md)) and OP-stack
  pricing ([plan](morechains.md)), not more wallets measured.

Where adoption can come from, in order:

1. **Agents.** An agent holds USDC, has no ETH, and has no one to manage a paymaster's API key.
   AgentKit works for the CDP smart wallet and viem accounts, and ZeroDev is next (above). MCP
   ([plan](MCP.md)) and tools for GOAT, ElizaOS and the Vercel AI SDK widen the same channel;
   x402 is how agents already pay.
2. **viem's smart accounts.** Kernel, Coinbase Smart Wallet, Light Account, MetaMask's kit and
   Safe (through permissionless.js) are built on viem's `toSmartAccount`, and our client takes
   any of them through ERC-6492. What's missing is a recipe per SDK in the docs, and being
   found where those SDKs' users look. An unmeasured wallet still works; it may be charged too
   little.
3. **Embedded-wallet platforms** (Privy, Dynamic, Turnkey, Circle's wallets). The app chooses
   how gas is paid, so Bruno is offered to app developers. The wallets measured here cover
   Privy's choices. Circle's wallets are the most USDC-minded, and Circle sells its own
   paymaster.
4. **Wallets, through EIP-5792.** The one way into consumer wallets without each integrating
   Bruno, but it means being an ERC-7677 paymaster service, which pays even when a call
   reverts: option 2 in [the ERC-4337 plan](erc4337.md), not taken. Wallets reach Bruno as a
   bundler instead.
5. **Safe treasuries.** Much value, but they usually hold ETH as well, so less need.

## What each wallet needs

For each wallet, these questions, answered from its source and measured on anvil against its
contracts fetched from Ethereum (as `test_wallets.py` does for Coinbase's):

- **Factories:** the factory addresses in use, by version, and whether they're the same on each
  chain.
- **Who creates it:** which SDKs and products make this wallet, and which version they default
  to.
- **Signature:** how it signs a typed-data hash (any replay-safe wrapping of the hash, the
  signature's encoding), and its length.
- **Verify gas:** what its `isValidSignature` adds to a transfer over a key's, per fork, for
  each kind of owner it takes: a key, a passkey, several owners.
- **ERC-6492:** whether its SDK wraps signatures while the wallet is undeployed, and whether a
  deployed wallet can need a "prepare" call.
- **Deployment gas:** what its factory call costs. The relay estimates this per request, so it
  isn't in the schedule, but it belongs in the analysis.

## Wallets deployed by a factory

These are ERC-4337 accounts: the address exists before the code, and ERC-6492 deploys it. Counts
are BundleBear's all-time account deployments per provider, from a search on 2026-10-28; its
page didn't load to confirm them. They don't cover Safe's deployments made outside ERC-4337.
Source: [BundleBear, ERC-4337 factories](https://www.bundlebear.com/erc4337-factories/all).

| Wallet | Accounts deployed | Status |
|---|---|---|
| Alchemy Light Account, Modular Account v2 | ~13.6M (Alchemy) | To analyse |
| ZeroDev Kernel | ~4.65M | v3.2 measured, as AgentKit makes it; other versions to do |
| SimpleAccount (the ERC-4337 reference) | ~4.28M | To analyse |
| Biconomy (Nexus; its older account deprecated) | ~2.11M | To analyse |
| Coinbase Smart Wallet | ~670K | Key owners measured; passkeys to do |
| Safe | Largest by value held | To analyse |
| thirdweb | Unknown | To analyse |
| MetaMask Smart Accounts (Hybrid, Multisig) | Unknown | To analyse |

### Coinbase Smart Wallet

Measured: v1 and v1.1 with a key owner (the table in the [ERC-6492 plan](erc6492.md)). To do:
a passkey owner, which Coinbase's consumer wallet uses by default.

Source: [coinbase/smart-wallet](https://github.com/coinbase/smart-wallet).

### Alchemy Light Account and Modular Account v2

The most deployed. Light Account has one owner, a key; Modular Account v2 takes passkeys
(WebAuthn) too. To find: the factories by version, and which one Alchemy's SDK defaults to.

Sources: [alchemyplatform/light-account](https://github.com/alchemyplatform/light-account),
[alchemyplatform/modular-account](https://github.com/alchemyplatform/modular-account).

### ZeroDev Kernel

Built on viem's `toSmartAccount`, so it wraps while undeployed (the [ERC-6492 plan](erc6492.md)
checked this). It's an ERC-7579 modular account: the signature check goes through a validator
module (ECDSA, WebAuthn and others), and that module, not the account, sets the cost. To find:
the default validator and the versions in use.

**First, because AgentKit uses it.** AgentKit's `ZeroDevWalletProvider` (checked 2026-10-28)
makes Kernel v3.2 with ZeroDev's ECDSA validator as its `sudo` plugin, a key from another
provider as the owner, on EntryPoint v0.7 by default. It installs ZeroDev's intent executor
(`INTENT_V0_3`) in the account's `initConfig`, so its deployment does more than a bare
Kernel's. It signs typed data locally through the Kernel account, so our client's ERC-6492
path applies, and sends its own transactions as intents through ZeroDev's bundler.

**Measured (2026-10-28).** Its check adds 16,636 gas on Prague and 17,436 on Amsterdam; its
signature is 86 bytes, the validator's identifier (0x01 and its address) and the owner's 65.
Its accounts name ZeroDev's meta-factory, `0xd703…42d5`, which every Kernel v3 deploys through,
so the schedule finds v3.2 by the factory the meta-factory's `deployWithFactory` names. Another
version through the same meta-factory is priced as unknown. The provider itself can't run on a
local chain (it takes its chain from AgentKit's table of public networks), so the npm tests
build the account with `@zerodev/sdk` and AgentKit's options, through AgentKit's viem provider.
Still to measure: v3.1 and v3.3, if they're in use.

Source: [AgentKit's `zeroDevWalletProvider.ts`](https://github.com/coinbase/agentkit/blob/main/typescript/agentkit/src/wallet-providers/zeroDevWalletProvider.ts).

Sources: [zerodevapp/kernel](https://github.com/zerodevapp/kernel),
[ERC-7579](https://eips.ethereum.org/EIPS/eip-7579).

### SimpleAccount

The ERC-4337 reference account (eth-infinitism), with one key owner; many apps start from it. To
find: whether it implements ERC-1271 in each version deployed, since a transfer from it needs
`isValidSignature`.

Source: [eth-infinitism/account-abstraction](https://github.com/eth-infinitism/account-abstraction).

### Biconomy Nexus

An ERC-7579 account like Kernel, so the cost depends on the validator module. To find: the
default validator, and whether the deprecated account still makes new wallets.

Sources: [bcnmy/nexus](https://github.com/bcnmy/nexus),
[Pimlico's comparison of account types](https://docs.pimlico.io/guides/how-to/accounts/comparison)
(which marks Biconomy's older account deprecated).

### Safe

The largest by value held: multisigs, DAOs and treasuries, with ERC-4337 added by a module. Its
signature check goes through its fallback handler and checks each owner's signature up to the
threshold, so its cost grows with the threshold. To find: the proxy factory and singleton
versions in use, and the cost for one owner and per further owner. Passkey owners are a separate
module.

Sources: [safe-global/safe-smart-account](https://github.com/safe-global/safe-smart-account),
[safe-global/safe-modules](https://github.com/safe-global/safe-modules) (the ERC-4337 and passkey
modules).

### thirdweb

Its own account, offered through Privy among others. To find: the factory, the signing format,
and how much it's used.

Source: [thirdweb-dev/contracts](https://github.com/thirdweb-dev/contracts).

### MetaMask Smart Accounts

From MetaMask's Smart Accounts Kit (formerly the Delegation Toolkit), which apps build in:
Hybrid (a key owner plus any number of passkeys) and Multisig (keys with a threshold). The
MetaMask extension's own upgrade is the EIP-7702 one, below. To find: the factories, and whether
the kit wraps signatures with ERC-6492 while undeployed.

Sources: [MetaMask Smart Accounts](https://docs.metamask.io/smart-accounts-kit/concepts/smart-accounts),
[MetaMask/delegation-framework](https://github.com/MetaMask/delegation-framework).

### Privy

Not a wallet design of its own. An app using Privy's smart wallets picks one of Kernel, Safe,
Light Account, Biconomy, thirdweb or Coinbase Smart Wallet, so analysing those covers it. Privy's
embedded wallets are keys, which can also be upgraded with EIP-7702.

Source: [Privy, smart wallets](https://docs.privy.io/guide/react/wallets/smart-wallets/).

## Keys upgraded with EIP-7702

These have nothing to deploy, so they're never wrapped. Delegates in use include MetaMask's
Stateless7702 (the extension's upgrade), Uniswap's Calibur, Ambire, OKX, Trust Wallet and Porto.
How widely each is used is unknown: no reliable source was found.

Sources: [MetaMask/delegation-framework](https://github.com/MetaMask/delegation-framework)
(Stateless7702), [Uniswap/calibur](https://github.com/Uniswap/calibur),
[AmbireTech/ambire-common](https://github.com/AmbireTech/ambire-common),
[ithacaxyz/porto](https://github.com/ithacaxyz/porto),
[Alchemy on EIP-7702 and wallets](https://www.alchemy.com/overviews/eip-7702-metamask-and-wallets).

USDC sends any signer that has code to ERC-1271, even through the `(v, r, s)` overload, which
packs the signature and calls `SignatureChecker.isValidSignatureNow`; that checks for code first
([`util/SignatureChecker.sol`](https://github.com/circlefin/stablecoin-evm/blob/master/contracts/util/SignatureChecker.sol),
[`v2/EIP3009.sol`](https://github.com/circlefin/stablecoin-evm/blob/master/contracts/v2/EIP3009.sol)). An upgraded key has
code, so its delegate's `isValidSignature` decides whether its transfer is valid. The relay
already reads the payer's code when planning, and estimates a payer with code as a contract.

To find, for each delegate: whether its `isValidSignature` accepts the key's plain signature
over the hash (if it doesn't, the key can't pay by transfer at all while delegated), and its cost
per fork.

## Passkeys

Coinbase Smart Wallet, MetaMask Hybrid, Kernel, Biconomy Nexus, Alchemy's Modular Account v2,
Safe (with a module) and Porto all take passkey (WebAuthn) owners. A P-256 check is far more
expensive than `ecrecover`: in Solidity, or through the [RIP-7212](https://github.com/ethereum/RIPs/blob/master/RIPS/rip-7212.md) precompile where a chain has it
(not Ethereum before frame transactions), or a ZK verifier (around 100,000 gas). To find, for
each wallet: which verifier it uses on each chain, and the cost measured.

## The price of a wallet Bruno doesn't know

A wallet the schedule doesn't know is priced at the largest figures in it: Kernel v3.2's gas
and Coinbase's signature length, each with a key owner. Safe with several owners, and any passkey owner, cost more, so until they're
measured these are underpriced unless the client passes `verifyGas`. Each wallet measured
narrows the gap. Whether the unknown case should price higher in the meantime is open.

## ERC-4337: UserOps, bundlers and paymasters

These wallets usually act through UserOps, which Bruno doesn't take: the wallet signs only an
EIP-3009 transfer. How that ecosystem works, how Bruno differs, and whether Bruno should meet it
anywhere are in [their own plan](erc4337.md).

## Order

1. Kernel v3.2 as AgentKit's ZeroDev provider makes it: done, 2026-10-28.
2. Light Account and Safe with one owner.
3. Coinbase Smart Wallet with a passkey owner, then the other passkey wallets.
4. A key upgraded with EIP-7702 paying by transfer: MetaMask's delegate and Calibur first.
5. Biconomy Nexus, SimpleAccount, thirdweb and MetaMask Smart Accounts.

## Standards

[ERC-4337](https://eips.ethereum.org/EIPS/eip-4337),
[ERC-6492](https://eips.ethereum.org/EIPS/eip-6492),
[ERC-1271](https://eips.ethereum.org/EIPS/eip-1271),
[EIP-7702](https://eips.ethereum.org/EIPS/eip-7702),
[ERC-7579](https://eips.ethereum.org/EIPS/eip-7579),
[RIP-7212](https://github.com/ethereum/RIPs/blob/master/RIPS/rip-7212.md).
