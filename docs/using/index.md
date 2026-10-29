---
description: "The ways to use Bruno, compared: REST, JSON-RPC and x402, and the TypeScript client and AgentKit provider built on them; who each suits, what you need to hold and what you sign"
---

# Getting started

There are several ways to use Bruno, and they all reach the same relay with the same
[guarantees](../SPEC.md#semantics): nothing is included unless the payee receives the amount
during the call, and a call that isn't included costs nothing. The options differ in what you
need to hold and what you sign.

## Interfaces

| | Best for | What you hold | What you sign | Steps |
|---|---|---|---|---|
| [REST](rest.md) | Apps and backends, from any HTTP client | Whatever the call pays from: the account's tokens or ETH, or what the call releases or earns. Or only USDC, paid by a [signed transfer](rest.md#paying-with-a-signed-transfer) | The call, typically a batch run by the account's EIP-7702 delegate, plus an authorization if the account isn't delegated yet. Or a USDC transfer of the price, from a key or a smart wallet | Get a quote, submit, then poll the status |
| [JSON-RPC](json-rpc.md) | The same, from Ethereum tooling | Same as REST | Same as REST | Same as REST, at a single endpoint |
| [x402](x402.md) | Any x402 client, such as an agent or a wallet | Only an EIP-3009 token (USDC) | An EIP-3009 transfer of the price, from a key or a smart wallet, deployed or not. The call itself doesn't have to pay anything | One request, which stays open until the call lands |

REST and JSON-RPC offer the same operations, with the same request and response objects.
MCP support is planned.

## Integrations

| | Best for | How it pays |
|---|---|---|
| [TypeScript client](typescript.md), `@mailam/bruno` | JavaScript and TypeScript apps on Deno, Node or Bun | Any way: a submission that pays for itself or is paid by a signed transfer, or x402. Works with any signer of typed data, smart accounts included |
| [AgentKit](agentkit.md), `@mailam/bruno-agentkit` | Agents built on Coinbase's AgentKit | x402, in USDC, from the agent's wallet: a key, or a smart wallet, deployed or not |
| [ERC-4337 accounts](user-operations/index.md), with viem or ZeroDev | Smart accounts that already send user operations | As at any bundler, by the op's fees in ETH; or, holding no ETH, by its own calls, in a token the relay takes, its fees zero |

For which chains, tokens and payers each option has been tested with, see
[support](../support.md).
