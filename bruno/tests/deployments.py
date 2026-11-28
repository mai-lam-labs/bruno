"""Deploying runners on the local chain, as the service's transactions would: simulated with
a plain eth_call on the pending block first, then sent, and mined as simulated. The deployer
reverts with nothing where a runner fails, so why one failed is read from a second eth_call,
the deployer's code overridden for it by code that passes a creation's revert data up."""

from collections.abc import Sequence
from dataclasses import dataclass, replace

import httpx
from eth_account.datastructures import SignedSetCodeAuthorization
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from bruno.chain import data_of
from bruno.deployer import DEPLOYER, runner_call, transaction_gas
from bruno.eip3009 import measuring as measuring_transfer
from bruno.evm import EncodedCall
from bruno.forks import GasSchedule
from bruno.jsonrpc import JsonRpcError, call
from bruno.runner import Erc20, PlannedCall, Runner, call_need
from bruno.tests.localchain import USDC, USDC_DOMAIN, LocalChain, LocalChainError, LocalToken
from bruno.tokens import erc20, measuring
from bruno.transactions import authorization_json

# the gas the tests' client calls are given between them: a choice, room for a signed batch
# that creates a few storage slots (~110,000 each under Amsterdam's state gas)
CLIENT_GAS = 1_000_000


def alone(call: EncodedCall, gas: int = CLIENT_GAS) -> tuple[PlannedCall, ...]:
    """A runner's calls of one: `call`, given at least `gas`."""
    return (PlannedCall(call, gas),)


# a deployer that passes a failed creation's revert data up, where Arachnid's reverts with
# nothing: put at the deployer's address by a state override, only to read why a runner failed.
# Its calldata is the initcode alone, and it salts by its caller and the block.
#   CALLDATASIZE PUSH0 PUSH0 CALLDATACOPY CALLER CALLDATASIZE MSTORE NUMBER PUSH1 32
#   CALLDATASIZE ADD MSTORE PUSH1 64 CALLDATASIZE KECCAK256 CALLDATASIZE PUSH0 PUSH0 CREATE2
#   DUP1 PUSH1 32 JUMPI RETURNDATASIZE PUSH0 PUSH0 RETURNDATACOPY RETURNDATASIZE PUSH0 REVERT
#   JUMPDEST PUSH0 MSTORE PUSH1 32 PUSH0 RETURN
REASONING = bytes.fromhex(
    "365f5f3733365243602036015260403620365f5ff5806020573d5f5f3e3d5ffd5b5f5260205ff3"
)
# the most gas the tests' relays let a transaction use, everything in it: the default
# configuration's
MAX_GAS = 2_000_000
# anvil's dev account #0 sends the deployments, as a concurrency account would
SENDER_ACCOUNT = 0


@dataclass(frozen=True, slots=True)
class Deployment:
    """A runner's deployment: whether it succeeded, and what the deployer returned (the
    runner's address) or, failing, what the runner reverts with (read through REASONING)."""

    succeeded: bool
    output: bytes


async def deploy(
    chain: LocalChain,
    runner: Runner,
    schedule: GasSchedule,
    authorizations: Sequence[SignedSetCodeAuthorization] = (),
    empty_authorities: int = 0,
    calls_gas: int = call_need(CLIENT_GAS),
    value: int = 0,
) -> Deployment:
    """Simulate the deployment with eth_call, then send it (a type-4 transaction with
    `authorizations`, `empty_authorities` of them for empty accounts), at the gas the runner's
    bounds and `schedule` say it needs for its calls not bounded to have `calls_gas`
    (runner.calls_need; by default, one call given CLIENT_GAS), carrying `value`, the ETH its
    calls send."""
    gas = transaction_gas(
        runner.initcode,
        runner.overhead_gas + calls_gas,
        schedule,
        len(authorizations),
        empty_authorities,
        sending=value > 0,
    )
    creation = runner_call(keccak(runner.initcode), runner.initcode, value)
    sender = await chain.account(SENDER_ACCOUNT)
    tx: dict[str, object] = {
        "from": sender,
        "to": creation.to,
        "data": "0x" + creation.data.hex(),
        "gas": hex(gas),
    }
    if value:
        tx["value"] = hex(value)
    if authorizations:
        tx["authorizationList"] = [authorization_json(a) for a in authorizations]
    async with httpx.AsyncClient() as client:
        try:
            output = data_of(await call(client, chain.url, "eth_call", [tx, "pending"]))
            simulated = Deployment(succeeded=True, output=output)
        except JsonRpcError as through:
            assert through.data in (None, "0x"), through.data  # the deployer says nothing
            reasoning = {**tx, "data": "0x" + runner.initcode.hex()}
            override = {DEPLOYER: {"code": "0x" + REASONING.hex()}}
            try:
                await call(client, chain.url, "eth_call", [reasoning, "pending", override])
            except JsonRpcError as e:
                revert = e.data if isinstance(e.data, str) else "0x"
                simulated = Deployment(succeeded=False, output=bytes.fromhex(revert[2:]))
            else:
                raise AssertionError("a runner that fails through the deployer and not this")
    receipt = await chain.receipt(await chain.rpc("eth_sendTransaction", [tx]))
    assert (receipt["status"] == "0x1") == simulated.succeeded
    return simulated


async def payment_token(
    chain: LocalChain, token: LocalToken, holder: str, schedule: GasSchedule
) -> Erc20:
    holder = to_checksum_address(holder)
    call = measuring(token.address, holder)
    estimate = int(await chain.rpc(call.method, list(call.params)), 16)
    return erc20(token.address, holder, estimate, schedule)


async def measured_exactly(
    chain: LocalChain, payee: ChecksumAddress, schedule: GasSchedule
) -> Erc20:
    """USDC as the relay measures it, its zero-value transfer's gas the least a transaction of
    it succeeds with, found by bisection: a node's exact estimate."""
    usdc = await payment_token(chain, USDC, payee, schedule)
    reading, calldata = measuring_transfer(USDC_DOMAIN, await chain.chain_id(), USDC.address, payee)
    (transaction, *_) = reading.params
    assert isinstance(transaction, dict)

    async def succeeds(gas: int) -> bool:
        try:
            await chain.rpc("eth_call", [{**transaction, "gas": hex(gas)}, "latest"])
        except LocalChainError:
            return False
        return True

    low, high = 0, int(await chain.rpc(reading.method, list(reading.params)), 16)
    while high - low > 1:
        middle = (low + high) // 2
        low, high = (low, middle) if await succeeds(middle) else (middle, high)
    return replace(usdc, transfer_gas=high - schedule.intrinsic(calldata).standard)


def transfer(token: LocalToken, to: str, amount: int) -> EncodedCall:
    return EncodedCall(
        token.address,
        LocalChain.call_bytes("transfer(address,uint256)", ["address", "uint256"], [to, amount]),
    )


def approve(token: LocalToken, spender: str, amount: int) -> EncodedCall:
    return EncodedCall(
        token.address,
        LocalChain.call_bytes(
            "approve(address,uint256)", ["address", "uint256"], [spender, amount]
        ),
    )


def transfer_from(token: LocalToken, owner: str, to: str, amount: int) -> EncodedCall:
    return EncodedCall(
        token.address,
        LocalChain.call_bytes(
            "transferFrom(address,address,uint256)",
            ["address", "address", "uint256"],
            [owner, to, amount],
        ),
    )


def raw_call(to: str, data: bytes) -> EncodedCall:
    return EncodedCall(to_checksum_address(to), data)
