---
description: "The runner's gas reserve, gas limits computed or estimated, pricing before a payment is signed, smart wallets' checks, per-fork schedules, and limits"
---

# Gas

**The runner never runs out before its payment check.** The call gets `GAS − R`, *R* being
the most everything after it can take: the opcodes to the end, memory, and the second
`balanceOf` with what EIP-150's 63/64 rule needs to give it its cap. *R* is summed as the code
is emitted, each piece carrying its own worst case, and pushed as a constant. `GAS < R`
reverts `ERCNNNN_GasExhausted()`, and the subtraction uses the same `GAS` reading as the
check, so it can't wrap. A call that burns everything it's given still leaves the check
enough.

**A transaction's gas limit is computed, unless the client chooses code that runs before the
call.** From the call's gas: the runner's own, the call's, 1/63 more for the call's CALL, then
at CREATE2 64/63 of the runner's frame, then the deployer's own, the authorizations', and the
intrinsic gas or the calldata floor, whichever is higher. `eth_estimateGas` would measure one
execution path at one state; the bound holds for every path the client's gas allows.

**A deployment, or a contract's signature check, is estimated.** Their cost is code the client
chooses, which the relay can't bound. Those steps get the gas left over, after reserving what
the rest of the runner needs and the call's declared gas. The transaction's gas limit is then
one `eth_estimateGas` of it as it will be sent, with no fees, from the relay's account. Because
the runner refuses to give the call less than its declared gas, the estimate's search can't go
below it, so the estimate covers every path the call's gas allows. An estimate that reverts is
the submission's simulation failing, with the revert data. Checked on anvil at both forks:
`eth_estimateGas` copes with the runner's self-destruct, and its estimate is the exact minimum.

**Before a payment is signed, its price can't be estimated,** since the signature isn't there
yet. A quote, or a first 402, that declares a deployment adds the factory call's own estimate.
One whose payer is a contract adds its signature check's gas, from `verifyGas` if the client
gives it, else from the wallet gas schedule for the deployment's factory, else the schedule's
largest figure. Once signed, the estimate decides: a payment that falls short is refused with
the amount it needs (402 at that price over x402), and the client signs again.

**The wallet gas schedule** lists, for each smart wallet factory the relay knows, what its
wallet's signature check adds to a transfer, per fork, measured in the EVM rather than taken
from receipts or estimates, which aren't exact. Its figures, and how each is measured, are in
`wallets.py`; which wallets it has is on the [support page](../../support.md#smart-wallets).

**Any factory.** Bruno keeps no allowlist of factories. The factory call runs inside the
runner, which is new for each transaction and holds nothing, and is atomic with the payment:
a factory can do whatever it likes, as the client's own call can. A submission deploys at most
one account.

**Per-fork schedules, the fork measured.** The figures that differ between forks are kept per
fork, each checked exactly against receipts on an anvil at that fork. Which is in force is
measured, never taken from activation times: a plain call to an account without code costs
the fork's base transaction cost (21,000 on Prague, 15,000 on Amsterdam), which nodes estimate
exactly. A cost no schedule predicts is a fork the code doesn't know: the chain is unavailable
rather than mispriced. It can't tell a fork that keeps the base cost; a schedule then wrong
elsewhere fails simulation, which costs nothing. Each fork's figures, and the EIPs behind
them, are in `forks.py`.

**What a submission costs on top of its call** comes mostly from the runner's CREATE2 and
the authorization. On Amsterdam: about 230,000 gas without an authorization, about 285,000
with one from an existing account, about 470,000 from an empty one (a client holding only
tokens). On Prague: about 70,000 to 100,000. A fresh address per transaction is what keeps
grants to the runner from outliving it; it is the dominant cost.

**A key's signed transfer's gas is bounded before any authorization exists,** for a quote or
a 402: a zero-value `transferWithAuthorization`, signed by a key made for it with a random
nonce, is measured against the live token as the chain opens (its signature check, its nonce's
new slot, the balances read and written back), then the two balance writes it skipped are
added at the fork's price: the payer's changing, and the payee's set from zero, or changing if
it holds some. The runner gives a key's transfer exactly that: a bound too low reverts, and
costs nothing. A contract's adds its signature check (the wallet gas schedule, above). Every
authorization's nonce is a new slot, priced as state growth on Amsterdam: about 150,000 gas a
transfer there, about 60,000 on Prague.

**Limits:** initcode at most 49,152 bytes (EIP-3860), so the call's data at most that less
the runner's code; `max_gas` and `max_call_bytes` per chain.
