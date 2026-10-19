# Bruno: permissionless post-paid gas relays

An account holding tokens but no ETH can't send a transaction. Bruno sends it and pays the
gas, and the transaction repays Bruno before it ends or reverts.

The mechanism rests on builders: Bruno sends only through `eth_sendBundle` (or equivalent),
so that a builder drops a reverting transaction instead of including it. A
transaction that doesn't pay therefore never lands, and costs Bruno nothing. A builder that
includes it reverted makes Bruno pay its gas, unpaid; the client is protected by the payment
check either way.

1. **Submit.** The client sends one call (typically to its own account, running a batch it
   signed through its 7702 delegate), its EIP-7702 authorization if needed, and a payment:
   token and amount.
2. **Wrap.** Bruno generates a runner: initcode that reads the payee's balance, makes the
   call, and reverts unless the payee (Bruno's float) gained at least the amount.
3. **Send.** One of Bruno's concurrency accounts sends the runner to Bruno's deployer, which
   CREATE2s it at an address bound to that account and block. The client's authorization
   rides in the same type-4 transaction. The float funds the accounts' gas.
4. **Bundle.** The transaction goes to builders in `droppingTxHashes`, or alone in its
   bundle.

The client's signatures fix what runs and what it pays; Bruno can only include it or not.
Each runner is a new address that keeps no code, so nothing granted to it outlives its
transaction.

Proof of concept. Target: Sepolia (Glamsterdam, ePBS).

## Client contract

What a client can rely on from any relay of this kind; Bruno is a reference implementation.

A submission is an optional EIP-7702 authorization, one call (a target and its calldata), a
payee and an amount.

**The relay guarantees:** nothing is included unless, during the call, the payee gains at
least the amount. A submission that isn't included costs the client nothing.

**The relay doesn't guarantee:** that the authorization or the call runs at all, or that they
run together; the sender; the call's `msg.sender`.

**So the call itself must, by the client's own signature:**
- authorize everything it does, never relying on `msg.sender`;
- check, before paying, that any delegation it relies on is in place: a delegate whose
  signatures bind its implementation (Calibur's EIP-712 salt does), or a check of the
  account's code hash against `keccak256(0xef0100 ‖ delegate)`;
- order and batch whatever it does;
- carry its own replay protection and validity window: anyone who sees a submission can
  run it.

## Bundling

Each submission is sent from one of the relay's concurrency accounts, whose gas the float
funds. There are two ways to bundle the submissions.

**Standalone.** Each submission is its own bundle, sent from an account that was funded
beforehand. A reverting submission fails its bundle and isn't included. This works with any
builder that takes bundles.

**Dropping.** There is one bundle per block. It starts with a funding transaction from the
float, which tops up each account in the bundle to its submission's maximum cost. Every
submission follows, listed in `droppingTxHashes`. A reverting submission is dropped, and the
rest land with the funding. Accounts need no balance beforehand. The funding lands even if
every submission is dropped, and stays in the accounts. This needs a builder that honours
`droppingTxHashes`.

Builder rule relied on (rbuilder `order_commit.rs` @ `6037fa7`, 698–732, 824–878): a
reverting or invalid transaction in `droppingTxHashes` is skipped; one listed nowhere fails
the bundle; a failed bundle is rolled back whole.

## Reference implementation

### Flow

```
from:  concurrency account i
to:    deployer
data:  runner initcode
type:  4 with the client's authorizations, else 2

deployer: CREATE2(0, initcode, keccak256(caller ‖ block number))
runner:   balance before → client call → balance after ≥ before + amount, else revert → RETURN(0, 0)
```

### Deployer

`0x48F47C9c5FC7bb594e31172a3FdEC10D8c51144c`, deployed through Arachnid's deployer
(`0x4e59b44847b379578588920cA78FbF26c0B4956C`) with salt 0. Calldata is the initcode.

- **Reverts with the runner's revert data**, the failed CREATE2's return data. A plain
  `eth_call`, `eth_estimateGas`, or builder simulation of the transaction reports why it
  fails: `ERCNNNN_Underpaid()`, the client call's own revert data, and so on. Empty after an
  address collision.
- **Salts with `keccak256(caller ‖ block number)`**: a runner's address belongs to one
  caller in one block. The same initcode from another caller, or in another block, is
  another runner.
- **Returns** the runner's address, ABI-encoded.

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

### Runner

`runner.py: in_call_runner(call, payment, schedule, delegated) -> Runner`

- `call`: an `EncodedCall`, value 0.
- `payment`: `Payment(token, payee, amount)`; `token` is `Erc20(address, balance_gas)` or
  `None` for ETH.
- `delegated`: targets that are or will be 7702-delegated.

Initcode: code, then the call's data. Execution:

1. Payee balance: `BALANCE`, or `STATICCALL(balance_gas, token, balanceOf(payee))`; anything
   but success with 32 bytes reverts `ERCNNNN_BalanceUnreadable()`.
2. `CODECOPY` the call's data to `memory[0:len]`; read `GAS` as *g*; *g* < *R* reverts
   `ERCNNNN_GasExhausted()`; `CALL(g − R, to, 0, 0, len, 0, 0)`; a failed call reverts with its
   own revert data.
3. Balance again; `after < before` or `after − before < amount` reverts `ERCNNNN_Underpaid()`.
4. `RETURN(0, 0)`.

| Error | Selector |
|---|---|
| `ERCNNNN_Underpaid()` | `0xdf9459c7` |
| `ERCNNNN_BalanceUnreadable()` | `0x693e471f` |
| `ERCNNNN_GasExhausted()` | `0x472c00d6` |

`ERCNNNN` stands for the standard's number, once assigned (`runner.py: ERC`).

#### Gas

`assembler.py` bounds each success-path piece. *R* is the sum of everything after the
call's `GAS` read, emitted as `PUSH3`. The client's `CALL` counts only its access (doubled for
a delegated target). `balanceOf` costs its access plus `ceil(balance_gas × 64/63)`.

- `Runner.overhead_gas`: the success path's total.
- `Runner.frame_gas(client_gas)`: `overhead_gas + client_gas + ceil(client_gas / 63)`.
- `tokens.py: erc20()`: `balance_gas` = `eth_estimateGas(balanceOf(holder))` − intrinsic.
- `deployer.py: overhead_gas(n, schedule)`: the deployer's own gas for `n` bytes of initcode.
- `deployer.py: transaction_gas(initcode, runner_gas, schedule, authorizations,
  empty_authorities)`: `max(intrinsic + execution, floor)`, execution being the deployer's
  gas, `ceil((runner_gas + 27) × 64/63)` at CREATE2, and the authorizations'.

#### Gas schedules

`forks.py`: one `GasSchedule` per fork. `Chain.schedule()` selects by the next block's
timestamp.

| Network | Fork |
|---|---|
| Ethereum | Prague |
| Sepolia | Amsterdam from 1791294816 |
| Hoodi | Amsterdam from 1793036568 |

| | Prague | Amsterdam |
|---|---|---|
| Cold account access | 2,600 | 3,000 |
| `CALL` value | 9,000 | 11,300 |
| New account: `CALL` / transaction | 25,000 / 0 | 183,600 / 183,600 |
| `CREATE2` / creation tx, empty target | 32,000 | 195,600 |
| Tx base / recipient / value | 21,000 / 0 / 0 | 12,000 / 3,000 / 6,000 |
| Calldata floor, zero / nonzero byte | 10 / 40 | 64 / 64 |
| Authorization: intrinsic / new delegation / empty authority | 25,000 / 0 / 0 | 7,816 / 44,190 / 183,600 |

Amsterdam: EIP-2780, 7976, 8037, 8038.

### Accounts

`accounts.py: derive(float, i)`: concurrency account *i*, BIP-32 with the float's key as seed,
`m/44'/60'/0'/0/i`. The float is the payee and funds the accounts.

### Transactions

`transactions.py`:
- `sign(...)`: type 2 (creation if `to` is `None`), or type 4 with authorizations.
- `sign_drain(...)`: type 1, value `balance − gas × gas_price`; leaves 0.

### Bundles

`bundle.py: bundles(mode, block, funding, txs)`, with one `Mode` per run: `STANDALONE`
(funded beforehand by maintenance) or `DROPPING`.

The funding tx is a creation whose initcode sends each account its shortfall
(`max(0, tx.max_cost − balance)`) and returns nothing. `version` is unset;
`revertingTxHashes` is never used.

`builders.py: send_bundle(client, url, params, identity)` adds `X-Flashbots-Signature` when
given an identity.

### Maintenance

`maintenance.py`, accounts by index:
- `monitor`: balances and nonces.
- `top_ups(states, required)`: shortfalls against `required` (index → wei).
- `replenish`: the shortfalls, in one funding tx.
- `sweep`: drains to the float, to 0.

## Development

```
make install          toolchain, project-local
make check            lint, format, ty, pyrefly; tests on Prague and Amsterdam anvils
make check-sepolia    live test on Sepolia (spends Sepolia ETH)
make <net>-wallet     create or show .secrets/<net>/wallet.key
make <net>-deploy     deploy the deployer and Calibur
make <net>-sweep      drain the live-test accounts into the wallet
```

anvil: nightly `6c21ffe`, port 8546, `--order fifo --disable-pool-balance-checks`.
`tests/localchain.py` installs the deployer and the fixtures in `tests/fixtures/evm/`
(Calibur, Permit2, the x402 proxy, USDC, UNI, WETH9, USDT). `tests/mockbuilder.py` applies
the builder rule above to anvil.

`make check-sepolia` sends standalone bundles to `https://builder-sepolia.nflaig.dev/rpc`, which
needs an `X-Flashbots-Signature`.
