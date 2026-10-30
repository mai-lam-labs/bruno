"""Deploying runners on the local chain, as the service's transactions would: simulated with
a plain eth_call on the pending block first (the deployer passes a runner's revert data up,
so the call's error data is the runner's own), then sent, and mined as simulated."""

from collections.abc import Sequence
from dataclasses import dataclass

import httpx
from eth_account.datastructures import SignedSetCodeAuthorization
from eth_typing import HexStr
from eth_utils.address import to_checksum_address

from bruno.deployer import DEPLOYER, transaction_gas
from bruno.evm import EncodedCall
from bruno.forks import GasSchedule
from bruno.net.jsonrpc import JsonRpcError, call
from bruno.runner import Erc20, Runner
from bruno.tests.localchain import LocalChain, LocalToken
from bruno.tokens import erc20, measuring
from bruno.transactions import authorization_json

# the gas the tests' client calls are given between them: a choice, room for a signed batch
# that creates a few storage slots (~110,000 each under Amsterdam's state gas)
CLIENT_GAS = 1_000_000
# the most gas the tests' relays let a transaction use, everything in it: the default
# configuration's
MAX_GAS = 2_000_000
# anvil's dev account #0 sends the deployments, as a concurrency account would
SENDER_ACCOUNT = 0


@dataclass(frozen=True, slots=True)
class Deployment:
    """A runner's deployment: whether it succeeded, and what the deployer returned (the
    runner's address, ABI-encoded) or reverted with (the runner's revert data)."""

    succeeded: bool
    output: bytes


async def deploy(
    chain: LocalChain,
    runner: Runner,
    schedule: GasSchedule,
    authorizations: Sequence[SignedSetCodeAuthorization] = (),
    empty_authorities: int = 0,
    client_gas: int = CLIENT_GAS,
) -> Deployment:
    """Simulate the deployment with eth_call, then send it (a type-4 transaction with
    `authorizations`, `empty_authorities` of them for empty accounts), at the gas the runner's
    bounds and `schedule` say `client_gas` needs."""
    gas = transaction_gas(
        runner.initcode,
        runner.frame_gas(client_gas),
        schedule,
        len(authorizations),
        empty_authorities,
    )
    data = "0x" + runner.initcode.hex()
    sender = await chain.account(SENDER_ACCOUNT)
    tx: dict[str, object] = {"from": sender, "to": DEPLOYER, "data": data, "gas": hex(gas)}
    if authorizations:
        tx["authorizationList"] = [authorization_json(a) for a in authorizations]
    async with httpx.AsyncClient() as client:
        try:
            result: str = await call(client, chain.url, "eth_call", [tx, "pending"])
            simulated = Deployment(succeeded=True, output=bytes.fromhex(result[2:]))
        except JsonRpcError as e:
            revert = e.data if isinstance(e.data, str) else "0x"
            simulated = Deployment(succeeded=False, output=bytes.fromhex(revert[2:]))
    receipt = await chain.receipt(await chain.rpc("eth_sendTransaction", [tx]))
    assert (receipt["status"] == "0x1") == simulated.succeeded
    return simulated


async def payment_token(
    chain: LocalChain, token: LocalToken, holder: str, schedule: GasSchedule
) -> Erc20:
    holder = to_checksum_address(holder)
    call = measuring(token.address, holder)
    estimate = int(await chain.rpc(call.method, call.params), 16)
    return erc20(token.address, holder, estimate, schedule)


def transfer(token: LocalToken, to: str, amount: int) -> EncodedCall:
    return EncodedCall(
        token.address,
        LocalChain.calldata("transfer(address,uint256)", ["address", "uint256"], [to, amount]),
    )


def approve(token: LocalToken, spender: str, amount: int) -> EncodedCall:
    return EncodedCall(
        token.address,
        LocalChain.calldata("approve(address,uint256)", ["address", "uint256"], [spender, amount]),
    )


def transfer_from(token: LocalToken, owner: str, to: str, amount: int) -> EncodedCall:
    return EncodedCall(
        token.address,
        LocalChain.calldata(
            "transferFrom(address,address,uint256)",
            ["address", "address", "uint256"],
            [owner, to, amount],
        ),
    )


def raw_call(to: str, data: bytes) -> EncodedCall:
    return EncodedCall(to_checksum_address(to), HexStr("0x" + data.hex()))
