# @mailam/bruno

A client for [Bruno](https://bruno.mailam.app), the permissionless gas relay. You can send a call on
Ethereum, or several in order, without holding ETH. Bruno is paid in ETH or a token as they're made,
and only if they succeed and land on-chain.

- **Post-paid and atomic:** your calls and Bruno's payment land together, in one transaction or one
  bundle. If a call reverts, or they never get included, you pay nothing.
- **Three ways to pay:**
  - **The call pays for itself:** in any token the relay takes, from your own account's signed
    batch.
  - **A signed transfer:** a USDC transfer that any signer of typed data signs, which the relay
    makes right after your calls.
  - **Over [x402](https://x402.org):** the same signed transfer, paid the x402 way.
- **Your own transactions:** an account holding a token and no ETH sends transactions of its own.
  Bruno sends it the ETH they need and is paid in the token, in one bundle.
- **Smart accounts:** a smart account can pay before it's deployed. Bruno deploys it in the same
  transaction (ERC-6492), so it never needs ETH.
- **No API key, no dependencies:** tested on Deno, Node 20 or later, and Bun. It only uses
  web-standard APIs, so it's meant to run in browsers and Cloudflare Workers too, but isn't tested
  there yet.
- **Chains:** Ethereum mainnet (`"ethereum"`) and Sepolia (`"sepolia"`).

## Install

From npm, or from JSR:

```sh
npm install @mailam/bruno    # npm
pnpm add @mailam/bruno       # pnpm
bun add @mailam/bruno        # Bun
deno add jsr:@mailam/bruno   # Deno, from JSR
```

Its versions are prereleases (`-dev`) until its API settles. On JSR, for npm, pnpm and Bun:
`npx jsr add @mailam/bruno`, `pnpm i jsr:@mailam/bruno`, `bunx jsr add @mailam/bruno`.

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

`eip3009` is the token's EIP-712 domain, its `name` and `version`. Without it, `payAndSubmit` takes
the domain from the transfer the relay's quote hands it, and x402 from the relay's offer.

Every amount the client returns is a `Value`, which carries both forms: `baseUnits`, and `decimal`
(or `null` if the token's decimals aren't known).

## Pay over x402

```ts
import { Bruno, viemSigner } from "@mailam/bruno";
import { privateKeyToAccount } from "viem/accounts";

const account = privateKeyToAccount(process.env.KEY as `0x${string}`);
const bruno = new Bruno({ chain: "sepolia" });

const paid = await bruno.x402.payAndSend(
  { calls: [{ to, data }] },
  viemSigner(account),
  { pay: [{ token: "USDC", max: { decimal: "2.5" } }] },
);
console.log(paid.transaction, paid.paid.decimal, paid.paid.token.symbol);
```

`pay` lists the tokens you're willing to pay in, in your order of preference. Each entry has:

- **`token`:** an alias or address.
- **`max`:** the most you'll pay, with the price and tip together.
- **`tip`** (optional): an amount on top of the price, which ranks your calls ahead of others' when
  the relay's queue is full.

`payAndSend` asks for the price, which comes back as a 402. It pays in the first entry the relay
offers whose price plus tip is within its `max`. If no entry qualifies, it throws `NoPayment` with
each entry's reason, before anything is signed. Otherwise it signs the token's
`transferWithAuthorization` for that amount, sends the calls with it, and resolves once the
transaction is included. If the relay refuses the payment, it throws `PaymentRefused` with x402's
`errorReason`, and nothing is spent.

`bruno.x402.price(request)` gives the price in each offered token without paying.

> **Note: over x402, your calls run from Bruno's contract or account, not yours.** This path is for
> calls whose effect doesn't depend on who sends them: a signed authorization (a USDC
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

`payAndSubmit` pays for your calls with a signed USDC transfer. Unlike x402, it's a normal
submission, so you get its id straight away and follow its status:

```ts
const { id, paid } = await bruno.payAndSubmit(
  { calls: [{ to, data }] },
  viemSigner(account),
  { token: "USDC", max: { decimal: "2.5" } },
);
const status = await bruno.wait(id);
```

It asks for a quote with the signer as the payer, signs the transfer the quote hands it if that's
within `max`, and submits the calls with it. Before signing, it checks the transfer is the payment:
for this token and chain, from the signer, to the payee, of the price quoted. If it isn't, it throws
a `TypeError` naming the field, having signed and sent nothing. The relay estimates a call's gas for
the quote, running it as it will run; give a call its `gas` in the request to set it yourself. Of
several calls, only one may leave its `gas` out. If the price has risen by the time it's submitted,
the relay says what it requires, and the transfer is signed once more at that, if it's still within
`max`. The submission must be included by the quote's `validUntilBlock`, or up to `deadlineBlocks`
blocks after it if you give that option. As over x402, your calls run from Bruno's contract or
account, not yours, and the transfer only happens if they all succeed and land.

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
// a draft of the call, paying the most you'd pay: the relay runs it to estimate its gas
const quote = await bruno.quote({ calls: [draft], authorizationList }, ["USDC", "ETH"]);
// if the call needs an account deployed, quote it with the deployment too:
// await bruno.quote({ calls: [draft] }, ["USDC"], { deployment })
const [price] = quote.prices; // a Value: price.baseUnits, price.decimal
// where it goes: quote.breakdowns[0] is { setup, call, value, payment, relay }, each { gas, amount }
// ...build and sign your account's call, paying `price` of USDC to `payee`...
// and add `deployment: { account, factory, factoryData }` here
const id = await bruno.submit({
  // the call given the gas the quote priced it for: quote.calls has each call's, in order
  calls: quote.calls.map(({ gas }) => ({ ...call, gas })),
  authorizationList,
  token: "USDC",
  amount: { baseUnits: price.baseUnits },
  maxBlockNumber: quote.validUntilBlock,
});
const status = await bruno.wait(id); // "done" or "failed", with its transaction
```

## Sponsored transactions

An account that holds a token and no ETH can send transactions of its own: Bruno sends it the ETH
they need, and is paid in the token by a transfer the account signs after them. They're the
account's own transactions, so each call is made by the account itself, with no delegate and no
smart account. The relay sends its transactions and the account's to block builders as one bundle,
for a builder to land whole or not at all.

The account's wallet has to sign a transaction without sending it. A viem local account does
(`privateKeyToAccount`, `mnemonicToAccount`); a wallet client's JSON-RPC account sends what it
signs, and won't.

```ts
import { Bruno, viemTransactionSigner } from "@mailam/bruno";
import { privateKeyToAccount } from "viem/accounts";

const bruno = new Bruno({ chain: "sepolia" });
const account = privateKeyToAccount(process.env.PRIVATE_KEY as `0x${string}`);

// each call is one transaction of the account's, in order
const calls = [{ to: target, data }];
const { id, paid } = await bruno.sponsor(calls, viemTransactionSigner(account), {
  token: "USDC",
  max: { decimal: "5" }, // refused, nothing signed, if the price is over this
});
const status = await bruno.wait(id); // "done" or "failed", with its transaction
console.log(paid.decimal, status.transactionHashes);
```

`sponsor` asks the relay what the calls cost, checks that what it's given to sign is the calls you
gave and a transfer of the price to the relay's forwarder and nothing else, has the wallet sign
each, and submits them. Pay in a token the account holds: the relay sends the account ETH for its
transactions' gas, but not what it pays with, so paying in ETH needs the account to hold the price
in ETH already.

A call's `gas` is yours to give. Left out, the relay fills in what the call used in a simulation of
your calls in order, and a tenth more. That's a measurement of one run, not an estimate of the limit
the call needs, and it suits a transfer or an approval. A call that earns a gas refund, or calls
deep into other contracts, can need well over what it uses, and then fails when it's submitted,
refused with nothing sent. Give `gas` on such a call: your node's `eth_estimateGas`, or for a call
that depends on an earlier one (a swap after its approval), which can't be estimated before the
earlier one has happened, a limit you know is enough. Gas a call doesn't burn stays in the account
as ETH.

### Quote, sign and submit yourself

To see the price first, or to sign somewhere else, do `sponsor`'s three steps one at a time:

```ts
import { checkedSponsored } from "@mailam/bruno";

// 1. what the calls cost, and what to sign
const quote = await bruno.quoteSponsorship(account.address, calls, ["USDC"]);
const price = quote.prices[0]; // in USDC, the token asked for

// 2. check it's the calls given and a payment of the price, then sign without sending
const usdc = bruno.token("USDC");
const toSign = checkedSponsored(quote, calls, bruno.chainId, usdc.address);
const signer = viemTransactionSigner(account);
const transactions = [];
for (const transaction of toSign) transactions.push(await signer.signTransaction(transaction));

// 3. submit: the account's transactions as signed, the payment last
const id = await bruno.submitSponsorship({
  account: account.address,
  transactions,
  token: "USDC",
  amount: { baseUnits: price.baseUnits },
  forwarder: quote.forwarder,
  maxBlockNumber: quote.maxBlockNumber,
});
```

`quote.transactions` are your calls filled in to sign, and `quote.payments` the payment in each
token you asked about, in the same order as `quote.prices`. `checkedSponsored` returns the
transactions to sign, the payment last, and throws if the quote isn't what you asked for; its last
argument is the address of the token you mean to pay in.

### An account whose code pays

A sponsorship can also have the relay make calls for the account, before any transaction of its own:
`calls`, as a submission's, made in order. An account with code, or one being delegated by an
EIP-7702 authorization, can pay from such a call into its code, with no transaction of its own. Give
the quote no transactions, and instead the authorizations the account signed and the call to make
into it. The call must pay the relay's forwarder (`(await bruno.terms()).forwarder`) the price, so
quote with a call paying one base unit, then sign the call again paying `quote.prices[0]`, and give
it the gas the quote answered for it:

```ts
const quote = await bruno.quoteSponsorship(account.address, [], ["USDC"], {
  authorizationList: [authorization],
  calls: [batchPaying(1n)], // { to: account.address, data }: your delegate's signed batch
});
const price = quote.prices[0];
const id = await bruno.submitSponsorship({
  account: account.address,
  authorizationList: [authorization],
  calls: [{ ...batchPaying(price.baseUnits), gas: quote.calls[0].gas }],
  token: "USDC",
  amount: { baseUnits: price.baseUnits },
  forwarder: quote.forwarder,
  maxBlockNumber: quote.maxBlockNumber,
});
```

Here `quote.payments` is empty: there's nothing more to sign. The relay delegates the account, makes
the call and collects the payment in two transactions of its own, and sends the account no ETH.

### Deadlines and status

A sponsorship lands by a block the quote fixes (`maxBlockNumber`). The furthest the relay takes is
the default; a nearer one costs less, since the account is funded for the fees a block may ask by
then, and `sponsor` refuses a quote that's to land later than the one you gave. The status's
`transactionHashes` are the sponsorship's transactions in order: the relay's first, the account's
own, and the relay's last, which collects the payment.

### What "whole or not at all" rests on

The account's transactions are ordinary signed transactions. Two things follow, which a smart
account's call, checked on chain, doesn't share:

- **A builder keeps the bundle whole, not the chain.** If a builder lands only part of it, the relay
  stops: the sponsorship is rejected (`reason: "bundle-broken"`), and the relay sends nothing more
  of it. It never sends your transactions again in pieces.
- **A signed transaction doesn't expire.** `maxBlockNumber` is when the relay stops sending, not
  when your signatures stop being valid. Whoever holds a signed transaction, the relay or a builder
  it was sent to, can land it later, for as long as its nonce is unused. To withdraw one, use its
  nonce: send any transaction of the account's at it.

So if your calls must not be left half done whatever a builder does, don't send them as separate
transactions: make them one call (a contract that does them together, or your account's own batch
through an EIP-7702 delegate).

## Refusals

Every refusal throws `BrunoError`. Its `reason` names the refusal, one of `REFUSALS` (the `Refusal`
type, exported): `underpaid`, `gas-over`, `signature-refused` and so on. Its `data` is the rest of
what the relay said, `field` and `most` for a field over its limit, or `required` and `token` for a
payment short of the price, and `code` the JSON-RPC code. Tell refusals apart by `reason`, not by
the message, which can change. A submission's `status.reason`, once it's rejected, is one of
`REJECTIONS`.

```ts
try {
  await bruno.quote({ calls: [{ ...call, gas: 50_000_000n }] }, ["USDC"]);
} catch (e) {
  if (e instanceof BrunoError && e.reason === "gas-over") console.log("at most", e.data?.most);
}
```

## Links

- [Bruno](https://bruno.mailam.app): the relay, its API, and the interface it implements
- [Source](https://github.com/mai-lam-labs/bruno)

## License

Apache-2.0. Copyright mai-lam-labs.
