# Bundlers behind API keys: outline and first draft

Audience: developers building apps on Ethereum, and the account-abstraction community. Name the
ERCs, don't restate them. The content is what the ecosystem looks like today, what it costs the
people building on it, and a bundler that works another way. Every figure and claim about
another project needs a public source before publishing; those still unchecked are marked
[check].

## Working title

"ERC-4337's API key problem"

Alternatives: "The permissionless bundler that wasn't"; "Who pays for the first transaction?"

## Hook

A team building an app for people who have never used a blockchain reported paying $500 a
month for an embedded-wallet and gas plan, out of a total spend of $3,500, before they had much
revenue [source: the public post]. Their users' first transaction needs gas they don't have, and
the only way the ecosystem offers to provide it is a subscription.

## Outline

1. **What ERC-4337 promised.** Account abstraction without a protocol change: anyone can run a
   bundler, and user operations travel through a shared mempool that any bundler can draw from,
   under ERC-7562's validation rules. No single operator decides what gets included.
2. **What happened.**
   - A bundler is, in practice, a hosted API behind a key. The SDKs ship with their own
     vendor's bundler as the default.
   - Most operations go through a handful of operators [check: BundleBear's share by bundler,
     with the date, https://www.bundlebear.com/overview/all].
   - The shared mempool is live on Ethereum, Arbitrum and Optimism, with Etherspot, Candide,
     Silius and Alchemy taking part [check: what share of operations it carries].
   - A public bundler with no key exists: Etherspot's free bundler
     (https://github.com/etherspot/free-bundler), which erc4337.io's quick-start features
     (https://docs.erc4337.io/userops/quick-start) [check: its limits and terms]. Others that started open now need a
     key: Candide's mainnet endpoint, Etherspot's own main one.
   - Biconomy shut its bundler down on 2026-02-28 and recommended no other
     (https://blog.biconomy.io/upgrade-path-from-biconomy-v2-to-nexus-post-feb-28-26): an account tied to
     one vendor's bundler has to move when the vendor does.
3. **Why it went that way: the paymaster.** Most users have no ETH. Someone has to front the
   gas and carry the risk that it isn't repaid, so the paymaster became the product. A paymaster
   needs an account, a deposit, a key, spending limits and a bill. The bundler comes bundled
   with it.
4. **What it costs.** The developer is the customer, not the user. Early teams pay fixed
   monthly fees and sponsorship markups to let users take their first step. Pimlico charges gas
   plus 10% for sponsorship on mainnet, with a card on file for pay-as-you-go
   (https://pimlico.io/pricing) [check: two or
   three developers' own complaints].
5. **The other way: an operation that pays for itself.** If an op pays its own fee, in ETH
   through its gas fields or in a token through one of its calls, nobody has to front anything.
   The bundler needs no account, no key and no limits: it checks that the op pays enough and
   sends it. On Ethereum, builders drop a bundled transaction that reverts, so an op that would
   fail is never included and costs nothing.
6. **The first transaction, from any token.** The worked example: a user holding USDT and no
   ETH. With EIP-7702, the delegation is a signature, so one operation can swap part of the
   token into one the bundler accepts, make the app's approvals and calls, and pay the fee.
7. **What we run.** A bundler on Ethereum and Sepolia with no key, priced from on-chain pools,
   paid only when the op lands, never a paymaster. Changing the bundler URL in viem,
   permissionless.js, ZeroDev, Alchemy's or MetaMask's SDK is the whole integration. Its
   specification is open, and heading for an ERC.
8. **What we'd like to see.** Bundlers that take payment from the op, so a bundler URL can be
   public, as Etherspot's free bundler already is. The shared mempool as the default path. SDKs
   that treat the bundler as a URL the developer chooses.

## Draft

**ERC-4337's API key problem**

ERC-4337 set out to give Ethereum smart accounts without changing the protocol. A user signs an
operation, any bundler picks it up, and the bundler pays the gas and is paid back from the
account. Operations were to travel through a shared mempool, so that no single operator could
decide what gets included. It was meant to be as open as sending a transaction.

Today, using a bundler starts with signing up. You create an account with a vendor, get an API
key, and put it in the bundler URL. The SDK you install is usually the same vendor's, and its
examples point at its own endpoint. If your users have no ETH, which is most users, you also
need the vendor's paymaster. That means a deposit, a spending policy and a monthly plan.

None of this is a failure of the standard. It follows from one question: who pays for the gas
before anyone has paid for it?

### The paymaster is the product

A new user holds no ETH. Their first operation needs gas, so someone has to pay it up front and
hope to be repaid. ERC-4337's answer is the paymaster, a contract that pays for an operation
from its own deposit. Whoever runs it carries the risk. So a paymaster needs to know who it's
paying for. It needs an account, a key to tie requests to that account, limits on what it will
spend, and a way to bill for it.

Once the paymaster needs an account, there's little reason for the bundler not to need one too.
The two are sold together, priced per month or as a markup on gas, and billed to the developer.
The developer, not the user, becomes the customer.

### What it costs to let someone in

For an established app, a gas plan is a line item. For a team still finding its users, it's a
fixed cost paid before the first transaction. [The public post, quoted and linked.] The fee
isn't for gas. It's for an account with a vendor whose service could, in principle, be open to
anyone.

### An operation that pays for itself

There's another answer to the question. If the operation pays its own fee as it runs, nobody
has to pay anything up front.

An ERC-4337 operation can already do this in ETH: its gas fields pay the bundler, with no
paymaster. It can do it in a token too. One of the account's own calls transfers the fee to the
bundler, in the same operation as everything else it does. The bundler simulates the operation,
checks that it pays enough, and sends it. If it doesn't pay, it isn't sent.

A bundler that works this way needs to know nothing about who sent the operation. It needs no
account, no key and no spending limits, because it never spends anything it isn't paid back in
the same transaction. Its URL can be public.

On Ethereum, it can go further. Builders accept bundles and drop a transaction in one that would
revert. A bundler that sends this way never has an operation included that fails, so a failed
operation costs its sender nothing. The bundler doesn't pay for it either.

### The first transaction, from any token

Take a user holding USDT and no ETH. USDT has no permit and no EIP-3009, so every app wants an
approval first, and the approval needs gas.

With EIP-7702, the user delegates their account to a smart account implementation by signing an
authorization. That signature is not a transaction. The first operation can then do everything
in one go:

1. swap part of the USDT into a token the bundler accepts, enough to cover the fee;
2. make the app's calls: the approval (USDT's set to zero first), a swap through whichever
   router the app uses, a deposit, a transfer;
3. pay the bundler's fee in the token from the first step.

The user needed a signature and the token, nothing else. The app needed a bundler URL.

### What we run

[Bruno, briefly: a bundler on Ethereum and Sepolia at a public URL, no key, EntryPoint v0.7 and
v0.8, EIP-7702 accounts, the fee priced from on-chain pools, never a paymaster. Tested with
viem, permissionless.js, ZeroDev, Alchemy's and MetaMask's SDKs, by changing the bundler URL.
Its specification is open, heading for an ERC. Link the docs.]

### What we'd like to see

[Bundlers taking payment from the operation, so their URLs can be public. The shared mempool
as the default path for operations. SDKs that treat the bundler as a URL the developer
chooses, and document how to use another.]

## To check before writing

- BundleBear (or a Dune dashboard): the share of operations by bundler and by paymaster on
  Ethereum and overall, with the date taken.
- The shared mempool: the share of operations through it on Ethereum.
- Etherspot's free bundler: its limits and terms, and how it's paid for. If it's subsidised,
  that's the contrast: it's free because someone pays, where an op paying for itself needs no
  one to.
- Vendor pricing beyond Pimlico's (Alchemy, ZeroDev, Privy, Dynamic), from their own pricing
  pages.
- The public post about $500 a month, and two or three other public complaints, to quote and
  link. Search found none; they need direct searches on X, Reddit and Farcaster.
- Bundled reverting transactions: builders' published bundle semantics (Flashbots' docs for
  `eth_sendBundle`, and the BuilderNet report of a reverted transaction included, May 2026).
- The worked example run end to end on Sepolia, with the transaction to link.
