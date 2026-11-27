"""The runner's deployer: Arachnid's deterministic deployment proxy, at one address on every
chain, with nothing of Bruno's to deploy. Kept behind this module (its address, its calldata,
its gas, what it answers) so that another deployer can take its place.

Calldata is a salt and the runner's initcode. It CREATE2s the initcode with the salt, passing
the transaction's value on, and answers the runner's address in 20 bytes; if the creation
fails it reverts with nothing, so a runner's own revert data doesn't come back.

The salt is Bruno's to choose, and it's a keyed hash (runner_salt): a submission's runner is at
a different address in every block it's tried for, and no one without the key can work that
address out before its transaction is seen. Once it is, anyone can create the same runner
there again, since the salt is only calldata: nothing may treat a runner's address as
authority.

     0  PUSH32 -32 CALLDATASIZE ADD                     n = the initcode's length
    35  PUSH1 0 DUP2 PUSH1 32 DUP3 CALLDATACOPY         memory[0:n] = initcode
    42  DUP1 CALLDATALOAD                               salt
    44  DUP3 DUP3 CALLVALUE CREATE2                     CREATE2(value, 0, n, salt)
    48  DUP1 ISZERO ISZERO PUSH1 57 JUMPI
    54  DUP2 DUP3 REVERT                                revert(0, 0)
    57  JUMPDEST DUP1 DUP3 MSTORE POP POP POP PUSH1 20 PUSH1 12 RETURN
"""

import asyncio
from fractions import Fraction
from math import ceil
from pathlib import Path

from eth_account.hdaccount import key_from_seed
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from .chain import Chain, lasting_fees, market_tip, quantity
from .common import BrunoError
from .config import ChainConfig, load
from .evm import EncodedCall, memory_gas
from .forks import NETWORK_NAMES, GasSchedule
from .transactions import sign, unsigned_size

# Arachnid's deterministic deployment proxy (github.com/Arachnid/deterministic-deployment-proxy)
ARACHNID: ChecksumAddress = to_checksum_address("0x4e59b44847b379578588920cA78FbF26c0B4956C")
DEPLOYER: ChecksumAddress = ARACHNID
RUNTIME = bytes.fromhex(
    "7fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffe03601600081602082378035"
    "828234f58015156039578182fd5b8082525050506014600cf3"
)
# the salt Bruno's own deployments through it use (the payee's forwarder): the same address on
# every chain
ARACHNID_SALT = bytes(32)

SALT_BYTES = 32
# what it answers: the address of what it created, alone
ANSWER_BYTES = 20
# a runner's salt where there's no submission to key it by (a quote, an estimate): every byte
# nonzero, as a transaction's gas limit prices a salt's whatever they are
STAND_IN_SALT = b"\xff" * SALT_BYTES
# the path the salts' key is derived on from the float's key, apart from the concurrency
# accounts' (m/44'/60'/0'/0/i)
SALT_KEY_PATH = "m/44'/60'/1'/0/0"


def arachnid_address(initcode: bytes) -> ChecksumAddress:
    """Where Arachnid's deployer puts `initcode`'s contract, at ARACHNID_SALT: the same on
    every chain."""
    return to_checksum_address(
        keccak(b"\xff" + bytes.fromhex(ARACHNID[2:]) + ARACHNID_SALT + keccak(initcode))[12:]
    )


def salt_key(float_account: LocalAccount) -> bytes:
    """The key runners' salts are hashed with: a private key derived from the float's on a path
    of its own, used for nothing else."""
    return key_from_seed(float_account.key, SALT_KEY_PATH)


def runner_salt(key: bytes, submission_id: bytes, block: int) -> bytes:
    """The salt of a submission's runner sent for block `block`: keccak256 of `key`, the
    submission's id and the block. Different for every submission and every block, and not to
    be worked out without the key."""
    return keccak(key + submission_id + block.to_bytes(32))


def runner_call(salt: bytes, initcode: bytes, value: int = 0) -> EncodedCall:
    """The call to the deployer that creates the runner `initcode` at `salt`, sending it
    `value`."""
    return EncodedCall(DEPLOYER, salt + initcode, value)


def priced_calldata(initcode: bytes) -> bytes:
    """A runner's calldata as its gas is priced: its salt's bytes nonzero, whatever they'll be,
    so a transaction's gas limit doesn't move with the salt."""
    return STAND_IN_SALT + initcode


# the runtime's gas, from its opcodes: before CREATE2, PUSH32 CALLDATASIZE ADD PUSH1 DUP2 PUSH1
# DUP3 CALLDATACOPY DUP1 CALLDATALOAD DUP3 DUP3 CALLVALUE (copying and memory aside); after
# it, DUP1 ISZERO ISZERO PUSH1 JUMPI JUMPDEST DUP1 DUP3 MSTORE POP POP POP PUSH1 PUSH1 RETURN
_BEFORE_CREATE2_GAS = 3 + 2 + 3 + 3 + 3 + 3 + 3 + 3 + 3 + 3 + 3 + 3 + 2
_AFTER_CREATE2_GAS = 3 + 3 + 3 + 3 + 10 + 1 + 3 + 3 + 3 + 2 + 2 + 2 + 3 + 3
# CALLDATACOPY's 3 a word; CREATE2's 6 a word of initcode (keccak) and 2 (EIP-3860)
_COPY_WORD_GAS = 3
_CREATE2_WORD_GAS = 6 + 2


def overhead_gas(initcode_length: int, schedule: GasSchedule) -> int:
    """The deployer's own gas for `initcode_length` bytes of initcode: everything but what
    CREATE2 forwards to the runner. Its answer is written to memory's first word, which the
    initcode has grown already unless it's empty."""
    words = ceil(initcode_length / 32)
    return (
        _BEFORE_CREATE2_GAS
        + _COPY_WORD_GAS * words
        + memory_gas(max(words, 1))
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
    *,
    sending: bool = False,
) -> int:
    """The gas limit a transaction to the deployer needs, under `schedule`, for a runner that
    may use `runner_gas`: intrinsic gas (the floor if higher, its salt priced as nonzero, a
    transaction `sending` ETH charged for that where the fork does), its
    `authorizations`' execution (`empty_authorities` of them for empty accounts), the
    deployer's own gas, and enough at CREATE2 that its 63/64 rule (EIP-150) still gives the
    runner `runner_gas` and leaves the deployer what it needs after."""
    overhead = overhead_gas(len(initcode), schedule) - _AFTER_CREATE2_GAS
    at_create2 = ceil((runner_gas + _AFTER_CREATE2_GAS) * 64 / 63)
    execution = (
        overhead + at_create2 + schedule.authorizations_gas(empty_authorities, authorizations)
    )
    intrinsic = schedule.intrinsic(
        priced_calldata(initcode), value=sending, authorizations=authorizations
    )
    return max(intrinsic.standard + execution, intrinsic.floor)


def runner_gas_within(
    initcode: bytes,
    gas_limit: int,
    schedule: GasSchedule,
    authorizations: int = 0,
    empty_authorities: int = 0,
    *,
    sending: bool = False,
) -> int:
    """The most gas a runner is given in a transaction to the deployer of `gas_limit`:
    transaction_gas's inverse."""
    overhead = overhead_gas(len(initcode), schedule) - _AFTER_CREATE2_GAS
    intrinsic = schedule.intrinsic(
        priced_calldata(initcode), value=sending, authorizations=authorizations
    )
    at_create2 = (
        gas_limit
        - intrinsic.standard
        - overhead
        - schedule.authorizations_gas(empty_authorities, authorizations)
    )
    return max(0, at_create2 * 63 // 64 - _AFTER_CREATE2_GAS)


class DeployerError(BrunoError):
    pass


async def deployed_code(chain: Chain, address: str, timeout: float) -> bytes:
    """`address`'s code once a deployment to it is in: up to `timeout` seconds for it to show.
    A load-balanced endpoint can answer the receipt from one node and the code from another
    still a block behind, which reads as no code at all."""
    for _ in range(int(timeout / chain.poll.seconds)):
        if code := await chain.code(address):
            return code
        await asyncio.sleep(chain.poll.seconds)
    return await chain.code(address)


# a deployment's max fee lasts this many blocks of rising base fee: it's sent to the public
# mempool and waited for
DEPLOY_BLOCKS = 5
# a sent deployment is waited for this many slots
DEPLOY_WAIT_SLOTS = 20
# its gas limit over its estimate: an estimate can be a little under what a transaction needs
DEPLOY_GAS_HEADROOM = Fraction(5, 4)


async def deploy_code(
    chain: Chain, wallet: LocalAccount, initcode: bytes, runtime: bytes, what: str
) -> bytes | None:
    """`initcode`'s contract (`what`) on `chain`, through Arachnid's at ARACHNID_SALT, sent from
    `wallet` unless it's there: its deployment's hash, or None if there was nothing to send.
    Raise unless what's there after is `runtime`, or if the wallet can't cover the deployment
    at its max fee."""
    if not await chain.code(ARACHNID):
        raise DeployerError(f"Arachnid's deployer isn't at {ARACHNID} on chain {chain.chain_id}")
    address = arachnid_address(initcode)
    calldata = ARACHNID_SALT + initcode
    sent = None
    if not await chain.code(address):
        latest = await chain.latest(await chain.op_stack())
        fees = lasting_fees(latest, market_tip(latest.tips), DEPLOY_BLOCKS)
        query = {"from": wallet.address, "to": ARACHNID, "data": "0x" + calldata.hex()}
        estimate = quantity(await chain.rpc("eth_estimateGas", [query]))
        gas = ceil(estimate * DEPLOY_GAS_HEADROOM)
        most = fees.cost(gas, unsigned_size(chain.chain_id, ARACHNID, calldata))
        held = await chain.balance(wallet.address)
        print(
            f">> deploying from {wallet.address}: at most {most / 10**18} ETH; it holds {held / 10**18}"
        )
        if held < most:
            raise DeployerError(f"{wallet.address} holds {held} wei, under the {most} it may cost")
        nonce = await chain.nonce(wallet.address)
        tx = sign(wallet, chain.chain_id, nonce, fees, gas, ARACHNID, calldata)
        receipt = await chain.send(tx, f"{what}'s deployment", DEPLOY_WAIT_SLOTS * chain.slot)
        paid = quantity(receipt.get("gasUsed")) * quantity(receipt.get("effectiveGasPrice"))
        print(f">> deployed: 0x{tx.hash.hex()}, {paid / 10**18} ETH")
        sent = tx.hash
    if await deployed_code(chain, address, DEPLOY_WAIT_SLOTS * chain.slot) != runtime:
        raise DeployerError(f"{address} doesn't hold {what}'s runtime")
    print(f">> {what} is at {address} on chain {chain.chain_id}")
    return sent


def network_chain(network: str, config: Path) -> tuple[int, ChainConfig]:
    """The chain `network` names, and its configuration in `config`: DeployerError if there's
    none."""
    chain_id = next((i for i, name in NETWORK_NAMES.items() if name == network), None)
    if chain_id is None:
        raise DeployerError(f"no network {network}: one of {', '.join(NETWORK_NAMES.values())}")
    chains = load(config).chains
    if chain_id not in chains:
        raise DeployerError(f"{config} names no chain {chain_id} ({network}): no nodes to use")
    return chain_id, chains[chain_id]
