---
description: "Why Bruno's transaction is a fresh runner deployed through its own deployer: what each does on-chain, one call, the payment as a balance difference"
---

# The deployer and the runner

```
from:  concurrency account i
to:    deployer
data:  runner initcode (code ‖ the call's data)
type:  4 with the client's authorizations, else 2

deployer: CREATE2(0, initcode, keccak256(caller ‖ block number))
runner:   [deploy] → balance before → [transfer] → call → balance after ≥ before + amount,
          else revert → SELFDESTRUCT
```

## Deployer

`0x48F47C9c5FC7bb594e31172a3FdEC10D8c51144c` on every chain: deployed through Arachnid's
deterministic deployer (`0x4e59b44847b379578588920cA78FbF26c0B4956C`) with salt 0. Ownerless,
no storage. Calldata is a runner's initcode. It:
- **salts with `keccak256(caller ‖ block number)`**: a runner's address belongs to one
  caller in one block, so no one else can deploy at it, and it can't be reused later;
- **returns** the runner's address, ABI-encoded;
- **reverts with the runner's revert data**, the failed CREATE2's return data, so `eth_call`,
  `eth_estimateGas` or a builder's simulation of the transaction reports why it fails. Empty
  after an address collision.

Its code, opcode by opcode, is in `deployer.py`.

## Runner

1. **Deploy an account,** if the submission has a deployment (its own, or an ERC-6492
   signature's) and the account has no code (`EXTCODESIZE`): call the factory with its data,
   given the gas left over ([gas](gas.md)). To prepare a deployed wallet (ERC-6492's other case),
   the code isn't checked. A factory call that fails reverts the runner with its revert data.
2. **Read the payee's balance:** `BALANCE` for ETH; for a token, `balanceOf(payee)` by
   `STATICCALL`, anything but success with 32 bytes reverting `ERCNNNN_BalanceUnreadable()`.
   Paid by a signed transfer (over x402, or a submission's `payment.transfer`), **make the
   transfer:** the client's `transferWithAuthorization` to the token, given exactly its bound
   for a key's signature, or the gas left over for a contract's ([gas](gas.md)); one that fails
   reverts the runner with the token's revert data.
3. **Make the call** with all but a reserve *R* of the gas left ([gas](gas.md)). Less than
   *R* plus the call's declared gas (and the 1/64 EIP-150 keeps back from it) reverts
   `ERCNNNN_GasExhausted()`, so the call always gets at least what the client declared. A
   call that fails reverts the runner with the call's own revert data.
4. **Read the balance again:** `after < before`, or `after − before < amount`, reverts
   `ERCNNNN_Underpaid()`.
5. **Self-destruct to the sender:** `SELFDESTRUCT(ORIGIN)`. Created in the same transaction,
   the runner's account is removed at its end (EIP-6780): nothing granted to it outlives the
   transaction, and nothing is left in state. Anything sent to its address beforehand goes to
   the relay's account that sent it, warm, so the step costs 5,002 gas. EIP-8037 still charges
   the account's creation in full (183,600 on Amsterdam): it refills nothing for an account
   created and destroyed in one transaction.

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
