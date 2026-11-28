---
description: "The ways to use Bruno, compared: REST, JSON-RPC and x402, and the TypeScript client built on them; who each suits, what you need to hold and what you sign; a call the relay makes, or your own transactions sponsored"
---

# Getting started

There are several ways to use Bruno, and they all reach the same relay with the same
[guarantees](../SPEC.md#semantics): nothing is included unless the payee receives the amount
during the calls, and calls that aren't included cost nothing. The options differ in what you
need to hold and what you sign.

## Interfaces

| | Best for | What you hold | What you sign | Steps |
|---|---|---|---|---|
| [REST](rest.md) | Apps and backends, from any HTTP client | Whatever the call pays from: the account's tokens or ETH, or what the call releases or earns. Or only USDC, paid by a [signed transfer](rest.md#paying-with-a-signed-transfer) | The call, typically a batch run by the account's EIP-7702 delegate, plus an authorization if the account isn't delegated yet. Or a USDC transfer of the price, from a key or a smart wallet | Get a quote, submit, then poll the status |
| [JSON-RPC](json-rpc.md) | The same, from Ethereum tooling | Same as REST | Same as REST | Same as REST, at a single endpoint |
| [x402](x402.md) | Any x402 client, such as an agent or a wallet | Only an EIP-3009 token (USDC) | An EIP-3009 transfer of the price, from a key or a smart wallet, deployed or not. The call itself doesn't have to pay anything | One request, which stays open until the call lands |

REST and JSON-RPC offer the same operations, with the same request and response objects.

## A call, or your own transactions

Everything above sends calls that Bruno makes for you: their `msg.sender` is not your
account, so a call that acts for you has to carry your signature (a delegate's batch, a signed transfer). If
you want ordinary transactions sent from your own account instead, with a token and no ETH, ask
for a [sponsorship](sponsorship/index.md): you sign the
transactions, and Bruno sends them between two of its own, funding the account and collecting
its payment. A sponsorship has its own two operations over REST and JSON-RPC
(`postpaid_quoteSponsorship`, `postpaid_sponsor`), and the TypeScript client's
[`sponsor`](typescript.md#sponsored-transactions) does all of it.

| | A call | A sponsorship |
|---|---|---|
| Who makes the calls | Bruno's transaction, or a transaction of Bruno's for each | Your account's own transactions |
| What you sign | The call's own authorization, or a transfer | Each transaction, and one more that pays |
| What you need | An account whose code checks your signature, or an EIP-3009 token | Any account with a key, holding a token Bruno takes, in a wallet that signs a transaction without sending it (a local key, not a browser wallet) |
| What keeps it whole | The chain, where the calls and payment are one transaction; the builder that includes the bundle, where each is its own | The builder that includes the bundle |

## Integrations

| | Best for | How it pays |
|---|---|---|
| [TypeScript client](typescript.md), `@mailam/bruno` | JavaScript and TypeScript apps on Deno, Node or Bun | Any way: a submission that pays for itself or is paid by a signed transfer, x402, or a sponsorship of your own transactions. Works with any signer of typed data, smart accounts included |
| [ERC-4337 accounts](user-operations/index.md), with viem or ZeroDev | Smart accounts that already send user operations | As at any bundler, by the op's fees in ETH; or, holding no ETH, by its own calls, in a token the relay takes, its fees zero |

For which chains, tokens and payers each option has been tested with, see
[support](../support.md).
