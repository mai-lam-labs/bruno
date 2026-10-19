"""The runner's CREATE2 deployer: ownerless, deployed through Arachnid's deterministic
deployer, so it has one address on every chain.

Calldata is the runner's initcode. The CREATE2 salt is keccak256(CALLER ‖ NUMBER): a runner's
address belongs to one caller in one block. On success it returns the runner's address,
ABI-encoded; on failure it reverts with the runner's revert data.

     0  CALLDATASIZE PUSH0 PUSH0 CALLDATACOPY           memory[0:n] = initcode
     4  CALLER CALLDATASIZE MSTORE                      memory[n:n+32] = caller
     7  NUMBER PUSH1 32 CALLDATASIZE ADD MSTORE         memory[n+32:n+64] = block number
    13  PUSH1 64 CALLDATASIZE KECCAK256                 salt
    17  CALLDATASIZE PUSH0 PUSH0 CREATE2                CREATE2(0, 0, n, salt)
    21  DUP1 PUSH1 32 JUMPI
    25  RETURNDATASIZE PUSH0 PUSH0 RETURNDATACOPY RETURNDATASIZE PUSH0 REVERT
    32  JUMPDEST PUSH0 MSTORE PUSH1 32 PUSH0 RETURN
"""

from math import ceil

from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from .evm import memory_gas
from .forks import GasSchedule

# Arachnid's deterministic deployment proxy: calldata `salt ‖ initcode`, CREATE2s it
# (github.com/Arachnid/deterministic-deployment-proxy), at this address on every chain
ARACHNID: ChecksumAddress = to_checksum_address("0x4e59b44847b379578588920cA78FbF26c0B4956C")

RUNTIME = bytes.fromhex(
    "365f5f3733365243602036015260403620365f5ff5806020573d5f5f3e3d5ffd5b5f5260205ff3"
)
# PUSH1 39  DUP1  PUSH1 9  PUSH0  CODECOPY  PUSH0  RETURN: the runtime, which follows
INITCODE = bytes.fromhex("60278060095f395ff3") + RUNTIME
ARACHNID_SALT = bytes(32)
DEPLOYER: ChecksumAddress = to_checksum_address(
    keccak(b"\xff" + bytes.fromhex(ARACHNID[2:]) + ARACHNID_SALT + keccak(INITCODE))[12:]
)

# the runtime's gas, from its opcodes: before CREATE2, CALLDATASIZE PUSH0 PUSH0 CALLDATACOPY
# CALLER CALLDATASIZE MSTORE NUMBER PUSH1 CALLDATASIZE ADD MSTORE PUSH1 CALLDATASIZE KECCAK256
# CALLDATASIZE PUSH0 PUSH0 (copying, memory and hashing aside); after it, DUP1 PUSH1 JUMPI
# JUMPDEST PUSH0 MSTORE PUSH1 PUSH0 RETURN
_BEFORE_CREATE2_GAS = 2 + 2 + 2 + 3 + 2 + 2 + 3 + 2 + 3 + 2 + 3 + 3 + 3 + 2 + 30 + 2 + 2 + 2
_AFTER_CREATE2_GAS = 3 + 3 + 10 + 1 + 2 + 3 + 3 + 2
# the salt's preimage, caller and block number: two words, hashed at 6 a word
_SALT_WORDS = 2
_KECCAK_WORD_GAS = 6
# CALLDATACOPY's 3 a word; CREATE2's 6 a word of initcode (keccak) and 2 (EIP-3860)
_COPY_WORD_GAS = 3
_CREATE2_WORD_GAS = 6 + 2


def arachnid_calldata() -> bytes:
    """The calldata that deploys the deployer through Arachnid's."""
    return ARACHNID_SALT + INITCODE


def overhead_gas(initcode_length: int, schedule: GasSchedule) -> int:
    """The deployer's own gas for `initcode_length` bytes of initcode: everything but what
    CREATE2 forwards to the runner."""
    words = ceil(initcode_length / 32)
    return (
        _BEFORE_CREATE2_GAS
        + _COPY_WORD_GAS * words
        + _KECCAK_WORD_GAS * _SALT_WORDS
        + memory_gas(ceil((initcode_length + 32 * _SALT_WORDS) / 32))
        + schedule.create
        + _CREATE2_WORD_GAS * words
        + _AFTER_CREATE2_GAS
    )


def transaction_gas(
    initcode: bytes,
    runner_gas: int,
    schedule: GasSchedule,
    authorizations: int = 0,
    empty_authorities: int = 0,
) -> int:
    """The gas limit a transaction to the deployer needs, under `schedule`, for a runner that
    may use `runner_gas`: intrinsic gas (the floor if higher), its `authorizations`'
    execution (`empty_authorities` of them for empty accounts), the deployer's own gas, and
    enough at CREATE2 that its 63/64 rule (EIP-150) still gives the runner `runner_gas` and
    leaves the deployer what it needs after."""
    overhead = overhead_gas(len(initcode), schedule) - _AFTER_CREATE2_GAS
    at_create2 = ceil((runner_gas + _AFTER_CREATE2_GAS) * 64 / 63)
    execution = overhead + at_create2 + schedule.authorizations(empty_authorities, authorizations)
    standard, floor = schedule.intrinsic(initcode, authorizations=authorizations)
    return max(standard + execution, floor)
