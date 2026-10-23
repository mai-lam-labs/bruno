# Plan: x402, settled first (`upfront`)

A second x402 flow beside the atomic one: the payment is settled on its own, first, and the
call is sent after. Once it settles, Bruno is paid; whether the call then succeeds, reverts or
lands at all is the client's risk, not Bruno's. So nothing here needs the bundler guarantee:
the call can go out any way the chain takes transactions. Bruno settles the payment itself,
on the call's chain, as it does now; a facilitator can settle instead where that buys a Bazaar
listing, never as a dependency.

Normative text: x402 v2 (`scratchpad/x402spec/`, coinbase/x402 at `0e6e8c5`): §6.1 Payment
Flow Models, and `exact`'s text on `upfront` (`scheme_exact.md`).

## What x402 says

- **`upfront`** is a defined flow: settle → resource → respond. "Payment is durably committed
  before the resource executes, giving the server finality first. Facilitator `/verify` is not
  part of this ordering; validity is established by settle."
- **`exact` MAY use it** "when the resource needs payment finality before execution". The
  client-signed EIP-3009 authorization is in the family that may use `authorization` or
  `upfront`, the mechanism declaring which.
- **Declared, not assumed:** when the flow isn't `authorization`, `accepts[].extra.paymentFlow`
  MUST be present. Clients MUST NOT pay a flow they don't recognize, and SHOULD prefer
  `authorization` where both are offered.
- **No refunds:** "a handler failure leaves the client charged with nothing delivered; this
  specification defines no refund."

## Decisions (proposed)

- **The resource is the submission, not its inclusion.** Settle → Bruno accepts the call
  and starts sending it → respond `200`, `PAYMENT-RESPONSE` the settlement's transaction, and
  the submission's status URL. The client follows the call there (as REST `status`, and MCP's
  `status`/Tasks). Nothing is held past the settlement's own inclusion.
- **Paid means sent until included or expired.** Bruno's obligation after settling is to
  send the call, priced at the quote, until it's included (succeeding or reverting) or its
  deadline passes. A reverting call is `done`, reverted; the status says so. A call that
  expires unsent is the one way Bruno keeps a payment for nothing, so its deadline is the
  client's and the quote prices inclusion within it.
- **Offered beside atomic, never instead.** On chains with the bundler guarantee both flows
  are listed, `authorization` first, as x402 asks; elsewhere `upfront` alone.
- **Paid on the call's chain, settled by Bruno.** The payment settles on the chain the call
  runs on, by Bruno's own 3009 settlement (a plain transaction, not a runner), to that chain's
  payee. Each chain's treasury then converts its own income to its own float's gas: the
  self-replenishing model holds, per chain, with no cross-chain treasury. Every chain Bruno
  serves has a treasury (pools, router, USDC), so every chain it serves can settle itself.
- **Facilitators are optional and pluggable, never required.** A facilitator may settle on
  a network Bruno also runs on, configured per network, for what it buys: a Bazaar listing.
  `payTo` is always Bruno's own payee, so the settler is interchangeable: dropping a
  facilitator (its pricing, its terms, its going away) falls back to Bruno's own settlement
  with nothing for clients to change. No facilitator is a dependency of any flow.
- **The settlement is the only thing at risk, and it's small.** Bruno sends the call only
  once the settlement is included, so nothing Bruno spends on the call is ever unpaid. What
  remains exposed is the settlement itself: one `transferWithAuthorization`, little calldata,
  a transfer's gas, checked by simulation before it's sent. A settlement that fails anyway (a
  nonce spent or funds moved in between) costs that, and nothing more: a far smaller griefing
  surface than an unpaid call carrying arbitrary calldata and gas.
- **Optimistic where gas is cheap, guaranteed where it's offered.** On L2s the settlement goes
  out as a plain transaction: a failure costs a fraction of a cent. Where builders drop
  reverting transactions (Ethereum, Sepolia, Unichain), the settlement goes through them too,
  so a failed settlement costs nothing; and there the atomic flow stays on offer beside it.
- **Prices in USDC, quoted per call.** As now: the call's measured gas at the chain's price,
  plus the margin; `upfront` adds the settlement's own cost where Bruno settles itself.
- **x402 over MCP follows:** the settlement's hash exists at once, so a paid MCP tool
  needn't hold (`v0X0-MCP.md`).

## Facilitators (research, 2026-10-23; to confirm before building)

- **CDP facilitator:** networks Base, Base Sepolia, Polygon, Arbitrum, Monad, World, World
  Sepolia (and Solana); `exact`, `upto`, `batch-settlement` on EVM. **Not Ethereum**, which
  the separate-network decision is for. Authenticated with a CDP API key (a JWT per request):
  a key on Bruno's side, the operator's, not the client's.
- **Bazaar listing** is automatic: a public HTTPS endpoint and one successful paid call
  settled through CDP's facilitator, with the `bazaar` extension declared. An open report
  (cdp-sdk #833) has a settled Base mainnet payment never indexed, v2 metadata dropped:
  unconfirmed.
- Other facilitators (x402.org's testnet one, third parties): to survey, by network, auth and
  whether they're indexed. Many exist (docs.x402.org lists ~17); any that indexes into a
  Bazaar can be configured the same way, so no single one is load-bearing.

## Longevity (discussed 2026-10-23)

- **CDP's terms:** 1,000 settlements a month free, then $0.001 each, behind a CDP API key.
  Used only where a listing is worth it, and only on networks Bruno also settles itself.
- **Cross-chain payment** (pay on one chain, run on another) is a permissionless take on
  Gelato's 1Balance, and it breaks self-replenishment: income piles up on the paying chain
  while the float drains on the executing one. It needs a cross-chain treasury (CCTP to move
  USDC, a swap at the far end, the price movement between), designed properly, not hastily.
  Deferred; not part of the first slices.
- **And it costs the user more:** everything a same-chain call costs, plus bridge or CCTP fees
  and gas at both ends, a second swap's slippage, price risk until the float is refilled, and
  capital idle on one chain while gas is pre-positioned on the other, all carried in the
  margin. A price-sensitive user bridges once and pays locally; what remains is a convenience
  market paying a premium. Same-chain is the product's own logic: pay with what you hold,
  where you are.
- **Decided (2026-10-23): same-chain only.** The existing treasury loop keeps working as it
  is. Cross-chain gas waits for "teh borg" (the author's separate project: moving gas from one
  chain to another behind a simple interface), plugged in once it's ready and deployed,
  rather than a cross-chain treasury built here.

Sources: docs.cdp.coinbase.com/x402/seller/facilitator, /x402/support/faq,
/api-reference/v2/rest-api/x402-facilitator/get-supported-payment-schemes-and-networks;
github.com/coinbase/cdp-sdk/issues/833.

## Open questions

1. **Which facilitator, if any, for a listing:** CDP (a key; Base, Polygon, Arbitrum…) or
   another that indexes into a Bazaar; on which networks, once Bruno executes there.
2. **Endpoint:** the same `/{chain}/v1/x402` listing both flows, or `upfront` on its own
   path? Default: the same, `extra.paymentFlow` telling them apart.
3. **The call's deadline** under `upfront`: the client's (`maxBlockNumber`-like), or Bruno's
   fixed window from settlement?
4. **Cross-chain payment:** deferred to "teh borg"; what interface Bruno's treasury would call
   it through, once it exists.
5. **Refusing before settling:** Bruno still measures the call before settling, so a call
   that can't run is refused unpaid; a call that reverts later (state moved) is paid. Stated
   in the 402's description?
