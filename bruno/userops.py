"""UserOperations (ERC-4337, EntryPoint v0.7) a relay takes as calls that pay for themselves,
and their gas estimated as ERC-7769's eth_estimateUserOperationGas answers it.

An op's fees are zero: it needs no ETH and no deposit, and its own calls pay the relay's payee
(docs/development/plans/erc4337.md). It's estimated by running it through EntryPointSimulations
put at the EntryPoint's address by eth_call's state override: simulateHandleOp takes the
account's stub signature and reports it failed, in its validation data, rather than reverting.
Run with a fee of a wei (both fee fields), funded by a deposit for the account in the
EntryPoint's own storage (by the same override, its deposits mapping), its `paid` is the gas it
used. The account itself is left as it is: given ETH, an undeployed account would exist already,
and Amsterdam's state gas (EIP-8037) charges creating one. That less its pre-execution gas, and less EntryPoint v0.7's
penalty of a tenth on the call gas it leaves unused, is its execution; each limit is what was
used and a tenth more. Its fees, and preVerificationGas, don't enter the price: the relay
prices the call that runs it.

EntryPointSimulations is never deployed. Its code (entry_point_simulations_0_7.hex, 21,210
bytes, sha256 1344b913c91c8390385f5a17d2b1cc1643e4d457932a6911d9bf45f6ef9a2fa8) is
eth-infinitism's published build: @account-abstraction/contracts 0.7.0's
artifacts/EntryPointSimulations.json, the build whose EntryPoint.json is Ethereum's EntryPoint
v0.7 byte for byte but its SenderCreator immutable (checked 2026-10-28)."""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from importlib.resources import files

import msgspec
from eth_abi.abi import decode, encode
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from .common import BrunoError
from .evm import NATIVE_ASSET
from .net.jsonrpc import Call

ENTRY_POINT = to_checksum_address("0x0000000071727De22E5E9d8BAf0edAc6f37da032")
SIMULATIONS_CODE = files("bruno").joinpath("entry_point_simulations_0_7.hex").read_text().strip()

# a packed UserOperation, as EntryPoint v0.7 takes it
_PACKED = "(address,uint256,bytes,bytes,bytes32,uint256,bytes32,bytes,bytes)"
_SIMULATE_HANDLE_OP = keccak(text=f"simulateHandleOp({_PACKED},address,bytes)")[:4]
_EXECUTION_RESULT = ["(uint256,uint256,uint256,uint256,bool,bytes)"]
# the fee an op is simulated at: a wei, so what it pays is the gas it uses
_SIMULATED_FEE = 1
# the deposit the account is given in the EntryPoint to fund its simulation: ample for any
# limit at a wei; EntryPoint v0.7's deposits mapping is at slot 0, a deposit its entry's first
# word (found on anvil, 2026-10-29)
_SIMULATED_DEPOSIT = 10**18
_DEPOSITS_SLOT = 0
# EntryPoint v0.7's penalty on an op's unused call gas, in percent
_UNUSED_GAS_PENALTY = 10
# each limit is what's used and this much more, in percent: what the stub signature, and the
# state between the estimate and the op's run, may move
_MARGIN = 10


class InvalidUserOperationError(BrunoError):
    """A field of the user operation is the client's mistake: `field`, its JSON name."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field


@dataclass(frozen=True, slots=True)
class UserOperation:
    """An op for EntryPoint v0.7, as ERC-7769 has its fields: its account (`sender`), nonce,
    factory and data while the account is undeployed, call data, gas limits, fees, and
    signature (the account's stub, to estimate it)."""

    sender: ChecksumAddress
    nonce: int
    factory: ChecksumAddress | None
    factory_data: bytes
    call_data: bytes
    call_gas_limit: int
    verification_gas_limit: int
    pre_verification_gas: int
    max_fee_per_gas: int
    max_priority_fee_per_gas: int
    signature: bytes

    def init_code(self) -> bytes:
        """The factory and its data, packed: empty for a deployed account."""
        if self.factory is None:
            return b""
        return bytes.fromhex(self.factory[2:]) + self.factory_data

    def account_gas_limits(self) -> bytes:
        """Its verification and call gas limits, packed as one word."""
        return self.verification_gas_limit.to_bytes(16) + self.call_gas_limit.to_bytes(16)

    def gas_fees(self) -> bytes:
        """Its priority fee and its most fee, packed as one word."""
        return self.max_priority_fee_per_gas.to_bytes(16) + self.max_fee_per_gas.to_bytes(16)

    def packed(self) -> tuple[object, ...]:
        """The op as the EntryPoint takes it."""
        return (
            self.sender,
            self.nonce,
            self.init_code(),
            self.call_data,
            self.account_gas_limits(),
            self.pre_verification_gas,
            self.gas_fees(),
            b"",
            self.signature,
        )

    def simulated(self, limit: int) -> UserOperation:
        """The op as it's simulated to estimate it: its limits each `limit`, its fees a wei."""
        return replace(
            self,
            verification_gas_limit=limit,
            call_gas_limit=limit,
            max_fee_per_gas=_SIMULATED_FEE,
            max_priority_fee_per_gas=_SIMULATED_FEE,
        )


@dataclass(frozen=True, slots=True)
class GasEstimate:
    """An op's gas limits, as eth_estimateUserOperationGas answers: preVerificationGas zero,
    its fees zero; its verification and call limits each what it used and a margin."""

    pre_verification_gas: int
    verification_gas_limit: int
    call_gas_limit: int


def simulation(op: UserOperation, limit: int) -> Call:
    """The eth_call that runs `op` through EntryPointSimulations, at the latest block: its
    limits each `limit`, its fees a wei; by state override, the simulation's code at the
    EntryPoint's address, and a deposit for the account in its storage."""
    data = _SIMULATE_HANDLE_OP + encode(
        [_PACKED, "address", "bytes"], [op.simulated(limit).packed(), "0x" + "00" * 20, b""]
    )
    deposit = keccak(bytes(12) + bytes.fromhex(op.sender[2:]) + _DEPOSITS_SLOT.to_bytes(32))
    overrides = {
        ENTRY_POINT: {
            "code": SIMULATIONS_CODE,
            "stateDiff": {"0x" + deposit.hex(): "0x" + _SIMULATED_DEPOSIT.to_bytes(32).hex()},
        },
    }
    return Call("eth_call", [{"to": ENTRY_POINT, "data": "0x" + data.hex()}, "latest", overrides])


def estimated(answer: bytes, limit: int) -> GasEstimate:
    """simulateHandleOp's answer, its op's limits each `limit`, read as the op's estimate: its
    pre-execution gas, and its execution, `paid` less that and less the penalty on the call
    gas it left unused; each a margin more."""
    pre_op_gas, paid, _, _, _, _ = decode(_EXECUTION_RESULT, answer)[0]
    used = paid // _SIMULATED_FEE
    # paid = pre_op_gas + execution + (limit - execution) * penalty / 100, solved for execution
    execution = max(0, (used - pre_op_gas - limit * _UNUSED_GAS_PENALTY // 100))
    execution = execution * 100 // (100 - _UNUSED_GAS_PENALTY)
    return GasEstimate(
        pre_verification_gas=0,
        verification_gas_limit=pre_op_gas * (100 + _MARGIN) // 100,
        call_gas_limit=execution * (100 + _MARGIN) // 100,
    )


# what a send reads: the relay's payee's balances, by a helper put at an address no contract
# has (by override); the op simulated as given, the helper read after it; the handleOps call's
# gas, estimated
BALANCES = to_checksum_address(keccak(text="bruno: the payee's balances")[-20:])
_HANDLE_OPS = keccak(text=f"handleOps({_PACKED}[],address)")[:4]
_BALANCE_OF = keccak(text="balanceOf(address)")[:4]
# an account's validation data: its signature failed if its aggregator (the low 20 bytes) is 1
_SIGNATURE_FAILED = 1
_AGGREGATOR = (1 << 160) - 1
_USER_OPERATION_EVENT = keccak(
    text="UserOperationEvent(bytes32,address,address,uint256,bool,uint256,uint256)"
)
_REVERT_REASON = keccak(text="UserOperationRevertReason(bytes32,address,uint256,bytes)")
_BEFORE_EXECUTION = keccak(text="BeforeExecution()")


def user_operation_hash(op: UserOperation, chain_id: int) -> bytes:
    """The op's hash, as EntryPoint v0.7's getUserOpHash computes it: what ERC-7769 names it by."""
    fields = encode(
        ["address", "uint256", "bytes32", "bytes32", "bytes32", "uint256", "bytes32", "bytes32"],
        [
            op.sender,
            op.nonce,
            keccak(op.init_code()),
            keccak(op.call_data),
            op.account_gas_limits(),
            op.pre_verification_gas,
            op.gas_fees(),
            keccak(b""),
        ],
    )
    return keccak(
        encode(["bytes32", "address", "uint256"], [keccak(fields), ENTRY_POINT, chain_id])
    )


def handle_ops(op: UserOperation, beneficiary: ChecksumAddress) -> bytes:
    """The EntryPoint's handleOps calldata for `op` alone, `beneficiary` paid its fees (none)."""
    return _HANDLE_OPS + encode([f"{_PACKED}[]", "address"], [[op.packed()], beneficiary])


def balances_code(payee: ChecksumAddress, tokens: list[ChecksumAddress]) -> bytes:
    """A helper's runtime code returning `payee`'s balance of each of `tokens`, a word each, in
    order (ETH as ERC-7528's address): straight-line, each token's balanceOf a STATICCALL, its
    calldata built past the answers' words."""
    out = len(tokens) * 32
    payee_ = bytes.fromhex(payee[2:])
    code = bytearray()
    for i, token in enumerate(tokens):
        if token == NATIVE_ASSET:
            code += b"\x73" + payee_ + b"\x31"  # PUSH20 payee, BALANCE
            code += b"\x61" + (i * 32).to_bytes(2) + b"\x52"  # PUSH2 i*32, MSTORE
            continue
        code += b"\x63" + _BALANCE_OF + b"\x60\xe0\x1b"  # PUSH4 selector, PUSH1 224, SHL
        code += b"\x61" + out.to_bytes(2) + b"\x52"  # PUSH2 out, MSTORE
        code += b"\x73" + payee_ + b"\x61" + (out + 4).to_bytes(2) + b"\x52"  # its argument
        code += b"\x60\x20\x61" + (i * 32).to_bytes(2)  # retSize 32, retOffset i*32
        code += b"\x60\x24\x61" + out.to_bytes(2)  # argsSize 36, argsOffset out
        code += b"\x73" + bytes.fromhex(token[2:]) + b"\x5a\xfa\x50"  # token, GAS, STATICCALL, POP
    code += b"\x61" + out.to_bytes(2) + b"\x60\x00\xf3"  # RETURN(0, out)
    return bytes(code)


def sending(op: UserOperation, payee: ChecksumAddress, tokens: list[ChecksumAddress]) -> list[Call]:
    """What a send reads, in one batch: `payee`'s balances in `tokens`; the op simulated as given
    (its own limits, fees and signature), the balances read after it; and handleOps' gas."""
    helper = {BALANCES: {"code": "0x" + balances_code(payee, tokens).hex()}}
    data = _SIMULATE_HANDLE_OP + encode([_PACKED, "address", "bytes"], [op.packed(), BALANCES, b""])
    calls = handle_ops(op, payee)
    return [
        Call("eth_call", [{"to": BALANCES, "data": "0x"}, "latest", helper]),
        Call(
            "eth_call",
            [
                {"to": ENTRY_POINT, "data": "0x" + data.hex()},
                "latest",
                {**helper, ENTRY_POINT: {"code": SIMULATIONS_CODE}},
            ],
        ),
        Call("eth_estimateGas", [{"from": payee, "to": ENTRY_POINT, "data": "0x" + calls.hex()}]),
    ]


def paid(before: bytes, simulated: bytes, tokens: list[ChecksumAddress]) -> dict[str, int]:
    """What the op paid the payee in each of `tokens`, from its balances `before` and the
    simulation's answer (the balances read after it): InvalidUserOperationError if the account
    refused its signature."""
    _, _, validation, _, _, after = decode(_EXECUTION_RESULT, simulated)[0]
    if validation & _AGGREGATOR == _SIGNATURE_FAILED:
        raise InvalidUserOperationError("signature", "the account doesn't take it")
    words = len(tokens)
    old = decode(["uint256"] * words, before)
    new = decode(["uint256"] * words, after)
    return {token: max(0, n - o) for token, o, n in zip(tokens, old, new, strict=True)}


@dataclass(frozen=True, slots=True)
class UserOperationReceipt:
    """An op's receipt, as ERC-7769 has it: its UserOperationEvent's fields, the revert data
    the EntryPoint gave if it failed, the logs its execution emitted, and its transaction's
    receipt, both as the node gave them."""

    user_op_hash: bytes
    sender: ChecksumAddress
    nonce: int
    paymaster: ChecksumAddress
    actual_gas_cost: int
    actual_gas_used: int
    success: bool
    reason: bytes
    logs: list[dict[str, object]]
    receipt: dict[str, object]


class _Log(msgspec.Struct, frozen=True):
    """What's read of a log: its topics and data."""

    topics: list[str]
    data: str


def receipt_of(op_hash: bytes, receipt: Mapping[str, object]) -> UserOperationReceipt | None:
    """ERC-7769's receipt of the op `op_hash`, from its transaction's `receipt`: None if the
    transaction holds no UserOperationEvent of it. Its logs are those its execution emitted
    (after the EntryPoint's BeforeExecution, before its event); its reason, the revert data the
    EntryPoint gave, if it failed."""
    raw = msgspec.convert(receipt["logs"], list[dict[str, object]])
    logs = msgspec.convert(raw, list[_Log])
    topic = "0x" + op_hash.hex()
    event: tuple[int, _Log] | None = None
    reason: _Log | None = None
    start = 0
    for i, log in enumerate(logs):
        first = bytes.fromhex(log.topics[0][2:])
        if first == _BEFORE_EXECUTION:
            start = i + 1
        elif first == _USER_OPERATION_EVENT and log.topics[1] == topic:
            event = (i, log)
        elif first == _REVERT_REASON and log.topics[1] == topic:
            reason = log
    if event is None:
        return None
    index, log = event
    nonce, success, cost, used = decode(
        ["uint256", "bool", "uint256", "uint256"], bytes.fromhex(log.data[2:])
    )
    revert = b""
    if reason is not None:
        _, revert = decode(["uint256", "bytes"], bytes.fromhex(reason.data[2:]))
    return UserOperationReceipt(
        user_op_hash=op_hash,
        sender=to_checksum_address("0x" + log.topics[2][-40:]),
        nonce=nonce,
        paymaster=to_checksum_address("0x" + log.topics[3][-40:]),
        actual_gas_cost=cost,
        actual_gas_used=used,
        success=success,
        reason=revert,
        logs=raw[start:index],
        receipt=dict(receipt),
    )
