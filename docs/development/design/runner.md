---
description: "Why Bruno's transaction is a fresh runner deployed through Arachnid's deployer at a salt only Bruno can work out: what each does on-chain, a submission's calls in order, the payment as a balance difference; and why an op paying by its fees, and calls to listed targets paid by a transfer, go without one"
---

# The deployer and the runner

```
from:  concurrency account i
to:    deployer
data:  salt ‖ runner initcode (code ‖ each call's data)
type:  4 with the client's authorizations, else 2

deployer: CREATE2(value, initcode, salt)
runner:   [deploy] → balance before → calls → [transfer] → balance after ≥ before + amount,
          else revert → SELFDESTRUCT
```

## Deployer

Arachnid's deterministic deployment proxy, `0x4e59b44847b379578588920cA78FbF26c0B4956C`, which
is on every chain already: Bruno deploys nothing of its own to send a runner. Calldata is a
salt and a runner's initcode. It:
- **CREATE2s the initcode with the salt**, passing the transaction's value on to it;
- **returns** the runner's address, 20 bytes;
- **reverts with nothing** if the creation fails, so a runner's own revert data doesn't come
  back: `eth_call`, `eth_estimateGas` and a builder's simulation say only that it failed.

**The salt is a keyed hash:** `keccak256(key ‖ submission id ‖ target block)`, the key a
private key derived from the float's on a path of its own. A submission's runner is at a
different address in every block it's tried for, so no two of Bruno's transactions run from
one address, and no one without the key can work an address out before its transaction is
seen, so nothing can be prepared at it. That is as far as a salt goes. Once the transaction
is public its salt is too, and anyone can create the same runner at the same address again:
the same calls and the same payment check, which pays the payee again or reverts. So nothing
may treat a runner's address as authority, and what a call leaves at it is as good as lost.

**A runner is never a plain contract creation.** A creation transaction's address follows
from its sender and that sender's nonce alone, which anyone can work out ahead, and whatever
the account sends next takes it: one submission's runner could sit at an address prepared for
another's. Through a deployer the address follows from the runner's own initcode and its
salt.

Its code, opcode by opcode, is in `deployer.py`.

## Runner

1. **Deploy an account,** if the submission has a deployment (its own, or an ERC-6492
   signature's) and the account has no code (`EXTCODESIZE`): call the factory with its data,
   given the gas left over ([gas](gas.md)). To prepare a deployed wallet (ERC-6492's other case),
   the code isn't checked. A factory call that fails reverts the runner with its revert data.
2. **Read the payee's balance:** `BALANCE` for ETH; for a token, `balanceOf(payee)` by
   `STATICCALL`, anything but success with 32 bytes reverting `ERCNNNN_BalanceUnreadable()`.
3. **Make each call, in order.** One the relay bounds is given exactly its bound. Any other
   is given all the gas left but a reserve *R* and what the later calls of its kind need
   ([gas](gas.md)). Less than that plus the call's declared gas (and the 1/64 EIP-150 keeps
   back from it) reverts `ERCNNNN_GasExhausted()`, so the call always gets at least what the
   client declared. A call that fails reverts the runner with the call's own revert data.
   Paid by a signed transfer (over x402, or a submission's `payment.transfer`), **make the
   transfer** after the calls: the client's `transferWithAuthorization` to the token, given
   exactly its bound, which *R* holds back from the calls. One that fails reverts the runner
   with the token's revert data. The calls come first so that one may bring the payer what it
   pays with, as calls sent beside their transfer do. A transfer a contract signed is the
   exception, made before the calls: its signature check is the payer's own code, of no bound,
   and it's given the gas left over ([gas](gas.md)). After the calls nothing would hold that
   gas back: a call may use any of its declared gas, and whatever it used more than its
   estimate would be the transfer's.
4. **Read the balance again:** `after < before`, or `after − before < amount`, reverts
   `ERCNNNN_Underpaid()`.
5. **Self-destruct to the sender:** `SELFDESTRUCT(ORIGIN)`. Created in the same transaction,
   the runner's account is removed at its end (EIP-6780): nothing granted to it outlives the
   transaction, and nothing is left in state. Anything sent to its address beforehand goes to
   the relay's account that sent it, warm, so the step costs 5,002 gas. EIP-8037 still charges
   the account's creation in full (183,600 on Amsterdam): it refills nothing for an account
   created and destroyed in one transaction.

**The runner is generated per submission, not deployed once.** Its payee, token, amount and
calls are constants in its code, so it reads no calldata and trusts no input at run time.
Each call's data is appended after the code, in order, and `CODECOPY`'d to memory.

**A call may send ETH.** The transaction carries the calls' values together, the deployer
passes it to the runner as it's created, and the runner sends each call its own. What it
doesn't send returns to the relay's account when it self-destructs. The ETH is the relay's, so
it's part of what a submission is charged, one for one, and a call that sends its value to
the payee has paid nothing by it: the charge is that value and the gas. What the calls send
comes under one cap with the gas ([gas](gas.md)). A call with a value always goes through a
runner, since a listed target is listed for calls that send none.

**Several calls, in the order given.** A runner makes a submission's calls in order and
reverts whole if one fails, so a submission of several is as atomic as one. None of them is
made from the client's account, and anyone who sees the calls can make each alone: a client
whose calls must be its own still batches through its delegate, which orders, authorizes and
replay-protects them by its own signature, as one call. Each call keeps its gas whatever the
others do. A call the relay bounds is given exactly its bound, and any other is given what's
left less what the runner's rest and the later calls need, so a call that burns all it's
given can't take a later call's gas.

**The payment is a balance difference, read twice:** `BALANCE` for ETH (ERC-7528's address
in the interface), `balanceOf` for a token. `after < before` is checked before `SUB`, so a
payee losing funds in the call can't wrap to a large gain. Paying and then taking back is
underpaid. Any token whose `balanceOf` answers 32 bytes works; USDT's missing return
value on `transfer` doesn't matter. `balanceOf` is called with exactly its measured gas, so a
token needing more reads as `ERCNNNN_BalanceUnreadable()` and the runner reverts: a wrong
bound costs nothing.

**A failed call reverts the runner with the call's own revert data.** Arachnid's deployer
doesn't pass it up, so a simulation shows only that the runner failed.

**Except an op paying by its fees:** its `handleOps` goes from the concurrency account to the
EntryPoint as is, with no deployer and no runner. The EntryPoint's own rules make "not paid"
mean "reverted": one that lands has paid its beneficiary, the payee, the op's gas at its price
(its calls failing or not, a paymaster's postOp too), and an op failing its validation reverts
`handleOps`, which the bundle then leaves out. So the payment is the EntryPoint's to prove, and
what it pays at a block's base fee is checked before each signing. It saves the deployer's
overhead (32,115 gas on Prague, 195,715 on Amsterdam, most of it EIP-8037's state cost for the
runner's account), the initcode's calldata and the runner's balance reads, all priced into the
op's `preVerificationGas`. The cost: an op whose calls fail in its block, cheaper than they
simulated, lands, paying for the gas it used, where the runner would have reverted it as short
of the simulated amount. Only an op the relay's bundler made goes this way: a client's own call
to the EntryPoint is a call like any other, and an op paying by its calls keeps the runner, the
EntryPoint paying nothing when those calls fail.

Calls each to a listed target, paid by a signed transfer, go without a runner too. The targets
a concurrency account may call directly are listed in `bruno/targets.json`. Each treats every
caller alike, so the account gains nothing there that a later call could use, and the runner's
isolation isn't needed. The account sends the calls itself, in order, then the transfer to
the token. Several transactions are a bundle of their own, which lands whole or not at all
([bundling](bundling.md)).

This keeps to what the relay does for any call: run what the client gives it, with the gas the
client states, and price its own overhead on top. A call that is its own transaction is given
its gas by the EVM, and the overhead is exactly the transactions' own. The calls come first so that
one can earn what pays for them. What is given up is the runner's measurement of the
payee's balance: payment rests on the transfer, which moves exactly what its payer signed, and
on the bundle being atomic, which everything Bruno sends already rests on. One call to a
target that isn't listed keeps the runner for all of a submission's calls, however it pays,
and so does a submission that deploys an account.

**Consecutive calls the relay bounds are one transaction.** A run of them, the payment's
transfer the last when a key signed it, goes through Multicall3's `aggregate3`, every call
required to succeed. Multicall3 is on the list for the same reason as any target: it treats
every caller alike and keeps nothing. One transaction's own gas is paid where each call
would have paid it, which is what makes several transfers in one submission worth sending.
A call the relay doesn't bound stays a transaction of its own, so the EVM gives it exactly
its gas. In a group nothing caps one call: the group's gas limit is what the relay pays for
and the payment covers, and a group that runs short fails whole. A group that fails reverts with Multicall3's own message, so its refusal doesn't say
which call failed. A chain is measured to have Multicall3's own code before it's served: an
`aggregate3` sent to an address without it would succeed and make no call.

**Why Arachnid's deployer, not one of our own:** there is nothing to deploy on a new chain,
and one component fewer to check before serving it. A deployer of our own could compute the
salt on chain from its caller, which would make a runner's address one sender's alone, and
could pass a runner's revert data up. Both are given up for now. The deployer is one module
in each relay (its address, its calldata, its gas, what it answers), so another can take its
place.