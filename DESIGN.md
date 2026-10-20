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

**Per-fork schedules.** The figures that differ between forks are kept per fork, chosen by the
next block's timestamp. Each is checked exactly against receipts on an anvil at that fork.

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

**Bounded.** The accounts hold at most `accounts` × `funding_multiplier` × the largest
transaction's cost. A transaction can't spend more than its gas limit × max fee, and nothing
is signed unless its account covers that, so an account can't be overdrawn: one that falls
short sits out until topped up. Shortfalls cost capacity, not money.

## Clock and reads

**Work only while there's work.** A relay's clock runs while a submission is pending or a
funding transaction is in flight: a submission taken starts it, and its batch closes at once.
With none, it stops; it wakes only to forget finished submissions after `retention_seconds`,
reading nothing. Quotes, status and the configuration are answered on request. An idle relay
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
rejected, rather than simulating down the ranking until the accounts are filled.

## Price

**Signed at exactly the next block's base fee** plus the priority fee: a bundle targets one
block, whose base fee is known from the latest. The price is that fee × the gas limit, in the
token, × the margins.

**Fee ceiling,** for quotes and top-ups: the next base fee risen by an eighth a block,
EIP-1559's most, for `quote_blocks` blocks. A quote is advisory; each block decides.

**Token prices from pools,** each pool's spot price along the token's path to WETH. ETH and
WETH need none: a wei is a wei. A spot
price can be moved within a block, but it's read from the latest block's state: moving it
means leaving the pool off-market at a block's end, open to arbitrage. Deep pools, and a
cache of `price_ttl_seconds`.

**Margins are multipliers,** string `Decimal`s, never basis points.

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
USDT, UNI, WETH9, Permit2, Uniswap v2 and v3 pools) and installed on anvil; runners are
generated and executed on-chain; gas is asserted exactly against receipts, on a Prague and an
Amsterdam anvil.

**A mock builder** applies rbuilder's commit rule to anvil, bundle by bundle, so both modes
are tested end to end. The live test sends standalone bundles to an ePBS builder on Sepolia.

To come: an rbuilder devnet running the same scenarios; mainnet checks with a small float
and a deliberate non-payer; a log of every bundle sent.

## Cloudflare Workers

The relay ported to TypeScript (`worker/`). The Worker routes each request to its chain's
Durable Object, one a chain, which keeps the queue, the batch and the funding in flight in its
SQLite storage. Its alarms are the clock (Clock and reads): each step an alarm.

**Idle costs nothing.** An object with nothing queued holds no alarm, so it's evicted from
memory and billed nothing; a deployment serving a quiet chain costs only its requests.

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

- **Prepaid (x402):** the payment taken before the call, so payment buys inclusion, not
  success.
- **Submissions persisted** in the Python relay: they're in memory there; a restart loses
  them. The Worker keeps them in its Durable Object's storage.
- **Deploying the Worker:** through Cloudflare's API, and running it live.
- **A funding transaction given up on** may still be pending; the next reuses its nonce and
  can be refused as an underpriced replacement.
- **Uniswap v4 and TWAP prices; bloXroute's `blxr_submit_bundle`.**
