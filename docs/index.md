---
description: "What Bruno is: a relay that sends a client's call and is paid by it as it runs, in ETH, any token or over x402; how a submission runs, and the client's contract"
---

# Bruno: permissionless post-paid gas relays

A transaction's sender must hold its gas before it's sent. Bruno sends a client's call and
pays the gas, and the call pays Bruno back as it runs: cash on delivery, in ETH or any token,
from whatever the call can reach. What the account holds, what the call releases or earns,
another account's signed payment: nothing is needed in advance.

Or pay by **x402**: a client holding only USDC signs an EIP-3009 authorization of the price,
and Bruno spends it in the call's own transaction. Bruno is its own facilitator, so the payment
moves only if the call succeeds and lands. Any x402 client can pay it: Ethereum's USDC is one of
x402's default assets.

The mechanism rests on builders: Bruno sends only through `eth_sendBundle` (or equivalent),
so that a builder drops a reverting transaction instead of including it. A
transaction that doesn't pay therefore never lands, and costs Bruno nothing. A builder that
includes it reverted makes Bruno pay its gas, unpaid; the client is protected by the payment
check either way.

1. **Submit.** The client sends one call (typically to its own account, running a batch it
   signed through its 7702 delegate), its EIP-7702 authorization if needed, and a payment:
   ETH or a token, and an amount.
2. **Wrap.** Bruno generates a runner: initcode that reads the payee's balance, makes the
   call, and reverts unless the payee (Bruno's payment account) gained at least the amount.
3. **Send.** One of Bruno's concurrency accounts sends the runner to Bruno's deployer, which
   CREATE2s it at an address bound to that account and block. The client's authorization
   rides in the same type-4 transaction. The float funds the accounts' gas.
4. **Bundle.** The transaction goes to builders in `droppingTxHashes`, or alone in its
   bundle.

The client's signatures fix what runs and what it pays; Bruno can only include it or not.
Each runner is a new address that self-destructs at the end of its transaction, so nothing
granted to it outlives the transaction, and nothing is left in state.

## Client contract

Nothing is included unless, during the call, the payee gains at least the amount; a
submission that isn't included costs the client nothing. Everything else is the client's to
enforce by its own signature: what the call does and in what order, any delegation it relies
on, and its replay protection, since anyone who sees a submission can run it. In full:
[SPEC.md](SPEC.md).

## Where next

- **Use it:** [choosing a way in](using/index.md): REST, JSON-RPC or x402, the TypeScript
  client, or an AgentKit agent.
- **What works where:** [support](support.md), and the [networks and builders](networks.md).
- **The standard:** the [specification](SPEC.md), as a client sees it.
- **How Bruno provides it, and why:** the [design](DESIGN.md).
- **Running a relay:** [deployment](DEPLOYMENT.md), and [development](development.md).
