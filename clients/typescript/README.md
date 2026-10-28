# @mailam/bruno

A client for [Bruno](https://bruno.mailam.app), the permissionless gas relay. You can send a call on
Ethereum without holding ETH. Bruno is paid in ETH or a token during the call itself, and only if
the call succeeds and lands on-chain.

- **Post-paid and atomic:** your call and Bruno's payment are in the same transaction. If the call
  reverts, or never gets included, you pay nothing.
- **Three ways to pay:**
  - **The call pays for itself:** in any token the relay takes, from your own account's signed
    batch.
  - **A signed transfer:** a USDC transfer that any signer of typed data signs, which the relay
    makes just before your call.
  - **Over [x402](https://x402.org):** the same signed transfer, paid the x402 way.
- **Smart accounts:** a smart account can pay before it's deployed. Bruno deploys it in the same
  transaction (ERC-6492), so it never needs ETH.
- **No API key, no dependencies:** tested on Deno, Node 20 or later, and Bun. It only uses
  web-standard APIs, so it's meant to run in browsers and Cloudflare Workers too, but isn't tested
  there yet.
- **Chains:** Ethereum mainnet (`"ethereum"`) and Sepolia (`"sepolia"`).

## Install

```sh
deno add jsr:@mailam/bruno
npx jsr add @mailam/bruno     # npm
pnpm i jsr:@mailam/bruno      # pnpm
bunx jsr add @mailam/bruno    # Bun
```

## Tokens and amounts

You can name a token by its alias on the chain, or by its address:

| Chain      | Aliases                    |
| ---------- | -------------------------- |
| `ethereum` | ETH, WETH, USDC, USDT, DAI |
| `sepolia`  | ETH, WETH, USDC            |

An amount always says its unit:

- **`{ decimal: "2.5" }` or `{ decimal: 2.5 }`:** whole units. The value must be exact. An amount
  with more digits than the token has (such as `0.1 + 0.2` of USDC) is refused, never rounded. This
  form only works for tokens whose decimals are known, which means the aliased ones.
- **`{ baseUnits: 2_500_000n }`:** the token's smallest unit (wei, for ETH). This works for any
  token.

Another chain's tokens, or a token without an alias, can be given to the client along with the
chain:

```ts
const bruno = new Bruno({
  chain: 31337, // any chain your relay serves, by id
  url: "http://127.0.0.1:8000",
  tokens: [{
    symbol: "USDC",
    address: "0x…",
    decimals: 6,
    eip3009: { name: "USDC", version: "2" },
  }],
});
```

`eip3009` is the token's EIP-712 domain, its `name` and `version`. A token needs it to pay by a
signed transfer with `payAndSubmit`; x402 reads it from the relay's offer instead.

Every amount the client returns is a `Value`, which carries both forms: `baseUnits`, and `decimal`
(or `null` if the token's decimals aren't known).

## Pay over x402

```ts
import { Bruno, viemSigner } from "@mailam/bruno";
import { privateKeyToAccount } from "viem/accounts";

const account = privateKeyToAccount(process.env.KEY as `0x${string}`);
const bruno = new Bruno({ chain: "sepolia" });

const paid = await bruno.x402.payAndSend(
  { call: { to, data }, gas: 100_000n },
  viemSigner(account),
  { pay: [{ token: "USDC", max: { decimal: "2.5" } }] },
);
console.log(paid.transaction, paid.paid.decimal, paid.paid.token.symbol);
```

`pay` lists the tokens you're willing to pay in, in your order of preference. Each entry has:

- **`token`:** an alias or address.
- **`max`:** the most you'll pay, with the price and tip together.
- **`tip`** (optional): an amount on top of the price, which ranks the call ahead of others when the
  relay's queue is full.

`payAndSend` asks for the price, which comes back as a 402. It pays in the first entry the relay
offers whose price plus tip is within its `max`. If no entry qualifies, it throws `NoPayment` with
each entry's reason, before anything is signed. Otherwise it signs the token's
`transferWithAuthorization` for that amount, sends the call with it, and resolves once the
transaction is included. If the relay refuses the payment, it throws `PaymentRefused` with x402's
error code, and nothing is spent.

`bruno.x402.price(request)` gives the price in each offered token without paying.

> **Note: over x402, the call runs from Bruno's contract, not your account.** This path is for calls
> whose effect doesn't depend on who sends them: a signed authorization (a USDC
> `transferWithAuthorization`, an EIP-2612 permit), a claim for a named recipient, or a mint for an
> address. For anything that has to act as your account, use the call-pays-for-itself path below.

A `Signer` is an address plus `signTypedData` over standard EIP-712 JSON (the `eth_signTypedData_v4`
shape, with numbers as decimal strings). `viemSigner` adapts a viem account. Any other signer works
too, such as a browser wallet:

```ts
const signer = {
  address,
  signTypedData: (typedData) =>
    window.ethereum.request({
      method: "eth_signTypedData_v4",
      params: [address, JSON.stringify(typedData)],
    }),
};
```

## Pay with a signed transfer

`payAndSubmit` pays for a call with a signed USDC transfer. Unlike x402, it's a normal submission,
so you get its id straight away and follow its status:

```ts
const { id, paid } = await bruno.payAndSubmit(
  { call: { to, data }, gas: 100_000n },
  viemSigner(account),
  { token: "USDC", max: { decimal: "2.5" } },
);
const status = await bruno.wait(id);
```

It asks for a quote with the signer as the payer, signs the transfer at that price if it's within
`max`, and submits the call with it. The submission must be included by the quote's
`validUntilBlock`, or up to `deadlineBlocks` blocks after it if you give that option. As over x402,
the call runs from Bruno's contract, not your account, and the transfer only happens if the call
succeeds and lands.

## Smart accounts

A smart account pays the same way, through x402 or `payAndSubmit`, whether it's deployed or not.
With viem:

```ts
import { toCoinbaseSmartAccount } from "viem/account-abstraction";

const smart = await toCoinbaseSmartAccount({ client, owners: [owner], version: "1.1" });
const paid = await bruno.x402.payAndSend(request, viemSigner(smart), {
  pay: [{ token: "USDC", max: { decimal: "2.5" } }],
});
```

While the account is undeployed, viem wraps its signatures as ERC-6492 says, and Bruno deploys it in
the same transaction that takes its payment. The client tells Bruno who's paying and how the account
is deployed (from `getFactoryArgs`), so the first price includes both. Over x402, if a payment still
turns out short, it signs once more at the price Bruno asks for.

A signer that isn't viem's can do the same: give it `getFactoryArgs`, returning the account's
`factory` and `factoryData` while it's undeployed. If you know what your account's signature check
costs, pass it as `verifyGas` in the request; otherwise Bruno uses its own figure for the
[wallets it knows](https://docs.mailam.app/bruno/support/#smart-wallets), and the most it knows for
any other.

## The call pays for itself

Your account's call can pay Bruno's payee itself, in any token the relay takes. A typical call is a
batch your account signs through its own EIP-7702 delegate, ending with the payment. The client
never builds, signs or delegates your account: you bring the call, along with any authorization
you've signed.

```ts
const { payee } = await bruno.terms();
const quote = await bruno.quote(gas, callBytes, ["USDC", "ETH"]);
// if the call needs an account deployed, quote it with the deployment too:
// await bruno.quote(gas, callBytes, ["USDC"], undefined, { deployment })
const [price] = quote.prices; // a Value: price.baseUnits, price.decimal
// ...build and sign your account's call, paying `price` of USDC to `payee`...
// and add `deployment: { account, factory, factoryData }` here
const id = await bruno.submit({
  call,
  gas,
  token: "USDC",
  amount: { baseUnits: price.baseUnits },
  maxBlockNumber: quote.validUntilBlock,
});
const status = await bruno.wait(id); // "done" or "failed", with its transaction
```

## Links

- [Bruno](https://bruno.mailam.app): the relay, its API, and the interface it implements
- [@mailam/bruno-agentkit](https://jsr.io/@mailam/bruno-agentkit): Bruno for Coinbase's AgentKit
- [Source](https://github.com/mai-lam-labs/bruno)
