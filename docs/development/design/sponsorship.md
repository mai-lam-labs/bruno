---
description: "Why a sponsorship is the account's own transactions between two of the relay's, why its payment is checked by a forwarder anyone may call, what the account is sent, and what whole or not at all rests on"
---

# Sponsorship

The [specification](../../SPEC.md#sponsorship) says what a sponsorship is and what a client
sends. This page records why it's built the way it is.

## Why the account's own transactions

A call Bruno makes through a runner has the runner as its `msg.sender`. It works only where
the call carries the user's authority in its data: a batch a delegate checks, or a signed
transfer. A sponsorship is for everything else. The transactions are the account's own, signed
by its own wallet, so every contract sees the account as the caller. The account needs no code
and no earlier transaction, and any wallet that signs a transaction without sending it can use
it.

ERC-4337 reaches some of the same users, and Bruno keeps the two apart. A user operation needs
a smart account. Having someone else pay for it needs a paymaster, which holds a deposit at
the EntryPoint and vouches for each operation before it runs. Bruno is a bundler and never a
paymaster. A sponsorship needs neither: the account is a plain key, and its payment is checked
on the chain after its transactions have run, in the same bundle.

## The three parts

A sponsorship is sent as one bundle:

1. Bruno's first transaction sends the account the ETH its transactions need.
2. The account's own transactions follow, as the account signed them.
3. Bruno's last transaction calls the forwarder, which sends the payee what the account paid.

The last transaction is the condition. Bruno signs it, and the forwarder reverts unless the
payee gained the amount. A builder doesn't include a bundle with a transaction that reverts,
so Bruno's ETH reaches the account only in a block where the account pays for it.

Bruno's transaction comes first because the account has no gas to pay with before it. Sending
ETH to an account that has code runs the code, so the first transaction can also carry the
account's EIP-7702 authorizations and a call into its code. An account whose code pays can pay
from that call, with no transaction of its own between Bruno's two.

Both of Bruno's transactions come from one concurrency account, chosen when a batch is made, as
for any submission. The address the account pays doesn't depend on which, so a quote allocates
nothing.

## The forwarder

The runner measures a payment by reading the payee's balance before and after a call it makes
itself. A sponsored account pays in a transaction of its own, where nothing of Bruno's runs.
So the account pays a contract, the forwarder, and Bruno's last transaction calls it. The
forwarder sends what it holds to the payee and reverts unless the payee gained the amount. It
measures as the runner does, by the payee's balance within its own call. A token that answers
false, takes a fee or sends the money elsewhere counts for what arrived.

The forwarder has no owner, no storage and no permissions. The payee's address is in its code,
so whatever it sends goes to the payee and nowhere else, and anyone may call it:

| Who calls the forwarder | When | What follows |
|---|---|---|
| Bruno's concurrency account | In its bundle | The check |
| Anyone else, the account included | Between the account's payment and Bruno's check | The payment reaches the payee early. Bruno's check then finds too little and reverts, so the bundle isn't included, and the payment with it |
| Anyone | Between blocks | The payee is sent what was left there |

Each call sends on all the forwarder holds of the asset, so in any block the calls take out no
more than was paid in, and every bundle that lands was paid its amount. This holds even if a builder
puts other transactions between a bundle's own. A payer who pays more than the price pays the
payee more, as with any submission.

The forwarder keeps one base unit of each token it has held. A balance that returns to zero
has its storage slot cleared, and setting the slot again at the next payment costs far more
than writing to one that's set. The unit is the contract's own rule. The relay asks for the
amount, and the check allows for the unit that stays behind, so the payee may gain one base
unit less than the amount. The first payment of a token sets the slot, and that gas is in the
payer's own transaction. An operator can send the forwarder one unit of each token ahead of
time, so that no payer pays for it.

The forwarder's address follows from the payee. It's deployed through Arachnid's deployer,
with the payee in its initcode. Nothing is configured, and a new payee
has a new forwarder. Because the code fixes the address, a relay only has to know whether code
is there. Where there's none it takes no sponsorship, since a call to an address without code
succeeds and would check nothing.

The forwarder is part of what the account signs. So a sponsorship keeps the forwarder it was
taken with, and its check calls that one until it lands or expires.

## What the account is sent

The account is sent each transaction's gas limit at its max fee, and the value it sends, in
full, whatever the account already holds. The amount then follows from what was submitted
alone, and nothing of the account's balance has to be read at each block. What the account
doesn't spend stays with it, and the price covers all of it.

ETH that a transaction sends to the forwarder isn't included. That ETH is the payment, and a
price that included it would have to cover itself. An account paying in ETH must hold the
price already, or be brought it by one of its earlier transactions.

One total caps what the client chooses. When a sponsorship is quoted and when it's taken,
Bruno's two transactions' gas and everything the account is sent must together cost no more
than a transaction of `max_gas` would at the most a block may ask by the deadline. The value
a transaction sends, and a max fee signed above what the deadline needs, take from the same
room as gas. A sponsorship is for gas: an account that wants to send real ETH must hold it.

What the account is sent is fixed from then on. Bruno's own gas isn't. It's signed each block
at that block's fees, as a call's is, and the tip has no ceiling, so after a spike a
sponsorship can cost more than the figure it was taken under. Two things still hold. It's
signed for a block only where its payment covers its charge at that block's fees, so what
comes back is worth more than what it cost. And it never costs more than a call of `max_gas`
would in the same block, or than the figure it was taken under, whichever is higher. Bruno's
gas floating with the market is accepted for a sponsorship as it is for a call.

Because the cap is read from the fees when a request is made, a sponsorship quoted right at it
can be refused when submitted if fees have fallen since. Its refusal says by how much.

The account may pay out of the ETH Bruno sent it. The check passes only if the payee gained
the amount, and the amount is priced above everything Bruno sends, so Bruno is paid whatever
the money's source.

## Fees, nonces and the deadline

The account signs once and can't sign again for each block, as Bruno does for its own
transactions. So each of its transactions is signed at a max fee no block before the deadline
can exceed. A base fee can rise by an eighth a block, which makes that fee about 1.42 times
the next block's three blocks ahead and 3.25 times ten blocks ahead. A nearer deadline
therefore costs less. The deadline is fixed at the quote, because the quote fills in the fees.

The nonces are fixed at signing too, so one sponsorship of an account is pending at a time. A
second would be signed at the same nonces.

## Priced as submitted

No quote is kept. A sponsorship is planned and priced as it was submitted, as a call is, and
is never compared with a quote. For that to work, its price must not depend on the amount it
pays. The amount appears in calldata, and calldata costs more for a nonzero byte than a zero
one. So calldata that may hold the amount is priced as if no byte of it were zero. Otherwise a
client told what's required, and paying exactly that, could be told it had underpaid again.

A quote simulates the bundle once for each token asked for, with a payment in that token in
place. The check passes only with the payment there, and each token's payment is a different
transaction.

A sponsorship has its own two methods. As fields of a call's quote and submission, one request
object would hold two requests, most fields of one refused beside the other's, and the OpenAPI
and OpenRPC documents would allow shapes the relay refuses.

## What whole or not at all rests on

A builder keeps the bundle whole, not the chain. The bundle goes to every builder with
nothing in it a builder may leave out ([bundling](bundling.md)), so it works on chains whose
builders take bundles of several transactions: Ethereum and Sepolia.

If a builder lands part of it, the relay finds some of its own transactions landed and not the
rest. It then stops. The sponsorship is rejected with the reason `bundle-broken`, the chain's
problems list it with the ETH that was sent, and nothing more of it is sent: neither the
account's transactions, which would then land in pieces, nor the check. Sending the rest could
recover Bruno's payment, but only by landing the account's transactions outside the bundle
they were signed for, and a builder that broke one bundle may break the next. The guarantee
comes before Bruno's own funds. What Bruno loses is its first transaction's gas and the ETH
that transaction sent, which the cap bounded when the sponsorship was taken.

Two things outlast a broken bundle. The account's signed transactions stay valid while their
nonces are unused, whoever holds them. A payment that reached the forwarder without its check
stays there until the next call to the forwarder for that asset, anyone's, which sends it to
the payee.

A block that's orphaned was gossiped first, so its transactions are public and anyone may land
one of them alone. Every other transaction of Bruno's checks its own payment, so a sponsorship
is the first this matters for. On mainnet 903 of 805,546 slots were re-orged between Pectra and
27 August 2025, about 0.11%
([Decipher, on ethresear.ch](https://ethresear.ch/t/post-pectra-effects-on-ethereum-reorg-rate-propagation-and-block-size/23018)).
A block's contents have also left an MEV-Boost relay without the block landing: in April 2023
a proposer took them with an invalid header and unbundled about $20M of searchers' bundles
([Flashbots' post-mortem](https://collective.flashbots.net/t/post-mortem-april-3rd-2023-mev-boost-relay-incident-and-related-timing-issue/1540)).
Nobody can cause a reorg when they choose, and only the account gains from the first
transaction landing alone, so the risk is accepted. How ePBS treats a payload revealed late
hasn't been checked.

## Compared with a builder's own sponsorship

Titan's builder sponsors searchers' bundles: one that fails for lack of gas funds has an ETH
transfer of Titan's put before it, if the bundle increases the builder's balance
([Titan's docs](https://docs.titanbuilder.xyz/llms-full.txt),
[Titan Tech Teatime](https://titanbuilder.substack.com/p/titan-tech-teatime-1)). A Flashbots
[proposal](https://collective.flashbots.net/t/proposal-builder-funded-gas-sponsorship-for-bundles/5820)
would bring the same to BuilderNet.

| | Titan | Bruno |
|---|---|---|
| Who sponsors | One builder, in its own blocks | Any builder that takes a bundle of several transactions |
| Repaid in | ETH, to the builder | Any token Bruno prices, or ETH, to its payee |
| Payment enforced by | The builder's own simulation | The bundle's last transaction, on the chain |
| For | Searchers' bundles | Any account's transactions |

## Set aside

- A payment address for each sponsorship, swept by the check, would set a new balance slot
  every time and pay the deployer's overhead on every check.
- A contract that records the payee's balance for the check to compare needs storage of its
  own, and is sound only if a bundle's transactions are contiguous.
- A deposit address for each user would have its payment swept outside the bundle, so nothing
  would tie the payment to the transactions it pays for.
- The concurrency account can't be the address paid, because it isn't chosen until the batch,
  after the account has signed.
- Multicall3 can't stand in for the forwarder. Anyone can move what it holds, and it sends only
  the amount asked, so a tip stays behind for whoever sweeps it. `aggregate3` checks only that
  a call succeeded, so a token answering false passes. It can't forward ETH
  ([Multicall3.sol](https://raw.githubusercontent.com/mds1/multicall3/main/src/Multicall3.sol)).
