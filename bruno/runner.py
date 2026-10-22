"""The runner: initcode the service generates per submission and deploys through the
deployer. It makes the client's one call and asserts the payment.

In order:
1. read the payee's balance (BALANCE for native ETH; for a token, STATICCALL balanceOf
   given exactly the token's balance_gas, reverting ERCNNNN_BalanceUnreadable() unless that
   call succeeded with exactly 32 bytes);
   if the payment comes with a transfer (an EIP-3009 authorization), call the token with its
   calldata, given exactly its gas: one that fails reverts the runner with its revert data;
2. make the call, CALL(GAS - R, target, 0, data), reverting ERCNNNN_GasExhausted() if
   GAS < R; a call that fails reverts the runner with the call's own revert data;
3. read the balance again: revert ERCNNNN_Underpaid() unless after >= before and
   after - before >= the amount;
4. SELFDESTRUCT(ORIGIN): created in this transaction, the runner's account is removed at its
   end (EIP-6780), leaving nothing in state; anything sent to its address before goes to the
   relay's account that sent the transaction.

R is the most gas everything after the call's GAS reading can take: the opcodes to the end,
memory expansion, and the balanceOf's cap with what EIP-150's 63/64 rule needs to grant it.
A call that uses all it's given still leaves the runner enough to finish. The assembler sums
these as it emits the code; each piece carries its own worst case.

The call's data is appended after the code and copied to memory (CODECOPY) before the call;
a transfer's calldata likewise, after it.
"""

from collections.abc import Collection
from dataclasses import dataclass
from math import ceil

from eth_typing import ChecksumAddress
from eth_utils.crypto import keccak

from .assembler import Assembly, Op
from .common import BrunoError
from .evm import WARM_ACCOUNT_ACCESS, EncodedCall
from .forks import GasSchedule

# EIP-3860's limit on initcode: 2 x the 24,576-byte code limit
MAX_INITCODE_BYTES = 49_152

# the standard's error namespace: its ERC number, once assigned
ERC = "ERCNNNN"
UNDERPAID = keccak(text=f"{ERC}_Underpaid()")[:4]
BALANCE_UNREADABLE = keccak(text=f"{ERC}_BalanceUnreadable()")[:4]
# the call was reached with less than R left: the outer gas limit was too low
GAS_EXHAUSTED = keccak(text=f"{ERC}_GasExhausted()")[:4]
_BALANCE_OF = keccak(text="balanceOf(address)")[:4]


class RunnerError(BrunoError):
    pass


@dataclass(frozen=True, slots=True)
class Erc20:
    """A payment token, and the most gas its balanceOf may take: the runner gives each of
    its balanceOf calls exactly that, so a token needing more reads as
    ERCNNNN_BalanceUnreadable()
    (the runner reverts, and is dropped: a wrong bound costs nothing). An EIP-3009 token's
    `transfer_gas` is a zero-value transferWithAuthorization's, measured (eip3009.measured)."""

    address: ChecksumAddress
    balance_gas: int
    transfer_gas: int | None = None


@dataclass(frozen=True, slots=True)
class Transfer:
    """A call to the payment's token that the runner makes before the client's: its calldata,
    given exactly `gas` (an EIP-3009 transfer, its gas bounded). A bound too low fails it, and
    the runner reverts: it costs nothing."""

    calldata: bytes
    gas: int


@dataclass(frozen=True, slots=True)
class Payment:
    """What the payee must receive during the client's call: at least `amount` of `token`,
    or of native ETH when `token` is None; and a transfer to make first, if the payment comes
    with one."""

    token: Erc20 | None
    payee: ChecksumAddress
    amount: int
    transfer: Transfer | None = None


@dataclass(frozen=True, slots=True)
class Runner:
    """A runner's initcode, and the most gas the runner itself takes: everything but what
    the client's call is given, which is the rest of its frame's gas."""

    initcode: bytes
    overhead_gas: int

    def frame_gas(self, client_gas: int) -> int:
        """The gas the runner's frame needs for the client's call to be given `client_gas`:
        its own, the client's, and what EIP-150's 63/64 rule keeps back from a call given
        all that's left."""
        return self.overhead_gas + client_gas + ceil(client_gas / 63)


def in_call_runner(
    call: EncodedCall,
    payment: Payment,
    schedule: GasSchedule,
    delegated: Collection[ChecksumAddress] = (),
) -> Runner:
    """The runner for `call` and `payment`, its gas bounded by `schedule`. `delegated` are
    the targets that are, or by the transaction's authorization list will be, EIP-7702
    delegated: a CALL to one also loads its delegate's code."""
    if call.value != 0:
        raise RunnerError("the client's call carries no value: the runner holds no ETH")
    if not 0 < payment.amount < 2**256:
        raise RunnerError(f"payment amount {payment.amount} isn't a positive uint256")
    asm = Assembly()
    _balance(asm, payment, schedule.cold_account_access)  # stack: before
    blobs = [bytes.fromhex(call.data.removeprefix("0x"))]
    if payment.transfer is not None:
        if payment.token is None:
            raise RunnerError("a transfer pays in a token, not ETH")
        _transfer(asm, payment.token.address, payment.transfer)
        blobs.append(payment.transfer.calldata)
    _call(asm, call, _call_access(call.to, delegated, schedule))
    _balance(asm, payment, WARM_ACCOUNT_ACCESS)  # stack: before after
    # after < before: the payee lost funds in the call
    asm.op(Op.DUP2, Op.DUP2, Op.LT).jumpi("underpaid")
    asm.op(Op.SUB)  # stack: after - before
    asm.push(payment.amount).op(Op.DUP2, Op.LT).jumpi("underpaid")
    asm.op(Op.ORIGIN, Op.SELFDESTRUCT)
    asm.end()
    _call_failed(asm)
    _revert_with(asm, "underpaid", UNDERPAID)
    _revert_with(asm, "balance_unreadable", BALANCE_UNREADABLE)
    _revert_with(asm, "gas_exhausted", GAS_EXHAUSTED)
    code, overhead = asm.assemble(blobs)
    if len(code) > MAX_INITCODE_BYTES:
        raise RunnerError(f"initcode is {len(code)} bytes, over EIP-3860's {MAX_INITCODE_BYTES}")
    return Runner(initcode=code, overhead_gas=overhead)


def _call_access(
    target: ChecksumAddress, delegated: Collection[ChecksumAddress], schedule: GasSchedule
) -> int:
    """The most a CALL to `target` is charged before it forwards gas: the target's access,
    cold at worst, and for a delegated EOA its delegate's too (EIP-7702)."""
    return schedule.cold_account_access * (2 if target in delegated else 1)


def _balance(asm: Assembly, payment: Payment, access: int) -> None:
    """Push the payee's balance, the payee and token costing `access`: cold for the first
    read, which makes them warm for the second (EIP-2929)."""
    if payment.token is None:
        asm.push_address(payment.payee).balance(access)
        return
    # memory[0:36] = balanceOf(payee); its answer to memory[0:32]
    asm.push(int.from_bytes(_BALANCE_OF) << 224).mstore_at(0)
    asm.push_address(payment.payee).mstore_at(4)
    asm.push(32).push(0).push(36).push(0).push_address(payment.token.address)
    asm.push(payment.token.balance_gas).staticcall(access, payment.token.balance_gas)
    asm.op(Op.ISZERO).jumpi("balance_unreadable")
    # a token with no code answers nothing: success, but no 32 bytes
    asm.op(Op.RETURNDATASIZE).push(32).op(Op.EQ, Op.ISZERO).jumpi("balance_unreadable")
    asm.mload_at(0)


def _transfer(asm: Assembly, token: ChecksumAddress, transfer: Transfer) -> None:
    """Call `token` with the transfer's calldata (the second blob), given exactly its gas: the
    token is warm, its balance read first. One that fails reverts with its revert data."""
    length = len(transfer.calldata)
    asm.push(length).push_data(1).push(0).codecopy(length)
    asm.push(0).push(0).push(length).push(0).push(0).push_address(token).push(transfer.gas)
    asm.capped_call(WARM_ACCOUNT_ACCESS, transfer.gas)
    asm.op(Op.ISZERO).jumpi("call_failed")


def _call(asm: Assembly, call: EncodedCall, access: int) -> None:
    """Make the client's call with all but R of the gas left: GasExhausted() if there isn't
    R; the call's own revert data if it fails."""
    length = len(bytes.fromhex(call.data.removeprefix("0x")))
    asm.push(length).push_data(0).push(0).codecopy(length)
    # CALL's arguments, last pushed first: gas, to, value, argsOffset, argsSize, retOffset,
    # retSize
    asm.push(0).push(0).push(length).push(0).push(0).push_address(call.to)
    # its gas, GAS - R, checked against the same GAS reading: a GAS read again after the
    # check could be under R, and the subtraction would wrap to a call given everything
    asm.reserve(0).gas_read(0).op(Op.DUP2, Op.DUP2, Op.LT).jumpi("gas_exhausted")
    asm.op(Op.SUB).client_call(access)
    asm.op(Op.ISZERO).jumpi("call_failed")


def _call_failed(asm: Assembly) -> None:
    """Revert with the failed call's revert data, as it is."""
    asm.label("call_failed")
    asm.op(Op.RETURNDATASIZE).push(0).push(0).op(Op.RETURNDATACOPY)
    asm.op(Op.RETURNDATASIZE).push(0).op(Op.REVERT)


def _revert_with(asm: Assembly, label: str, selector: bytes) -> None:
    asm.label(label)
    asm.push(int.from_bytes(selector) << 224).push(0).op(Op.MSTORE)
    asm.push(4).push(0).op(Op.REVERT)
