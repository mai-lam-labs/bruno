"""The runner: initcode the service generates per submission and deploys through the
deployer. It makes the client's calls, in order, and asserts the payment.

In order:
1. if there's a deployment (ERC-6492's, or the submission's own) and its account has no code
   (EXTCODESIZE; unchecked when it's to prepare a deployed account), call its factory with its
   data, given the gas left over (below): one that fails reverts the runner with its revert
   data;
2. read the payee's balance (BALANCE for native ETH; for a token, STATICCALL balanceOf
   given exactly the token's balance_gas, reverting ERCNNNN_BalanceUnreadable() unless that
   call succeeded with exactly 32 bytes);
3. make each call, in order, sending it its value, if it has one, from what the transaction
   sent the runner. One the relay bounds is given exactly its gas, CALL(gas, target,
   0, data). Any other is given all that's left but what follows it needs, CALL(GAS - R - L,
   target, 0, data), L what the later calls of its kind need, reverting
   ERCNNNN_GasExhausted() unless GAS >= R + L + N, N what the call's declared gas needs (the
   gas, and EIP-150's 63/64 kept back from it): the call is given at least its declared gas.
   One that fails reverts the runner with the call's own revert data;
   if the payment comes with a transfer (an EIP-3009 authorization) a key signed, call the
   token with its calldata, given exactly its gas: after the calls, so a call may bring the
   payer what it pays with; one that fails reverts the runner with its revert data. A
   contract's transfer, whose signature check is its own code, is made before the calls
   instead, given the gas left over (below): what has no bound runs first, the calls'
   declared gas and the rest of the runner kept back from it;
4. read the balance again: revert ERCNNNN_Underpaid() unless after >= before and
   after - before >= the amount;
5. SELFDESTRUCT(ORIGIN): created in this transaction, the runner's account is removed at its
   end (EIP-6780), leaving nothing in state; what it holds, ETH its calls didn't send and
   anything sent to its address before, goes to the relay's account that sent the
   transaction.

R is the most gas everything after a call's GAS reading can take: the opcodes to the end,
memory expansion, and each cap (a bounded call's, a key's transfer's, the balanceOf's) with
what EIP-150's 63/64 rule needs to grant it. A call that uses all it's given still leaves the
runner enough to finish, and every later call its gas. The assembler sums
these as it emits the code; each piece carries its own worst case.

A step given the gas left over (a deployment, a contract's transfer) runs code the client
chooses, of any cost: CALL(GAS - R' - N), R' its own R and N what every call not bounded
needs, reverting ERCNNNN_GasExhausted() if GAS < R' + N, so it leaves the rest of the runner
and the calls' declared gas. Its runner has
no bound of its own: its transaction's gas is estimated as it will run (plan.py).

Each call's data is appended after the code, in order, and copied to memory (CODECOPY) before
its call; a transfer's calldata likewise, after them, and a deployment's factory data after
that.
"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from math import ceil

from eth_typing import ChecksumAddress

from .abi import BALANCE_OF, selector
from .assembler import Assembly, AssemblyError, Op
from .common import BrunoError
from .erc6492 import Deployment
from .evm import WARM_ACCOUNT_ACCESS, EncodedCall
from .forks import GasSchedule

# EIP-3860's limit on initcode: 2 x the 24,576-byte code limit
MAX_INITCODE_BYTES = 49_152

# the standard's error namespace: its ERC number, once assigned
ERC = "ERCNNNN"
UNDERPAID = selector(f"{ERC}_Underpaid()")
BALANCE_UNREADABLE = selector(f"{ERC}_BalanceUnreadable()")
# the call couldn't be given its declared gas, or a step what the rest needs: the outer gas
# limit was too low
GAS_EXHAUSTED = selector(f"{ERC}_GasExhausted()")


class RunnerError(BrunoError):
    pass


@dataclass(frozen=True, slots=True)
class Erc20:
    """A payment token, and the most gas its balanceOf may take: the runner gives each of
    its balanceOf calls exactly that, so a token needing more reads as
    ERCNNNN_BalanceUnreadable()
    (the runner reverts, and is dropped: a wrong bound costs nothing). An EIP-3009 token's
    `transfer_gas` is a zero-value transferWithAuthorization's, measured (eip3009.measured);
    `forward_gas` a zero-value transfer's by the payee's forwarder, measured, None if it didn't
    measure."""

    address: ChecksumAddress
    balance_gas: int
    transfer_gas: int | None = None
    forward_gas: int | None = None


@dataclass(frozen=True, slots=True)
class Transfer:
    """A call to the payment's token that the runner makes with the client's: its calldata,
    given exactly `gas` (a key's EIP-3009 transfer, its gas bounded), made after the client's
    call; or None: the gas left over (a contract's, whose signature check is its own code),
    made before it. A bound too low fails it, and the runner reverts: it costs nothing."""

    calldata: bytes
    gas: int | None


@dataclass(frozen=True, slots=True)
class Payment:
    """What the payee must receive during the client's call: at least `amount` of `token`,
    or of native ETH when `token` is None; and a transfer to make with it, if the payment
    comes with one."""

    token: Erc20 | None
    payee: ChecksumAddress
    amount: int
    transfer: Transfer | None = None


@dataclass(frozen=True, slots=True)
class PlannedCall:
    """One of a submission's calls as it's planned: the `call`, and the `gas` it's given. One
    the relay `bounded` is given exactly that, its own bound of the call with what EIP-150
    keeps back (plan.planned_call decides, nothing else does); any other, at least what its
    submission states."""

    call: EncodedCall
    gas: int
    bounded: bool = False


@dataclass(frozen=True, slots=True)
class Runner:
    """A runner's initcode, and the most gas the runner itself takes, its bounded calls'
    among it: everything but what its other calls are given, which is the rest of its
    frame's gas."""

    initcode: bytes
    overhead_gas: int

    def frame_gas(self, calls: Sequence[PlannedCall]) -> int:
        """The gas the runner's frame needs for each of `calls` to be given its gas: its own,
        and what the calls not bounded need (calls_need). A runner with a step given the gas
        left over needs that step's too, which only an estimate knows."""
        return self.overhead_gas + calls_need(calls)


def call_need(client_gas: int) -> int:
    """What the call needs left above its R to be given `client_gas`: the gas, and what EIP-150's
    63/64 rule keeps back from a call given all that's left."""
    return client_gas + ceil(client_gas / 63)


def calls_need(calls: Sequence[PlannedCall]) -> int:
    """What `calls` need left above the runner's own gas, together: each call's not bounded
    (call_need). A bounded call's gas is the runner's own, a cap among its overhead."""
    return sum(call_need(planned.gas) for planned in calls if not planned.bounded)


def client_gas_within(need: int) -> int:
    """The most gas a call can be given with `need` left above its R: call_need's inverse."""
    return max(0, need) * 63 // 64


def in_call_runner(
    calls: Sequence[PlannedCall],
    payment: Payment,
    schedule: GasSchedule,
    delegated: Collection[ChecksumAddress] = (),
    *,
    deployment: Deployment | None = None,
    prepare: bool = False,
) -> Runner:
    """The runner for `calls`, made in order, each given its gas, and `payment`, its gas bounded
    by `schedule`; `deployment` made first, if its account has no code (always, to `prepare` a
    deployed one). `delegated` are the targets that are, or by the transaction's authorization
    list will be, EIP-7702 delegated: a CALL to one also loads its delegate's code."""
    return _runner(calls, payment, schedule, delegated, deployment, prepare, True)


def estimating_runner(
    calls: Sequence[PlannedCall],
    payment: Payment,
    schedule: GasSchedule,
    delegated: Collection[ChecksumAddress] = (),
    *,
    deployment: Deployment | None = None,
) -> Runner:
    """in_call_runner's runner without its payment's transfer and check: the deployment, the
    payee's balance read, then the calls, one stating no gas given all the gas left but what
    the later ones need, as the runner sent makes them, so each finds the state it will. It
    doesn't revert unpaid, so it's sent only where something after it checks the payment: a
    sponsorship's calls, the forwarder checking in the bundle's last transaction. Otherwise
    it's only estimated, for what the call stating no gas needs (plan.call_gas), a transfer
    unsigned yet priced from its bound (eip3009.transfer_gas)."""
    return _runner(calls, payment, schedule, delegated, deployment, False, False)


def _runner(
    calls: Sequence[PlannedCall],
    payment: Payment,
    schedule: GasSchedule,
    delegated: Collection[ChecksumAddress],
    deployment: Deployment | None,
    prepare: bool,
    checked: bool,
) -> Runner:
    """A runner's steps: its payment's transfer and check only if `checked`. Each call's data
    is the blob of its place and its GAS reading the reading of its place; the transfer's and
    the deployment's follow the calls'."""
    if not calls:
        raise RunnerError("a runner makes at least one call")
    if any(planned.call.value and planned.bounded for planned in calls):
        raise RunnerError("a call the relay bounds sends no ETH")
    if checked and not 0 < payment.amount < 2**256:
        raise RunnerError(f"payment amount {payment.amount} isn't a positive uint256")
    need = calls_need(calls)
    asm = Assembly()
    blobs = [planned.call.data for planned in calls]
    transfer = payment.transfer if checked else None
    transfer_blob = len(blobs)
    transfer_reading, deployment_reading = len(calls), len(calls) + 1
    if transfer is not None:
        blobs.append(transfer.calldata)
    if deployment is not None:
        blobs.append(deployment.factory_data)
        access = schedule.cold_account_access
        _deploy(asm, deployment, len(blobs) - 1, deployment_reading, access, need, prepare)
    _balance(asm, payment, schedule.cold_account_access)  # stack: before
    if transfer is not None and payment.token is None:
        raise RunnerError("a transfer pays in a token, not ETH")
    # a transfer with no bound, before the calls: after them, nothing would keep its gas back
    if transfer is not None and payment.token is not None and transfer.gas is None:
        _transfer(asm, payment.token.address, transfer, transfer_blob, transfer_reading, need)
    for index, planned in enumerate(calls):
        access = _call_access(planned.call.to, delegated, schedule)
        if planned.call.value:
            # sending ETH: its charge, and a new account's, the target perhaps empty by then
            access += schedule.call_value + schedule.new_account
        _client_call(asm, index, planned, access, calls_need(calls[index + 1 :]))
    if transfer is not None and payment.token is not None and transfer.gas is not None:
        _transfer(asm, payment.token.address, transfer, transfer_blob, transfer_reading, need)
    if checked:
        _balance(asm, payment, WARM_ACCOUNT_ACCESS)  # stack: before after
        # after < before: the payee lost funds in the calls
        asm.op(Op.DUP2, Op.DUP2, Op.LT).jumpi("underpaid")
        asm.op(Op.SUB)  # stack: after - before
        asm.push(payment.amount).op(Op.DUP2, Op.LT).jumpi("underpaid")
    asm.op(Op.ORIGIN, Op.SELFDESTRUCT)
    asm.end()
    call_failed(asm)
    if checked:
        revert_with(asm, "underpaid", UNDERPAID)
    revert_with(asm, "balance_unreadable", BALANCE_UNREADABLE)
    revert_with(asm, "gas_exhausted", GAS_EXHAUSTED)
    try:
        assembled = asm.assemble(blobs)
    except AssemblyError as e:
        raise RunnerError(str(e)) from e
    code = assembled.code
    if len(code) > MAX_INITCODE_BYTES:
        raise RunnerError(f"initcode is {len(code)} bytes, over EIP-3860's {MAX_INITCODE_BYTES}")
    return Runner(initcode=code, overhead_gas=assembled.gas)


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
    asm.push(int.from_bytes(BALANCE_OF) << 224).mstore_at(0)
    asm.push_address(payment.payee).mstore_at(4)
    asm.push(32).push(0).push(36).push(0).push_address(payment.token.address)
    asm.push(payment.token.balance_gas).staticcall(access, payment.token.balance_gas)
    asm.op(Op.ISZERO).jumpi("balance_unreadable")
    # a token with no code answers nothing: success, but no 32 bytes
    asm.op(Op.RETURNDATASIZE).push(32).op(Op.EQ, Op.ISZERO).jumpi("balance_unreadable")
    asm.mload_at(0)


def _deploy(
    asm: Assembly,
    deployment: Deployment,
    blob: int,
    reading: int,
    access: int,
    need: int,
    prepare: bool,
) -> None:
    """Call the deployment's factory with its data (blob `blob`), given the gas left over
    (GAS reading `reading`), unless its account has code already (unchecked to `prepare` it):
    both cold, the first touched. One that fails reverts with its revert data."""
    if not prepare:
        asm.push_address(deployment.account).extcodesize(access).jumpi("deployed")
    length = len(deployment.factory_data)
    asm.push(length).push_data(blob).push(0).codecopy(length)
    asm.push(0).push(0).push(length).push(0).push(0).push_address(deployment.factory)
    _left_over(asm, reading, need)
    asm.client_call(access)
    asm.op(Op.ISZERO).jumpi("call_failed")
    if not prepare:
        asm.label("deployed")


def _transfer(
    asm: Assembly,
    token: ChecksumAddress,
    transfer: Transfer,
    blob: int,
    reading: int,
    need: int,
) -> None:
    """Call `token` with the transfer's calldata (blob `blob`), given exactly its gas, or the
    gas left over if it has no bound (GAS reading `reading`): the token is warm, its balance
    read first. One that fails reverts with its revert data."""
    length = len(transfer.calldata)
    asm.push(length).push_data(blob).push(0).codecopy(length)
    asm.push(0).push(0).push(length).push(0).push(0).push_address(token)
    if transfer.gas is None:
        _left_over(asm, reading, need)
        asm.client_call(WARM_ACCOUNT_ACCESS)
    else:
        asm.push(transfer.gas).capped_call(WARM_ACCOUNT_ACCESS, transfer.gas)
    asm.op(Op.ISZERO).jumpi("call_failed")


def _left_over(asm: Assembly, index: int, need: int) -> None:
    """Push a CALL's gas, GAS - R - `need`, its R measured from this GAS reading:
    GasExhausted() if there isn't R + `need`. Checked against the same GAS reading: a GAS read
    again after the check could be under it, and the subtraction would wrap to a call given
    everything."""
    asm.reserve(index, need).gas_read(index).op(Op.DUP2, Op.DUP2, Op.LT).jumpi("gas_exhausted")
    asm.op(Op.SUB)


def _client_call(asm: Assembly, index: int, planned: PlannedCall, access: int, later: int) -> None:
    """Make the client's call, the `index`th (its data that blob, its GAS reading that
    reading), sending it its value, which the runner holds: its transaction's, passed on by
    the deployer. Bounded, it's given exactly its gas. Otherwise it's given all the gas left but
    R and `later`, what the calls after it need: GasExhausted() if there isn't R + `later` and
    what its own gas needs, the call then given less than its declared gas. The call's own
    revert data if it fails."""
    call = planned.call
    length = len(call.data)
    asm.push(length).push_data(index).push(0).codecopy(length)
    # CALL's arguments, last pushed first: gas, to, value, argsOffset, argsSize, retOffset,
    # retSize
    asm.push(0).push(0).push(length).push(0).push(call.value).push_address(call.to)
    if planned.bounded:
        asm.push(planned.gas).capped_call(access, planned.gas)
    else:
        # its gas, GAS - R - later, checked against the same GAS reading: a GAS read again
        # after the check could be under it, and the subtraction would wrap to a call given
        # everything. Stack: R + later, GAS; then R + later + need, compared with GAS
        asm.reserve(index, later).gas_read(index).push(call_need(planned.gas))
        asm.op(Op.DUP3, Op.ADD, Op.DUP2, Op.LT).jumpi("gas_exhausted")
        asm.op(Op.SUB).client_call(access)
    asm.op(Op.ISZERO).jumpi("call_failed")


def call_failed(asm: Assembly) -> None:
    """Revert with the failed call's revert data, as it is."""
    asm.label("call_failed")
    asm.op(Op.RETURNDATASIZE).push(0).push(0).op(Op.RETURNDATACOPY)
    asm.op(Op.RETURNDATASIZE).push(0).op(Op.REVERT)


def revert_with(asm: Assembly, label: str, selector: bytes) -> None:
    asm.label(label)
    asm.push(int.from_bytes(selector) << 224).push(0).op(Op.MSTORE)
    asm.push(4).push(0).op(Op.REVERT)
