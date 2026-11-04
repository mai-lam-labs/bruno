---
description: "Plan, for analysis: the agent payment standards beyond what Bruno speaks today, which ones its stack could support with little new, and what to find out before building each"
---

# Plan: agent payment standards, for analysis

The standards around agents paying (surveyed 2026-11-03), sorted by how much of Bruno's
existing stack each would reuse: the runner (a payment and a call in one transaction), bundles
that never land a reverting transaction, the relay's own EIP-3009 settlement, the ERC-4337
bundler, and the planned MCP server and `upfront` x402. Nothing here is decided. Each entry
says what it is, where it would sit in Bruno, a first guess at the effort, and what to find
out before building it. The guesses are to confirm by reading each spec as written.

Already planned elsewhere, and not repeated here: ERC-7710 and ERC-7715
([delegated permissions](delegations.md)), x402's facilitator interface, `auth-capture` and
`extra.splits` ([facilitator](facilitator.md)), x402 settled first ([`upfront`](x402-async.md)),
and MCP ([MCP](MCP.md)).

## Paying in more tokens

Bruno takes payment in any token with EIP-3009. Most tokens don't have it.

- **EIP-2612 `permit` as a payment** ([EIP-2612](https://eips.ethereum.org/EIPS/eip-2612)).
  The runner calls `permit`, then `transferFrom` to the payee, then the call, all in one
  transaction, so the payment and the call land together as with 3009. First guess: small to
  medium. To find out first: a permit names its spender when it's signed, and the runner's
  address changes with each transaction (3009 has no spender: anyone submits it, and it pays
  the payee directly), so the spender has to be an address fixed in advance that the runner
  acts through; then how many of the tokens Bruno prices have `permit`; the
  nonce (sequential, unlike 3009's random one) and what it means for a quote signed ahead of
  a busy account; tokens whose `permit` differs from the standard (DAI's).
- **Permit2** ([Uniswap/permit2](https://github.com/Uniswap/permit2)). x402's `exact` uses it on
  EVM for tokens without 3009. It needs the payer's one-time approval of the Permit2 contract;
  x402's `eip2612_gas_sponsoring` and `erc20_gas_sponsoring` extensions
  ([extensions](https://github.com/x402-foundation/x402/tree/main/specs/extensions)) cover
  getting that approval when the payer has no ETH, which is Bruno's own case. The runner could
  make the approval and the transfer in the transaction that uses them. First guess: medium, a
  new payment kind in the runner and x402's Permit2 payload. To find out: the extensions'
  exact flow, and whether an approval Bruno pays for can itself be paid for in the same
  transaction.

## More of x402

- **`upto`** ([scheme](https://github.com/x402-foundation/x402/blob/main/specs/schemes/upto/scheme_upto.md)).
  The client authorises a maximum, and the settlement takes up to it, possibly nothing. That
  fits a relay paid after the fact: a quote is a ceiling, and the call's real gas is known only
  once it runs. First guess: medium, if the runner can settle the amount measured during the
  call. To find out: how `upto` authorises on EVM (Permit2 or otherwise; EIP-3009 moves an exact
  amount), and whether the settled amount can be computed inside the transaction.
- **`payment_identifier`** and **`offer-and-receipt`**
  ([extensions](https://github.com/x402-foundation/x402/tree/main/specs/extensions)). By their
  names, an idempotency key for a payment and a signed offer with its receipt: the idempotence
  and expectation management that x402 lacks elsewhere. First guess: small, if they're what
  their names say. To find out: read both; whether a receipt can carry the submission's status
  URL.
- **`bazaar`.** Discovery of x402 services and MCP tools. Covered for a facilitator in
  [facilitator](facilitator.md); to find out whether Bruno's own endpoint can be listed without
  being a facilitator.
- **`batch-settlement`** ([schemes](https://github.com/x402-foundation/x402/tree/main/specs/schemes)).
  An escrow and off-chain vouchers, settled in batches. First guess: large, a contract and a
  voucher ledger. Later, if at all.
- **x402 over A2A** ([transport](https://github.com/x402-foundation/x402/blob/main/specs/transports-v2/a2a.md))
  and AP2's x402 extension ([a2a-x402](https://github.com/google-agentic-commerce/a2a-x402)).
  The payment rides on an A2A task's `input-required` state. The semantics would be Bruno's
  `upfront` x402, over another transport, as MCP is. First guess: medium, once `upfront`
  exists. To find out: whether any A2A client pays this way in practice.

## Other payment protocols

- **Machine Payments Protocol** ([mpp.dev](https://mpp.dev/protocol)), Stripe and Tempo's,
  also on HTTP 402. It claims compatibility with x402's charge flows
  ([comparison](https://mpp.dev/mpp-vs-x402)). Its production methods are Tempo and cards,
  with Solana; Bruno serves neither chain. To find out: whether a method exists, or is
  allowed, for an EVM chain Bruno serves; whether serving both protocols on one endpoint
  (as its `mppx` does) costs Bruno anything. Until then, watch.
- **L402, ACP, UCP.** Lightning, and card checkout. Not Bruno's.

## Identity and trust

- **ERC-8004 Trustless Agents** ([EIP](https://eips.ethereum.org/EIPS/eip-8004), Draft).
  Registries for an agent's identity, reputation and validation. Bruno could register its
  endpoints as a service: one transaction and a metadata file, for discovery by agents that
  read the registry. First guess: small. To find out: who reads it today, which deployment
  is canonical, and whether a registration is worth keeping current.
- **Web Bot Auth** ([IETF](https://datatracker.ietf.org/wg/webbotauth/documents/)). Agents
  signing their requests so a site can tell who they are. Bruno serves anyone and asks no one
  who they are, so it has nothing to verify. Not Bruno's, unless a client wants Bruno's own
  outgoing requests signed.
- **Verifiable MCP** ([proposal](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/3354),
  [reference](https://github.com/ripple-node-lab/mcp-verifiable-tools-demo)). Evidence attached
  to a tool's result: a TEE attestation or a proof. Bruno's results are checked on chain
  already (the transaction either did what was asked or never landed), and Bruno runs in no
  TEE. Watch. Its `outputCommitment` doesn't cover `structuredContent`, where x402 over MCP
  puts the payment, so the payment details would be unattested even where it's used.
- **ERC-8183 Agentic Commerce** ([EIP](https://eips.ethereum.org/EIPS/eip-8183), Draft). A job
  held in escrow until an evaluator approves it. A contract for agents to call; Bruno would
  send those calls like any other. Nothing to build.
- **Elpis** ([elpis.devilabs.dev](https://elpis.devilabs.dev/), Devilabs; read 2026-11-04). A
  service, not a standard: rules an agent's payment is checked against before money moves
  (ceilings, allowlists, conditions agreed in advance), each decision recorded with its reasons,
  and an ERC-8183 evaluator built on them
  ([integrations](https://elpis.devilabs.dev/docs/integrations)). The decision and the payment
  are kept apart: the customer's own wallet signs every transaction, through a vault the
  customer deploys, holding USDC while a payment is staged, with a guardian who can halt it
  ([custody](https://elpis.devilabs.dev/docs/custody)). It names Arc mainnet as its network. Its
  MCP server ([agents](https://elpis.devilabs.dev/docs/agents)) splits tools by scope, so no
  tool both decides and executes, and asks for an idempotency key on every proposal. For
  Bruno, the transactions it builds are ones Bruno could send, a payer with USDC and no ETH
  being its case, though not on Arc, which Bruno doesn't serve. First guess: nothing to build
  for the transactions; the MCP shape is worth reading alongside [MCP](MCP.md). To find out: who
  submits the signed transactions and pays their gas (the docs read so far don't say); which
  chains besides Arc; whether its vault calls could be paid for through Bruno.

## Order, a first guess

1. EIP-2612 as a payment: the most tokens for the least change.
2. `payment_identifier` and `offer-and-receipt`, once read.
3. `upto`, once its EVM mechanism is known.
4. Permit2, with the gas-sponsoring extensions.
5. ERC-8004 registration, if anyone reads the registry.
6. A2A, after `upfront`; MPP, if it reaches a chain Bruno serves.
