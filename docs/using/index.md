---
description: "The ways to use Bruno, side by side: REST, JSON-RPC and x402, and the TypeScript client and AgentKit provider built on them; who each suits, what the caller holds and what it signs"
---

# Choosing a way in

Every way in reaches the same relay, under the same [contract](../SPEC.md#semantics): nothing
is included unless the payee gains the amount during the call, and a call that isn't included
costs nothing. They differ in what the caller holds and what it signs.

## Interfaces

| | Suits | The caller holds | It signs | Steps |
|---|---|---|---|---|
| [REST](rest.md) | Apps and backends, any HTTP client | Whatever its call pays from: its account's ETH or tokens, what the call releases or earns | Its call, typically its account's batch through its EIP-7702 delegate, and an authorization if it isn't delegated yet | Quote, submit, poll the status |
| [JSON-RPC](json-rpc.md) | The same, from Ethereum tooling | The same | The same | The same, at one endpoint |
| [x402](x402.md) | Any x402 client: agents, wallets | An EIP-3009 token (USDC), nothing else | An EIP-3009 transfer of the price; the call needn't pay anything itself | One request, held until the call lands |

REST and JSON-RPC are the same four operations, with the same request and response objects. MCP
is planned.

## Integrations

| | Suits | Pays by |
|---|---|---|
| [TypeScript client](typescript.md), `@mailam/bruno` | JavaScript and TypeScript apps: Deno, Node, Bun | Either: a submission, or x402, with any signer of typed data |
| [AgentKit](agentkit.md), `@mailam/bruno-agentkit` | Agents built on Coinbase's AgentKit | x402, in USDC, from the agent's wallet |

Which chains, tokens and payers each is proven with: [support](../support.md).
