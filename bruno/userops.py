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

from dataclasses import dataclass
from importlib.resources import files

from eth_abi.abi import decode, encode
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from .common import BrunoError
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

    def packed(self, verification_gas: int, call_gas: int, fee: int) -> tuple[object, ...]:
        """The op as the EntryPoint takes it, given these limits and both fees `fee`."""
        return (
            self.sender,
            self.nonce,
            self.init_code(),
            self.call_data,
            verification_gas.to_bytes(16) + call_gas.to_bytes(16),
            self.pre_verification_gas,
            fee.to_bytes(16) + fee.to_bytes(16),
            b"",
            self.signature,
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
        [_PACKED, "address", "bytes"],
        [op.packed(limit, limit, _SIMULATED_FEE), "0x" + "00" * 20, b""],
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
