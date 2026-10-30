# Bruno: permissionless post-paid gas relays via Bundlers

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

**Documentation: [docs.mailam.app/bruno](https://docs.mailam.app/bruno/)**: paying over x402,
the TypeScript client and the AgentKit provider, what works where, the specification, the
design, and running a relay. The relay itself: [bruno.mailam.app](https://bruno.mailam.app).

## License

Copyright mai-lam-labs.

- **The relay** (its Python reference, the Cloudflare Worker, the image) and everything else
  not listed below: [AGPL-3.0-or-later](LICENSE). Run it as a service, modified, and you offer
  its users your source ([running a relay](https://docs.mailam.app/bruno/DEPLOYMENT/#the-license)).
- **The TypeScript client** (`clients/typescript`, `@mailam/bruno`) and **the AgentKit
  provider** (`integrations/agentkit`, `@mailam/bruno-agentkit`): Apache-2.0, each its own
  `LICENSE`, shipped in its package.
- **The documentation** (`docs/`, `articles/`, the specification included) and **its
  examples** (`npm-userops/`): [CC0 1.0](docs/LICENSE), dedicated to the public domain: copy
  them freely.

The code up to 0.5.1 was published under MIT or Apache-2.0, at your option, and stays so.
