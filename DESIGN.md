# Bruno: design

The decisions behind Bruno's implementation of SPEC.md, and the limits they work within.
Running it: DEPLOYMENT.md.

## On-chain

```
from:  concurrency account i
to:    deployer
data:  runner initcode (code ‖ the call's data)
type:  4 with the client's authorizations, else 2

deployer: CREATE2(0, initcode, keccak256(caller ‖ block number))
runner:   balance before → call → balance after ≥ before + amount, else revert → RETURN(0, 0)
```

### Deployer

`0x48F47C9c5FC7bb594e31172a3FdEC10D8c51144c` on every chain: deployed through Arachnid's
deterministic deployer (`0x4e59b44847b379578588920cA78FbF26c0B4956C`) with salt 0. Ownerless,
no storage. Calldata is a runner's initcode. It:
- **salts with `keccak256(caller ‖ block number)`**: a runner's address belongs to one
  caller in one block, so no one else can deploy at it, and it can't be reused later;
- **returns** the runner's address, ABI-encoded;
- **reverts with the runner's revert data**, the failed CREATE2's return data, so `eth_call`,
  `eth_estimateGas` or a builder's simulation of the transaction reports why it fails. Empty
  after an address collision.

```
365f5f3733365243602036015260403620365f5ff5806020573d5f5f3e3d5ffd5b5f5260205ff3

 0  CALLDATASIZE PUSH0 PUSH0 CALLDATACOPY        memory[0:n] = initcode
 4  CALLER CALLDATASIZE MSTORE                   memory[n:n+32] = caller
 7  NUMBER PUSH1 32 CALLDATASIZE ADD MSTORE      memory[n+32:n+64] = block number
13  PUSH1 64 CALLDATASIZE KECCAK256              salt
17  CALLDATASIZE PUSH0 PUSH0 CREATE2
21  DUP1 PUSH1 32 JUMPI
25  RETURNDATASIZE PUSH0 PUSH0 RETURNDATACOPY RETURNDATASIZE PUSH0 REVERT
32  JUMPDEST PUSH0 MSTORE PUSH1 32 PUSH0 RETURN
```

Its initcode: `PUSH1 39 DUP1 PUSH1 9 PUSH0 CODECOPY PUSH0 RETURN`, then the runtime above.

### Runner

1. **Read the payee's balance:** `BALANCE` for ETH; for a token, `balanceOf(payee)` by
   `STATICCALL`, anything but success with 32 bytes reverting `ERCNNNN_BalanceUnreadable()`.
   Paid over x402, **make the transfer:** the client's `transferWithAuthorization` to the
   token, given exactly its bound (Gas, below); one that fails reverts the runner with the
   token's revert data.
2. **Make the call** with all but a reserve *R* of the gas left (Gas, below). Less than *R*
   left reverts `ERCNNNN_GasExhausted()`. A call that fails reverts the runner with the
   call's own revert data.
3. **Read the balance again:** `after < before`, or `after − before < amount`, reverts
   `ERCNNNN_Underpaid()`.
4. **Return no code:** the runner's address ends with a nonce of 1 and no code, so nothing
   granted to it outlives the transaction.

**The runner is generated per submission, not deployed once.** Its payee, token, amount and
call are constants in its code, so it reads no calldata and trusts no input at run time.
The call's data is appended after the code and `CODECOPY`'d to memory.

**One call.** A client batches through its own delegate, which orders, authorizes and
replay-protects its calls by its own signature. A runner making several calls would add
ordering the relay controls and the client can't verify.

**The payment is a balance difference, read twice:** `BALANCE` for ETH (ERC-7528's address
in the interface), `balanceOf` for a token. `after < before` is checked before `SUB`, so a
payee losing funds in the call can't wrap to a large gain. Paying and then taking back is
underpaid. Any token whose `balanceOf` answers 32 bytes works; USDT's missing return
value on `transfer` doesn't matter. `balanceOf` is called with exactly its measured gas, so a
token needing more reads as `ERCNNNN_BalanceUnreadable()` and the runner reverts: a wrong
bound costs nothing.

**A failed call reverts with the call's own revert data,** passed up by the deployer, so a
simulation shows the client exactly why.

**Why our own deployer, not Arachnid's directly:** the salt binds the caller and block, so no
one else can deploy at a runner's address, and it can't be reused later; and the runner's
revert data reaches the transaction's output.

## Bundling

A submission's transaction is sent only through `eth_sendBundle`, in one of two modes per
builder:

- **Standalone:** each transaction is its own bundle. A reverting transaction fails its
  bundle and isn't included. Any builder that takes bundles.
- **Dropping:** one bundle a block, every transaction listed in `droppingTxHashes`. A
  reverting transaction is dropped and the rest land. A builder that honours
  `droppingTxHashes`.

**Every builder gets every transaction,** in its own mode. Each comes from its own account at
that account's next nonce, so it lands at most once, whichever builder wins the block; the next
block's are signed afresh. Bundles are signed (`X-Flashbots-Signature`) by the float, unless a
builder has an identity key of its own.

`version` is unset and `revertingTxHashes` never used. The rule relied on (rbuilder
`order_commit.rs` @ `6037fa7`, 698–732, 824–878): a reverting or invalid transaction in
`droppingTxHashes` is skipped; one listed nowhere fails the bundle; a failed bundle is rolled
back whole.

**A builder serving the interface itself** needn't use the deployer or runner: it can include
the call directly, from its own account, only where the block's state shows the payee paid.
That skips the runner's CREATE2, the bulk of the overhead.

## Gas

**The runner never runs out before its payment check.** The call gets `GAS − R`, *R* being
the most everything after it can take: the opcodes to the end, memory, and the second
`balanceOf` with what EIP-150's 63/64 rule needs to give it its cap. *R* is summed as the code
is emitted, each piece carrying its own worst case, and pushed as a constant. `GAS < R`
reverts `ERCNNNN_GasExhausted()`, and the subtraction uses the same `GAS` reading as the
check, so it can't wrap. A call that burns everything it's given still leaves the check
enough.

**A transaction's gas limit is computed, never estimated.** From the call's gas: the runner's
own, the call's, 1/63 more for the call's CALL, then at CREATE2 64/63 of the runner's frame,
then the deployer's own, the authorizations', and the intrinsic gas or the calldata floor,
whichever is higher. `eth_estimateGas` would measure one execution path at one state; the
bound holds for every path the client's gas allows.

**Per-fork schedules, the fork measured.** The figures that differ between forks are kept per
fork, each checked exactly against receipts on an anvil at that fork. Which is in force is
measured, never taken from activation times: a plain call to an account without code costs
the fork's base transaction cost (21,000 on Prague, 15,000 on Amsterdam), which nodes estimate
exactly. A cost no schedule predicts is a fork the code doesn't know: the chain is unavailable
rather than mispriced. It can't tell a fork that keeps the base cost; a schedule then wrong
elsewhere fails simulation, which costs nothing.

| | Prague | Amsterdam |
|---|---|---|
| Cold account access | 2,600 | 3,000 |
| `CALL` value | 9,000 | 11,300 |
| New account: `CALL` / transaction | 25,000 / 0 | 183,600 / 183,600 |
| `CREATE2` / creation tx, empty target | 32,000 | 195,600 |
| Tx base / recipient / value | 21,000 / 0 / 0 | 12,000 / 3,000 / 6,000 |
| Calldata floor, zero / nonzero byte | 10 / 40 | 64 / 64 |
| Authorization: intrinsic / new delegation / empty authority | 25,000 / 0 / 0 | 7,816 / 44,190 / 183,600 |

Amsterdam: EIP-2780, 7976, 8037, 8038. Sepolia runs it from 1791294816, Hoodi from 1793036568.

**What a submission costs on top of its call** comes mostly from the runner's CREATE2 and
the authorization. On Amsterdam: about 230,000 gas without an authorization, about 285,000
with one from an existing account, about 470,000 from an empty one (a client holding only
tokens). On Prague: about 70,000 to 100,000. A fresh address per transaction is what keeps
grants to the runner from outliving it; it is the dominant cost.

**An x402 transfer's gas is bounded before any authorization exists,** for the challenge's
price: a zero-value `transferWithAuthorization`, signed by a key made for it with a random
nonce, is measured against the live token as the chain opens (its signature check, its nonce's
new slot, the balances read and written back), then the two balance writes it skipped are
added at the fork's price: the payer's changing, and the payee's set from zero, or changing if
it holds some. The runner gives the transfer exactly that: a bound too low reverts, and costs
nothing. Every authorization's nonce is a new slot, priced as state growth on Amsterdam: about
150,000 gas a transfer there, about 60,000 on Prague.

| Over the zero-value transfer | Prague | Amsterdam |
|---|---|---|
| A slot changing | 2,800 | 10,000 |
| A slot set from zero | 19,900 | 107,920 |

**Limits:** initcode at most 49,152 bytes (EIP-3860), so the call's data at most that less
the runner's code; `max_gas` and `max_call_bytes` per chain.

## Accounts

- **Float:** the instance's one secret. It funds the concurrency accounts, which are derived
  from its key (BIP-32, the key as seed, `m/44'/60'/0'/0/i`): one key to hold, any number of
  accounts.
- **Concurrency accounts:** each sends at most one transaction a block, at its own nonce, so
  submissions in the same block don't contend for one nonce.
- **Payee:** separate from the float. Payments accrue where the hot key isn't; converting
  them to ETH is another process's job.

**One user of the float's nonce.** The float sends only funding transactions, one at a time.
A funding transaction in a bundle as well would race it for the same nonce, so bundles never
carry funding, in either mode.

**No dust.** Draining an account uses a type-1 transaction whose gas limit is exactly what a
transfer uses and whose value is the balance less gas × gas price, leaving 0. An account
holding less than its drain's gas is an error, not a remainder.

## Queue and batches

**Submissions never hold an account.** They wait in a queue; a batch maps some onto accounts
for one batch interval; the next batch maps afresh. Bundles target a single block, so a
transaction signed for a block that's passed needs no cancelling, and the next is signed at
whatever the account's nonce is.

**Ranked by profit a transaction, not per gas.** What's scarce is accounts, one transaction
each a block, not block gas. Profit: the amount in wei at the pools' price, less the
transaction's gas limit × the next block's fee.

**A bounded queue.** At most `max_pending`; a full queue takes a submission only by evicting
its least profitable, and otherwise answers with the amount that would get in. Deadlines are
at most `max_deadline_blocks` ahead. Nothing waits indefinitely and nothing grows without
bound.

**Simulated again before every block.** A client can make its submission fail after it's
taken: spend its tokens, use its signature elsewhere. Builders would drop it every block at
no cost to the client, holding an account until its deadline. Simulating before each block
rejects it within one.

**A higher payer waits at most one batch.** A batch's mapping isn't changed mid-batch:
funding was computed for it. A submission whose transaction is out is never evicted, since it
may land, nor rejected for failing simulation, since a call that has landed no longer
simulates; its latest four transactions are each looked for, past a node lagging a block.

**Bounded state.** A finished submission's status is kept `retention_seconds`, then
forgotten; revert data kept with a rejection is capped. Submissions are queued one at a time,
so a duplicate or a full queue is decided once, however many arrive together.

## Funding

**Per submission, with headroom.** A batch maps each submission to the lowest-numbered free
account that already covers its transaction at the fee ceiling; failing that, the
lowest-numbered free account, topped up to `funding_multiplier` × that cost. With headroom,
an account covers its next transactions without another funding round, and since the lowest
accounts are used first, those that stay funded follow the actual concurrency.

**Not waited for.** A batch's covered submissions go out at the next block; those awaiting
funding join once it lands. While a funding transaction is in flight, a batch leaves out what
needs funding.

**Priced to last its wait.** A funding transaction's max fee is the base fee risen as fast as
EIP-1559 allows for as many blocks as `funding_timeout_seconds` waits (ten, on Ethereum), so a
surge can't strand it before it's given up on; it pays only its block's base fee and the tip.
One the float can't cover at that max isn't sent: the next batch tries again, once the
treasury has topped the float up. The treasury's bundles are priced likewise, for the blocks
they're offered for.

**Bounded.** The accounts hold at most `accounts` × `funding_multiplier` × the largest
transaction's cost. A transaction can't spend more than its gas limit × max fee, and nothing
is signed unless its account covers that, so an account can't be overdrawn: one that falls
short sits out until topped up. Shortfalls cost capacity, not money.

## Treasury

**The float has to refill itself.** Payments go to the payee, in whatever token the client
chose; the float pays gas in ETH. Without something moving revenue back, the relay stops once
the float is spent. A treasury per chain does it, given the payee's key: the payee and the
float stay separate accounts so each has one user of its nonce, and more floats can be added.

**A run every `interval_seconds`, nothing in between.** A run reads the float, its accounts
and the payee in one batch, measures what the float and accounts spent since the last run
(what they held then, plus what the last run forwarded if it landed, less what they hold now),
and sends what's needed as one bundle. It waits for nothing: the next run reads the forward's
receipt. Idle, it costs a read every few hours.

**The target is a runway, with a floor.** The float and accounts are topped up to
`runway_days` of their measured spend, and to at least enough to fund every account once for
the largest transaction at the fee ceiling: what the relay's first full batch can draw, before
any spend is measured. Both follow fees.

**Through the payee.** Its ETH above its reserve goes first; then its WETH, unwrapped; then
its other tokens, each swapped along the pools that price it, through Uniswap's SwapRouter02
in one call: each run of v2 or v3 pools a leg, its output kept in the router, then unwrapped
and paid to the payee. The payee forwards the lot to the float. The minimum out is the pools'
price less the token's margin (its pools' cut) and `slippage_multiplier`.

**Gas measured, not assumed.** Every transaction's gas is measured with `eth_estimateGas` as
it will be sent, with a quarter's headroom: forks price gas differently (Amsterdam's state
creation costs many times Prague's), so no fixed figure fits. Measuring also refuses a swap
that would revert before anything is sent. A swap can only be measured once the router may
spend its token, so each token is approved ahead of its first swap, in a run that does only
that; the next run follows soon after, not a whole interval later.

**The payee keeps a reserve:** one run's gas at its max fee, × `funding_multiplier`, the gas
measured for the tokens it holds. Swaps pay
the payee inside the run's bundle, so the reserve only has to carry the run until they do;
conversions refill it. A payee below what a conversion's gas needs stalls, and the snapshot
says so: it needs ETH from outside.

**Converting must be worth it.** A conversion goes ahead only if its gas and its pools' cut
come to at most its value × (`max_cost_multiplier` − 1), 2% by default; converted, it's made
at least that large, up to what the payee holds, so a worthwhile conversion is made once
rather than in pieces. The rule is waived when the float is running dry: it can't cover a
transaction, or its measured spend outlasts it before the next run. A single swap of a few
dollars on Ethereum costs several percent in gas; the rule converts larger amounts less often.

**One bundle, all or nothing,** to every builder, for each of the next five blocks: a swap's
slippage reverts the bundle, and nothing is spent. A payee's transactions are never in the
public mempool.

**The page shows what was last read.** The root shows each chain's float and concurrency
accounts (each address linked to the network's explorer, its balance and nonce), and the
treasury's last run: the payee's holdings, the target and reserve, how many transactions of
`max_gas` the float covers, what it did. Viewing it reads nothing from the chain. The accounts
are as the relay last read them: every batch reads them all, the float with them, in the
request it makes anyway, and each treasury run reads them too; the later read wins. An idle
relay reads nothing, and nothing it holds changes but by its own transactions or the
treasury's, so the figures stay true; ETH sent to the float from outside shows at the next
read. On the Worker, the hourly cron that wakes a chain's object for its treasury reads them
again when the treasury isn't due, so they're an hour old at most; the object is otherwise
only woken to answer.

**A base unit of each x402 token stays with the payee.** Converted, the payee's balance of a
token would return to zero, and the next x402 transfer to it would set the slot again: 107,920
gas on Amsterdam, on every such submission's bound. Keeping one unit keeps the slot set; the
relay prices the new slot only while the payee holds none, as each treasury run tells it.

**Exposure is the float, not the payments.** Revenue waits as stablecoins until it's needed;
what's held in ETH is the float and the payee's reserve, a few days of gas.

## Measured, not assumed

**What the relay knows of a chain, it measures,** in one batch: its nodes' chain id, the fork,
the deployer's code, each token's `balanceOf` gas, decimals and symbol, each x402 token's
transfer, the payee's holdings. Nothing is read on start; the chain is measured when it's first
needed, and again once `measure_seconds` old (an hour) when next needed, so an idle relay reads
nothing, and a fork or a token's upgrade goes unnoticed at most that long. On the Worker, each
wake measures afresh.

**Degrade, never die.** A token that can't be measured is suspended and the rest served; one
whose x402 transfer fails is taken without x402. A chain whose nodes are another chain, whose
fork no schedule knows, or whose deployer isn't its runtime is unavailable: nothing is quoted,
taken or sent, and the treasury holds off, until it measures right. Nodes that don't answer
keep the last measurements, tried again a minute later; with none yet, the chain is
unavailable and its nodes aren't asked again for a minute. A node's errors stay in the log:
what clients and the root see names no URL. The root lists what's wrong.

**A simulation must deploy the runner:** the deployer answers its address, 32 bytes; any other
answer (an address without code answers nothing) means no runner ran and no payment was
checked, and fails the simulation.

**Releases are checked against the networks.** Before deploying, `make preflight` checks every
configured chain against its live nodes, read-only; after, `make <net>-verify` pays through the
deployed relay: a payment a unit short refused, one landed, one over x402. Its client's call is
its own `transferWithAuthorization` to the payee, so it needs no ETH and no delegation. Both
exist because Ethereum was served for two releases without the deployer: every check had run
on a local chain that installs it.

## Clock and reads

**Work only while there's work.** A relay's clock runs while a submission is pending or a
funding transaction is in flight: a submission taken starts it, and its batch closes at once.
With none, it stops; it wakes only to forget finished submissions after `retention_seconds`,
reading nothing, and a submission taken meanwhile starts it again at once, not when that
falls due. Quotes, status and the configuration are answered on request. An idle relay
reads nothing from its nodes.

**A step a slot.** Each step reads the latest block and the next base fee, in one request, and
prices and schedules all its work by them: the block's work if there's a new block, the batch
if it's due. The next step is at the next slot's start, its block looked for a second in, or
when the batch falls due, whichever is sooner. Nothing to do reads nothing more: a batch with
nothing queued, or nothing that still simulates, and a block with no batch to send. A step that
fails, its nodes not answering, is tried again a second later, then twice as long after each
failure in a row, up to five minutes.

**A slot's work is a few requests, whatever the queue.** Where each submission needs the same
read (its planning reads, its simulation, its receipts) or each account does (balance and
nonce), the reads go as one JSON-RPC batch, of at most 100 calls a request. A batch fails over
to the next node whole; a node's error answer to one call fails only that submission. Closing a
batch simulates every ranked submission at once, and those that no longer simulate are
rejected, rather than simulating down the ranking until the accounts are filled. Measuring a
chain is one request too (Measured, not assumed), as is a token's price (every pool on its
path), and a treasury's reads.

## Price

**The tip is the market's:** the larger of the median of the last 20 blocks' median tips and
the median of the last 5, so it follows a rise within a few blocks. No floor: the median is
what half of recent transactions paid, enough to be included by definition, even when it's 0.
No cap: a higher tip never costs the relay, since a submission whose amount doesn't cover the
block waits, and a cap would take the relay out of service under contention.

**Our fee is the tip again:** the relay earns what the builder does, for each gas. A fixed
margin over cost earns little when fees are low and too much when they're high; the tip
tracks how contended blocks are, which is what the service is worth. Floors keep it from
nothing when the market tips little: `min_fee_per_gas`, and `min_fee` on a transaction,
priced in a token (a cent of USDC). They protect the income, not inclusion, so they're on
our fee, never the builder's tip.

**Signed at exactly the next block's base fee** plus the tip: a bundle targets one block,
whose base fee is known from the latest. The price is that fee × the gas limit, and our fee, in
the token, × the margins. The chain's margin defaults to none; a token's covers converting
it to ETH.

**A quote allows for rising fees:** the next base fee risen by an eighth a block, EIP-1559's
most, through the quote's blocks, and twice the tip. Accounts are funded to that ceiling. A
quote is advisory; each block decides.

**Token prices from pools,** each pool's spot price along the token's path to WETH. ETH and
WETH need none: a wei is a wei. A spot
price can be moved within a block, but it's read from the latest block's state: moving it
means leaving the pool off-market at a block's end, open to arbitrage. Deep pools, and a
cache of `price_ttl_seconds`.

**Margins are multipliers,** string `Decimal`s, never basis points.

## x402

**Its own endpoint,** `POST /{chain}/v1/x402`, REST only: x402 v2 over HTTP, `exact` on EVM by
EIP-3009. The 402 is the quote and the signed authorization the acceptance: there's no quote
or submission step apart from them.

**The relay is the facilitator, atomically.** The authorization is spent by the runner, in the
transaction that makes the call (Runner), so it's spent only if the call succeeds and is
included; nothing is settled apart from the call. The payment is still a balance difference:
the transfer is how it arrives.

**The challenge:** an `exact` requirement in each token with `eip3009`, its amount the quote
for the call in the body (its gas, its length, its authority), `maxTimeoutSeconds` the furthest
deadline. Without a body, or with one that doesn't parse, it's a reference call's price (one
authorization from an existing account, 150,000 gas, 512 bytes): discovery's probes reach the
402. The latest block is read at most once a slot for it, however many ask.

**A payment is acknowledged, not waited for.** Taken, it's answered 200 at once:
`PAYMENT-RESPONSE` with `transaction` empty, since a submission's transaction is signed afresh
each block until one lands, and its status URL (`Location`, the body, and the response's
`status` extension). The client follows it there, or by the token's
`AuthorizationUsed(from, nonce)`. A paid request holds no connection and costs what a
submission does.

**A paid submission is an ordinary one** from there: queued, ranked, evicted, simulated each
block. Its deadline is the last block expected before `validBefore`, at most
`max_deadline_blocks` ahead; its id has the authorization in place of the deadline, so the same
payment sent again is the same submission, answered as it stands: its transaction once it has
landed, a 402 once it's been rejected or has expired. Refusals are 402s with x402's codes: an amount
under today's charge, or under what would get into a full queue, comes with a fresh
`PAYMENT-REQUIRED` at what's needed.

**The authorization doesn't bind the call.** x402 gives no field that could: anyone who sees it
(the relay, the builders) could attach it to another call, or settle it directly to the payee.
That exposure is x402's; the relay tracks no nonces beyond the simulation, which refuses a used
one.

**Discovery:** the 402 carries the `bazaar` extension (the body's schema and an example) and
the resource's description; `/.well-known/x402` lists each chain's endpoint; OpenAPI describes
it with `x-payment-info`. The catalogs list only Base (and Solana) today: CDP's from settlements
through its own facilitator, x402scan by probing.

## Risks

**A builder that includes a reverting transaction** (ignoring `droppingTxHashes`, or a
standalone bundle's failure) makes the relay pay its gas, unpaid. Builders are used only once
seen to drop; nothing is ever listed in `revertingTxHashes`.

**Grants to the caller.** Each runner is a fresh address, salted by its sender and block, left
with no code: approvals or roles a call grants its `msg.sender` are dead after the
transaction. The relay's own accounts are never the call's `msg.sender`.

**Denial of service.** Every submission taken costs simulations. The queue, deadlines, gas,
calldata and authorizations are bounded, and each authorization is range-checked (y_parity,
low s, its nonce) before any work; rate limiting belongs in front of the endpoint. One
submission failing (a drained pool, a node's bad answer) skips it, not its batch or block.

**What clients see.** An internal error is answered as one, its detail logged: a node's URL,
an API key in it, never reaches a response. A chain is reached by its canonical id or its
network's name, both the same chain; the Worker routes only a chain the configuration names,
by its id, so no request creates an object, or a second one for a chain.

**Keys.** The float's key is hot: it holds ETH for gas, bounded by the funding settings.
Payments accrue to the payee, whose key is held elsewhere.

## Testing

**Against real code, not models of it.** Contracts are fetched from mainnet (Calibur, USDC,
USDT, UNI, WETH9, Permit2, Uniswap's v2 and v3 pools, its position manager and SwapRouter02)
and installed on anvil at their mainnet addresses; runners are generated and executed
on-chain; gas is asserted exactly against receipts, on a Prague and an Amsterdam anvil. The
pools hold real liquidity, minted through their own code, so the treasury's swaps run through
Uniswap's router as they would on Ethereum: a v3 swap with its approval, and a v3 leg into a
v2 leg.

**A mock builder** applies rbuilder's commit rule to anvil, bundle by bundle, so both modes
are tested end to end. The live test sends standalone bundles to an ePBS builder on Sepolia.
The Python relay is served by uvicorn in the test's own event loop, as in production, so
nothing in a test drives it from a second loop.

**A burn-in** (`make burn-in`, minutes, not part of `make check`) leaves each relay to its own
clock on the fast chain for a hundred-odd blocks under a seeded random load: clients paying
in USDC, USDT or WETH, honest or underpaying, spending their tokens after submitting, with
deadlines a block away, submitting twice; base-fee shocks; the treasury living on the
revenue. Every block: none of the relay's transactions reverts on-chain, nothing answers with
a server error, nothing lands past its deadline; at the end, nothing is left pending and
neither relay logged an error. Its seed replays the load.

To come: an rbuilder devnet running the same scenarios; mainnet checks with a small float
and a deliberate non-payer; a log of every bundle sent.

## Cloudflare Workers

The relay ported to TypeScript (`worker/`). The Worker routes each request to its chain's
Durable Object, one a chain, which keeps the queue, the batch and the funding in flight in its
SQLite storage. Its alarms are the clock (Clock and reads): each step an alarm.

**Idle costs nothing.** An object with nothing queued holds no alarm, so it's evicted from
memory and billed nothing; a deployment serving a quiet chain costs only its requests.

**The treasury runs in the chain's object,** as the Python treasury holds its relay: an hourly
cron trigger calls each chain with a treasury, and its object runs it when its interval is up,
keeping the snapshot in its storage. A cron rather than an alarm: nothing has to wake the
object after a deploy for the treasury to start.

**Python stays the reference.** It writes golden vectors (runner initcode as its keccak256,
every gas figure, signed transactions, funding, bundles, RLP at its boundaries, QR codes) that
the port must reproduce exactly, and the documents and page the Worker serves: OpenRPC,
OpenAPI, and the root's page, whose QR codes the Python makes with segno and the port's own
encoder reproduces, mask choice included. The server tests run
against both: the Worker under workerd, against anvil and the mock builder served over HTTP,
its batches and blocks driven through test hooks that only a local build enables, or left to
its own alarms on a fast chain: a block every two seconds, built by the mock builder.

**A small supply chain.** Deno and workerd are single binaries pinned by sha256. The only
packages are `@noble/hashes`, `@noble/curves`, `@scure/bip32` and `@scure/base`, audited,
depending on nothing else, fetched from JSR and locked by integrity. No npm, no install
scripts, no Wrangler: the Workers runtime types the relay uses are declared in the port, and
the QR encoder is the port's own.

## Not yet

- **x402 beyond EIP-3009:** Permit2 through `x402ExactPermit2Proxy`, for tokens without it;
  ERC-1271 signatures (FiatToken v2.2's `bytes` overload); x402 over MCP; `upfront` and
  `escrow` flows.
- **Submissions persisted** in the Python relay: they're in memory there; a restart loses
  them. The Worker keeps them in its Durable Object's storage.
- **Running the Worker live** on Cloudflare: it deploys (DEPLOYMENT.md, Cloudflare), but has
  only run live under workerd.
- **A funding transaction given up on** may still be pending; the next reuses its nonce and
  can be refused as an underpriced replacement.
- **Uniswap v4 and TWAP prices; bloXroute's `blxr_submit_bundle`.**
- **The treasury's snapshot in the Python server** is in memory, like its submissions: a
  restart measures spend afresh from the next run.
- **A treasury's v2 hops** go through the router's own Uniswap V2 factory: a pair from another
  factory prices a token, but can't be swapped through.
