"""UserOperations (ERC-4337, EntryPoint v0.7 and v0.8) a relay takes as calls that pay for
themselves, and their gas estimated as ERC-7769's eth_estimateUserOperationGas answers it.

An op pays the relay one of two ways (docs/development/plans/erc4337.md). Its fees zero, its own
calls pay the relay's payee, in a token it takes: it needs no ETH and no deposit. Its fees above
zero, it pays as at any bundler: the EntryPoint pays its gas, at its price, to the beneficiary,
the relay's payee, in ETH, from the account's deposit or what it sends the EntryPoint.

It's estimated by running it through EntryPointSimulations put at the EntryPoint's address by
eth_call's state override: simulateHandleOp takes the account's stub signature and reports it
failed, in its validation data, rather than reverting. Run with a fee of a wei (both fee
fields), its `paid` is the gas it used. An op paying by its calls is funded by a deposit for the
account in the EntryPoint's own storage (by the same override, its deposits mapping); one paying
by its fees isn't, so an account that can't pay its prefund fails. The account itself is left
as it is: given ETH, an undeployed account would exist already, and Amsterdam's state gas
(EIP-8037) charges creating one. That less its pre-execution gas, and less EntryPoint v0.7's
penalty of a tenth on the call gas it leaves unused (v0.8: only if over 40,000 is), is its
execution; each limit is what was used and a tenth more.

An EIP-7702 account's op (v0.8) names the marker 0x7702 as its factory, and may bring the
account's authorization (ERC-7769's eip7702Auth), sent in the relay's transaction; simulated,
the account is given the delegation's code by state override, its signature unchecked.

EntryPointSimulations is never deployed. Its code for each version is eth-infinitism's
published build, @account-abstraction/contracts' artifacts/EntryPointSimulations.json, the
build whose EntryPoint.json is Ethereum's EntryPoint byte for byte but its immutables:
entry_point_simulations_0_7.hex (0.7.0; 21,210 bytes, sha256
1344b913c91c8390385f5a17d2b1cc1643e4d457932a6911d9bf45f6ef9a2fa8; checked 2026-10-28) and
entry_point_simulations_0_8.hex (0.8.0; 23,785 bytes, sha256
53169f31b6801ef92f9de9ee598c0ba15d8765b60774bba1fb6ab52f833fec55; checked 2026-10-29)."""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from importlib.resources import files

import msgspec
from eth_abi.abi import decode, encode
from eth_account.datastructures import SignedSetCodeAuthorization
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from .common import BrunoError
from .evm import NATIVE_ASSET
from .net.jsonrpc import Call


@dataclass(frozen=True, slots=True)
class EntryPoint:
    """An EntryPoint version ops are taken for: its `address`, its EntryPointSimulations'
    `simulations_code` (put at its address to estimate an op), how much call gas an op may leave
    unused before it's charged a tenth of it (`penalty_threshold`), and whether an op's hash is
    EIP-712's (`eip712`, v0.8) or v0.7's."""

    version: str
    address: ChecksumAddress
    simulations_code: str
    penalty_threshold: int
    eip712: bool


def _simulations(version: str) -> str:
    name = f"entry_point_simulations_{version.replace('.', '_')}.hex"
    return files("bruno").joinpath(name).read_text().strip()


ENTRY_POINT_0_7 = EntryPoint(
    "0.7",
    to_checksum_address("0x0000000071727De22E5E9d8BAf0edAc6f37da032"),
    _simulations("0.7"),
    0,
    eip712=False,
)
ENTRY_POINT_0_8 = EntryPoint(
    "0.8",
    to_checksum_address("0x4337084D9E255Ff0702461CF8895CE9E3b5Ff108"),
    _simulations("0.8"),
    40_000,
    eip712=True,
)
# those taken, the preferred first (eth_supportedEntryPoints)
ENTRY_POINTS = (ENTRY_POINT_0_8, ENTRY_POINT_0_7)
# an EIP-7702 account's factory (v0.8): the marker 0x7702, padded to an address as its init code
# packs it, whatever factory data follows
EIP7702_MARKER = to_checksum_address("0x7702" + "00" * 18)
_DELEGATION_PREFIX = bytes.fromhex("ef0100")

# a packed UserOperation, as the EntryPoint takes it
_PACKED = "(address,uint256,bytes,bytes,bytes32,uint256,bytes32,bytes,bytes)"
# EntryPoint v0.8's EIP-712 hash of one
_DOMAIN_TYPE = keccak(
    text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)
_PACKED_USER_OPERATION_TYPE = keccak(
    text="PackedUserOperation(address sender,uint256 nonce,bytes initCode,bytes callData,"
    "bytes32 accountGasLimits,uint256 preVerificationGas,bytes32 gasFees,bytes paymasterAndData)"
)
_SIMULATE_HANDLE_OP = keccak(text=f"simulateHandleOp({_PACKED},address,bytes)")[:4]
_EXECUTION_RESULT = ["(uint256,uint256,uint256,uint256,bool,bytes)"]
# the fee an op is simulated at: a wei, so what it pays is the gas it uses
_SIMULATED_FEE = 1
# the deposit the account is given in the EntryPoint to fund its simulation: ample for any
# limit at a wei; both versions' deposits mapping is at slot 0, a deposit its entry's first
# word (found on anvil, 2026-10-29)
_SIMULATED_DEPOSIT = 10**18
_DEPOSITS_SLOT = 0
# the EntryPoint's penalty on an op's unused call gas, in percent
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
class FeePayment:
    """What an op paying by its fees pays the beneficiary: the `gas` it's charged, at its price,
    from its two fees, as EntryPoint v0.7's getUserOpGasPrice has it."""

    gas: int
    max_fee_per_gas: int
    max_priority_fee_per_gas: int

    def price(self, base_fee: int) -> int:
        """Its price a gas in a block of `base_fee`: its max fee if that's its priority fee
        too, whatever the base fee; else the base fee and its priority fee, at most its max
        fee."""
        if self.max_fee_per_gas == self.max_priority_fee_per_gas:
            return self.max_fee_per_gas
        return min(self.max_fee_per_gas, self.max_priority_fee_per_gas + base_fee)

    def at(self, base_fee: int) -> int:
        """What it pays in a block of `base_fee`: at a base fee of 0, what it pays in any."""
        return self.gas * self.price(base_fee)


@dataclass(frozen=True, slots=True)
class UserOperation:
    """An op for `entry_point`, as ERC-7769 has its fields: its account (`sender`), nonce,
    factory and data while the account is undeployed (an EIP-7702 account's factory
    EIP7702_MARKER), call data, gas limits, fees, and signature (the account's stub, to
    estimate it); and the account's EIP-7702 `authorization`, if it brings one."""

    entry_point: EntryPoint
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
    authorization: SignedSetCodeAuthorization | None = None

    def init_code(self) -> bytes:
        """The factory and its data, packed: empty for a deployed account."""
        if self.factory is None:
            return b""
        return bytes.fromhex(self.factory[2:]) + self.factory_data

    @property
    def eip7702(self) -> bool:
        """Whether its account is an EIP-7702 account, its factory the marker."""
        return self.factory == EIP7702_MARKER

    def overrides(self) -> dict[str, object]:
        """What a simulation of it overrides of its account: given its authorization's
        delegation as code, if it brings one."""
        if self.authorization is None:
            return {}
        code = _DELEGATION_PREFIX + self.authorization.address
        return {self.sender: {"code": "0x" + code.hex()}}

    def warmed(self) -> list[dict[str, object]]:
        """The access list a run of it as it's sent is given: its account, if it brings an
        authorization, which the transaction carrying it warms (EIP-7702)."""
        if self.authorization is None:
            return []
        return [{"address": self.sender, "storageKeys": []}]

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

    @property
    def pays_by_fees(self) -> bool:
        """Whether the op pays the relay by its fees, in ETH through the EntryPoint (a max fee
        above zero), rather than by its calls."""
        return self.max_fee_per_gas > 0

    def fee_payment(self, gas: int) -> FeePayment:
        """What the op pays when charged `gas`, at its fees."""
        return FeePayment(gas, self.max_fee_per_gas, self.max_priority_fee_per_gas)

    def gas_price(self, base_fee: int) -> int:
        """What the op pays a gas in a block of `base_fee` (FeePayment.price)."""
        return self.fee_payment(0).price(base_fee)

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
    """The eth_call that runs `op` through its EntryPoint's simulations, at the latest block:
    its limits each `limit`, its fees a wei; by state override, the simulation's code at the
    EntryPoint's address, for an op paying by its calls a deposit for the account in its
    storage, and the account's delegation if the op brings an authorization."""
    data = _SIMULATE_HANDLE_OP + encode(
        [_PACKED, "address", "bytes"], [op.simulated(limit).packed(), "0x" + "00" * 20, b""]
    )
    entry_point = op.entry_point
    override: dict[str, object] = {"code": entry_point.simulations_code}
    if not op.pays_by_fees:
        deposit = keccak(bytes(12) + bytes.fromhex(op.sender[2:]) + _DEPOSITS_SLOT.to_bytes(32))
        override["stateDiff"] = {"0x" + deposit.hex(): "0x" + _SIMULATED_DEPOSIT.to_bytes(32).hex()}
    overrides = {entry_point.address: override, **op.overrides()}
    return Call(
        "eth_call", [{"to": entry_point.address, "data": "0x" + data.hex()}, "latest", overrides]
    )


def _used(answer: bytes, limit: int, entry_point: EntryPoint) -> tuple[int, int]:
    """simulateHandleOp's answer, its op's limits each `limit`: the op's pre-execution gas, and
    its execution, `paid` less that and less the penalty on the call gas it left unused, if
    `entry_point` charged one."""
    pre_op_gas, paid, _, _, _, _ = decode(_EXECUTION_RESULT, answer)[0]
    used = paid // _SIMULATED_FEE - pre_op_gas
    # used = execution + (limit - execution) * penalty / 100, solved for execution
    penalised = max(0, used - limit * _UNUSED_GAS_PENALTY // 100)
    execution = penalised * 100 // (100 - _UNUSED_GAS_PENALTY)
    if limit <= execution + entry_point.penalty_threshold:
        execution = max(0, used)
    return pre_op_gas, execution


def estimated(answer: bytes, limit: int, entry_point: EntryPoint) -> GasEstimate:
    """simulateHandleOp's answer, its op's limits each `limit`, read as the op's estimate: its
    pre-execution gas and its execution, each a margin more; preVerificationGas zero (an op
    paying by its fees is given the rest of the gas it's charged by the relay, with_overhead)."""
    pre_op_gas, execution = _used(answer, limit, entry_point)
    return GasEstimate(
        pre_verification_gas=0,
        verification_gas_limit=pre_op_gas * (100 + _MARGIN) // 100,
        call_gas_limit=execution * (100 + _MARGIN) // 100,
    )


def with_overhead(
    estimate: GasEstimate, answer: bytes, limit: int, entry_point: EntryPoint, charged: int
) -> GasEstimate:
    """`estimate`, of an op paying by its fees, its preVerificationGas the rest of the gas it's
    to be `charged`, beyond what it used (in `answer`, its limits each `limit`): what the
    EntryPoint charges it is then that gas, at its price."""
    pre_op_gas, execution = _used(answer, limit, entry_point)
    return replace(estimate, pre_verification_gas=max(0, charged - pre_op_gas - execution))


# what a send reads: the relay's payee's balances, by a helper put at an address no contract
# has (by override); the op run by the EntryPoint's own handleOps, from the helper, which then
# reads them again, twice: at a base fee of 0, as eth_call runs, and at the node's, the call
# given a fee by an account given ETH (by override); the handleOps call's gas, estimated
BALANCES = to_checksum_address(keccak(text="bruno: the payee's balances")[-20:])
_CALLER = to_checksum_address(keccak(text="bruno: a send's caller")[-20:])
# the fee the second run is given, over any base fee, and what its caller is given to pay it
_RUN_FEE = 10**15
_CALLER_BALANCE = 2**128
_HANDLE_OPS = keccak(text=f"handleOps({_PACKED}[],address)")[:4]
_BALANCE_OF = keccak(text="balanceOf(address)")[:4]
# the EntryPoint's error for an op it refuses, and its reason when the account refuses the
# op's signature
_FAILED_OP = keccak(text="FailedOp(uint256,string)")[:4]
_SIGNATURE_ERROR = "AA24 signature error"
# where the helper's balances begin, after its call to the EntryPoint: a JUMPDEST
_BALANCES_START = 58
_USER_OPERATION_EVENT = keccak(
    text="UserOperationEvent(bytes32,address,address,uint256,bool,uint256,uint256)"
)
_REVERT_REASON = keccak(text="UserOperationRevertReason(bytes32,address,uint256,bytes)")
_BEFORE_EXECUTION = keccak(text="BeforeExecution()")


def user_operation_hash(op: UserOperation, chain_id: int, delegate: bytes | None = None) -> bytes:
    """The op's hash, as its EntryPoint's getUserOpHash computes it: what ERC-7769 names it by.
    An EIP-7702 account's (v0.8) hashes its `delegate` (and its factory data) as its init code:
    its authorization's address, else what its code delegates to."""
    init_code = op.init_code()
    if op.eip7702:
        if delegate is None:
            raise ValueError("an EIP-7702 account's op is hashed with its delegate")
        init_code = delegate + op.factory_data
    types = ["address", "uint256", "bytes32", "bytes32", "bytes32", "uint256", "bytes32", "bytes32"]
    values: list[object] = [
        op.sender,
        op.nonce,
        keccak(init_code),
        keccak(op.call_data),
        op.account_gas_limits(),
        op.pre_verification_gas,
        op.gas_fees(),
        keccak(b""),
    ]
    entry_point = op.entry_point
    if not entry_point.eip712:
        fields = keccak(encode(types, values))
        return keccak(
            encode(["bytes32", "address", "uint256"], [fields, entry_point.address, chain_id])
        )
    struct = keccak(encode(["bytes32", *types], [_PACKED_USER_OPERATION_TYPE, *values]))
    domain = keccak(
        encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
            [_DOMAIN_TYPE, keccak(text="ERC4337"), keccak(text="1"), chain_id, entry_point.address],
        )
    )
    return keccak(b"\x19\x01" + domain + struct)


def delegate_of(code: bytes) -> bytes | None:
    """What an account's `code` delegates to, if it's an EIP-7702 delegation."""
    if len(code) != 23 or code[:3] != _DELEGATION_PREFIX:
        return None
    return code[3:]


def handle_ops(op: UserOperation, beneficiary: ChecksumAddress) -> bytes:
    """The EntryPoint's handleOps calldata for `op` alone, `beneficiary` paid its fees (none)."""
    return _HANDLE_OPS + encode([f"{_PACKED}[]", "address"], [[op.packed()], beneficiary])


def balances_code(
    entry_point: EntryPoint, payee: ChecksumAddress, tokens: list[ChecksumAddress]
) -> bytes:
    """A helper's runtime code returning `payee`'s balance of each of `tokens`, a word each, in
    order (ETH as ERC-7528's address), then the block's base fee. Called with data, it first
    calls `entry_point` with that data, reverting with its revert data if that reverts. Each
    token's balanceOf a STATICCALL, its calldata built past the answers' words."""
    out = (len(tokens) + 1) * 32
    payee_ = bytes.fromhex(payee[2:])
    code = bytearray()
    start = _BALANCES_START.to_bytes(2)
    # no data: straight to the balances
    code += b"\x36\x15\x61" + start + b"\x57"  # CALLDATASIZE, ISZERO, PUSH2 start, JUMPI
    code += b"\x36\x60\x00\x60\x00\x37"  # CALLDATACOPY(0, 0, CALLDATASIZE)
    # then the EntryPoint called with it all: GAS, no value, no return data kept
    code += (
        b"\x60\x00\x60\x00\x36\x60\x00\x60\x00\x73"
        + bytes.fromhex(entry_point.address[2:])
        + b"\x5a\xf1"
    )
    code += b"\x61" + start + b"\x57"  # PUSH2 start, JUMPI: on to the balances if it succeeded
    code += b"\x3d\x60\x00\x60\x00\x3e\x3d\x60\x00\xfd"  # else REVERT with its revert data
    code += b"\x5b"  # JUMPDEST, at _BALANCES_START
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
    code += b"\x48\x61" + (len(tokens) * 32).to_bytes(2) + b"\x52"  # BASEFEE, its word
    code += b"\x61" + out.to_bytes(2) + b"\x60\x00\xf3"  # RETURN(0, out)
    return bytes(code)


def sending(op: UserOperation, payee: ChecksumAddress, tokens: list[ChecksumAddress]) -> list[Call]:
    """What a send reads, in one batch: `payee`'s balances in `tokens`; the op run by the
    EntryPoint's handleOps as it will be sent (its own limits, fees and signature, the payee
    its beneficiary), the balances read after it, at a base fee of 0 and again at the node's
    (the gas an op is charged moves a little with the base fee: the EntryPoint's price is a
    min); and handleOps' gas. The account given its authorization's delegation by state
    override, if the op brings one, and warmed as the transaction carrying it warms it."""
    entry_point = op.entry_point
    accounts = op.overrides()
    warmed = {"accessList": op.warmed()} if accounts else {}
    helper = {BALANCES: {"code": "0x" + balances_code(entry_point, payee, tokens).hex()}}
    calls = "0x" + handle_ops(op, payee).hex()
    priced = {
        "from": _CALLER,
        "to": BALANCES,
        "data": calls,
        "maxFeePerGas": hex(_RUN_FEE),
        "maxPriorityFeePerGas": "0x0",
        **warmed,
    }
    caller = {_CALLER: {"balance": hex(_CALLER_BALANCE)}}
    run = {"from": payee, "to": entry_point.address, "data": calls}
    return [
        Call("eth_call", [{"to": BALANCES, "data": "0x"}, "latest", helper]),
        Call(
            "eth_call",
            [{"to": BALANCES, "data": calls, **warmed}, "latest", {**helper, **accounts}],
        ),
        Call("eth_call", [priced, "latest", {**helper, **accounts, **caller}]),
        Call("eth_estimateGas", [run, "latest", accounts] if accounts else [run]),
    ]


def refusal(revert: bytes) -> InvalidUserOperationError | None:
    """The client's mistake an EntryPoint's revert names, if it's one: the account refusing the
    op's signature."""
    if len(revert) < 4 + 3 * 32 or revert[:4] != _FAILED_OP:
        return None
    _, reason = decode(["uint256", "string"], revert[4:])
    if reason != _SIGNATURE_ERROR:
        return None
    return InvalidUserOperationError("signature", "the account doesn't take it")


def paid(before: bytes, after: bytes, tokens: list[ChecksumAddress]) -> dict[str, int]:
    """What the op paid the payee in each of `tokens`, from its balances `before` the op and
    `after` it."""
    words = len(tokens)
    old = decode(["uint256"] * words, before[: words * 32])
    new = decode(["uint256"] * words, after[: words * 32])
    return {token: max(0, n - o) for token, o, n in zip(tokens, old, new, strict=True)}


def fees_paid(op: UserOperation, runs: list[tuple[int, bytes]], tokens: int) -> FeePayment:
    """What `op`, paying by its fees, pays the payee: the least gas it was charged in `runs`
    (each the ETH it paid, at its price under that run's base fee, the last word read after
    it, past its `tokens` balances), at its fees."""
    gas: list[int] = []
    for eth, after in runs:
        (run_base_fee,) = decode(["uint256"], after[tokens * 32 : (tokens + 1) * 32])
        gas.append(eth // op.gas_price(run_base_fee))
    return op.fee_payment(min(gas))


@dataclass(frozen=True, slots=True)
class UserOperationReceipt:
    """An op's receipt, as ERC-7769 has it: its UserOperationEvent's fields, the revert data
    the EntryPoint gave if it failed, the logs its execution emitted, and its transaction's
    receipt, both as the node gave them."""

    user_op_hash: bytes
    entry_point: ChecksumAddress
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
    """What's read of a log: its address, topics and data."""

    address: str
    topics: list[str]
    data: str


def receipt_of(op_hash: bytes, receipt: Mapping[str, object]) -> UserOperationReceipt | None:
    """ERC-7769's receipt of the op `op_hash`, from its transaction's `receipt`: None if the
    transaction holds no UserOperationEvent of it from an EntryPoint taken. Its logs are those its
    execution emitted
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
        elif first == _USER_OPERATION_EVENT and log.topics[1] == topic and _taken(log.address):
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
        entry_point=to_checksum_address(log.address),
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


def _taken(address: str) -> bool:
    return any(e.address.lower() == address.lower() for e in ENTRY_POINTS)


def entry_point_at(address: ChecksumAddress) -> EntryPoint:
    """The EntryPoint taken at `address`: InvalidUserOperationError if there's none."""
    for entry_point in ENTRY_POINTS:
        if entry_point.address == address:
            return entry_point
    taken = ", ".join(f"v{e.version} {e.address}" for e in ENTRY_POINTS)
    raise InvalidUserOperationError("entryPoint", f"only {taken}")
