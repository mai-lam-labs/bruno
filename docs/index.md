---
description: "What Bruno is: a relay that sends your transaction first and is paid during it, in tokens or over x402, so an account with no ETH can still transact; and how it works"
---

# Bruno: permissionless post-paid gas relays

Bruno solves gas's chicken-and-egg problem: an account needs ETH to send a transaction, and
often needs to send a transaction to get ETH. Bruno sends the transaction first and is paid
during it, in whichever way suits the client:

- from the tokens the account holds, with no ETH at all;
- from what the call itself releases or earns;
- from another account's signed payment;
- over x402, holding nothing but USDC.

## How it works

Normally a transaction's sender must hold enough ETH for its gas before sending it. With Bruno,
the client hands its call to the relay instead. Bruno sends it and pays the gas, and the call
pays Bruno back while it runs. It works like cash on delivery: the payment can be in ETH or any
token Bruno accepts, and it can come from anything the call can reach. The client doesn't need
to hold anything in advance.

The client can also pay with **x402**. A client holding only USDC signs an EIP-3009
authorization for the price, and Bruno collects it inside the call's own transaction. Bruno
acts as its own facilitator, so the payment only moves if the call succeeds and lands. Any x402
client can pay Bruno, because Ethereum's USDC is one of x402's default assets.

Bruno relies on block builders to keep this safe. It only sends transactions as bundles,
through `eth_sendBundle` or an equivalent, and builders drop a bundled transaction that
reverts instead of including it. A transaction that doesn't pay Bruno therefore never lands
and costs Bruno nothing. If a builder breaks that rule and includes the reverted transaction,
Bruno pays its gas and isn't paid back. The client is protected either way, because the payment
check is part of the transaction.

A submission goes through four steps:

1. **Submit.** The client sends Bruno one call, a payment (ETH or a token, and an amount) and,
   if its account isn't delegated yet, an EIP-7702 authorization. Typically the call goes to
   the client's own account and runs a batch the client signed through its EIP-7702 delegate.
2. **Wrap.** Bruno generates a runner: initcode that reads the payee's balance, makes the
   call, and reverts unless the payee (Bruno's payment account) has gained at least the
   amount.
3. **Send.** One of Bruno's concurrency accounts sends the runner to Bruno's deployer, which
   deploys it with CREATE2 at an address tied to that account and block. The client's
   authorization rides in the same type-4 transaction. Bruno's float account funds the
   concurrency accounts' gas.
4. **Bundle.** Bruno sends the transaction to builders, either marked as droppable
   (`droppingTxHashes`) or alone in its own bundle.

The client's signatures decide what runs and what it pays. All Bruno can decide is whether to
include it. Each runner is deployed at a new address and self-destructs at the end of its
transaction, so nothing granted to it outlives the transaction and nothing is left in state.

## Next steps

- **[Getting started](using/index.md):** REST, JSON-RPC or x402, the TypeScript client, or an
  AgentKit agent.
- **What works where:** [support](support.md), and the [networks and builders](networks.md).
- **Working on Bruno, or running a relay:** [development](development/index.md), with the
  [specification](SPEC.md), the [design](development/design/index.md) and
  [deployment](DEPLOYMENT.md).
