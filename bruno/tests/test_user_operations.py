"""UserOperations (ERC-4337, EntryPoint v0.7), against both relays: the relay estimates an op's
gas as ERC-7769's eth_estimateUserOperationGas, over JSON-RPC and REST, its fees zero and the
op's own calls paying; the op signed with those limits runs through the EntryPoint's handleOps,
as a call that pays for itself would. What the relay refuses, it refuses reading nothing."""

from typing import Any

import pytest
from eth_abi.abi import decode, encode
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress, HexStr
from eth_utils.crypto import keccak

from bruno.evm import EncodedCall
from bruno.tests import kernel
from bruno.tests.calibur import fresh_client
from bruno.tests.interface import rpc
from bruno.tests.localchain import ENTRY_POINT_0_7, USDC, LocalChain
from bruno.tests.served import Served

# a packed UserOperation (EntryPoint v0.7)
OP = "(address,uint256,bytes,bytes,bytes32,uint256,bytes32,bytes,bytes)"
# @zerodev/sdk's stub ECDSA signature: what an op is estimated with, before it's signed
STUB_SIGNATURE = (
    "0xfffffffffffffffffffffffffffffff0000000000000000000000000000000007aaa"
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1c"
)
PAID = 10**6
USER_OPERATION_EVENT = keccak(
    text="UserOperationEvent(bytes32,address,address,uint256,bool,uint256,uint256)"
)


async def kernel_op(
    chain: LocalChain, owner: LocalAccount, payee: ChecksumAddress
) -> dict[str, Any]:
    """An undeployed Kernel's op, as ERC-7769 writes one, before it's signed: its batch pays
    `payee` PAID of USDC, its fees zero, its signature the stub."""
    account = await kernel.address_of(chain, owner)
    await chain.mint(USDC, account, 10 * PAID)
    transfer = keccak(text="transfer(address,uint256)")[:4] + encode(
        ["address", "uint256"], [payee, PAID]
    )
    nonce = await chain.call(
        ENTRY_POINT_0_7, "getNonce(address,uint192)", ["address", "uint192"], [account, 0]
    )
    return {
        "sender": account,
        "nonce": hex(int.from_bytes(nonce)),
        "factory": kernel.META_FACTORY,
        "factoryData": "0x" + kernel.factory_data(owner).hex(),
        "callData": "0x" + kernel.execute_batch([(USDC.address, 0, transfer)]).hex(),
        "maxFeePerGas": "0x0",
        "maxPriorityFeePerGas": "0x0",
        "signature": STUB_SIGNATURE,
    }


def packed(op: dict[str, Any], estimate: dict[str, str], signature: bytes) -> tuple[Any, ...]:
    """`op` packed as the EntryPoint takes it, its limits `estimate`'s, its fees zero."""
    init = bytes.fromhex(op["factory"][2:] + op["factoryData"][2:])
    limits = int(estimate["verificationGasLimit"], 16).to_bytes(16) + int(
        estimate["callGasLimit"], 16
    ).to_bytes(16)
    return (
        op["sender"],
        int(op["nonce"], 16),
        init,
        bytes.fromhex(op["callData"][2:]),
        limits,
        int(estimate["preVerificationGas"], 16),
        bytes(32),
        b"",
        signature,
    )


async def test_an_estimated_op_runs_through_handle_ops(chain: LocalChain, served: Served) -> None:
    """An undeployed Kernel's op, estimated by the relay over JSON-RPC and REST alike, signed
    with those limits, runs through handleOps from another account: the Kernel deployed, the
    payee paid inside the op, the op a success that cost it nothing, and the call's gas within a
    tenth of what it used."""
    owner = fresh_client("a user operation's owner")
    payee = fresh_client("a user operation's payee").address
    op = await kernel_op(chain, owner, payee)
    async with served.interface() as http:
        answer = await rpc(
            http, served.chain_id, "eth_estimateUserOperationGas", [op, ENTRY_POINT_0_7]
        )
        rest = await http.post(
            f"/{served.chain_id}/v1/user-operations/gas",
            json={"userOperation": op, "entryPoint": ENTRY_POINT_0_7},
        )

    estimate = answer["result"]
    assert rest.status_code == 200, rest.text
    assert rest.json() == estimate
    unsigned = packed(op, estimate, b"")
    op_hash = await chain.call(ENTRY_POINT_0_7, f"getUserOpHash({OP})", [OP], [unsigned])
    signed = packed(op, estimate, kernel.sign_user_operation(owner, op_hash))
    sender = await chain.account(1)
    data = keccak(text=f"handleOps({OP}[],address)")[:4] + encode(
        [f"{OP}[]", "address"], [[signed], payee]
    )
    gas = int(
        await chain.rpc(
            "eth_estimateGas", [{"from": sender, "to": ENTRY_POINT_0_7, "data": "0x" + data.hex()}]
        ),
        16,
    )
    receipt = await chain.send_call(
        sender, EncodedCall(ENTRY_POINT_0_7, HexStr("0x" + data.hex())), "handleOps", gas
    )

    events = [
        decode(["uint256", "bool", "uint256", "uint256"], bytes.fromhex(log["data"][2:]))
        for log in receipt["logs"]
        if bytes.fromhex(log["topics"][0][2:]) == USER_OPERATION_EVENT
    ]
    assert [(success, cost) for _, success, cost, _ in events] == [(True, 0)]
    assert await chain.code(op["sender"]) != "0x"
    assert await chain.balance_of(USDC, payee) == PAID
    assert gas <= int(receipt["gasUsed"], 16) * 11 // 10


@pytest.mark.parametrize(
    ("case", "field"),
    [
        ("another entry point", "entryPoint"),
        ("a paymaster", "paymaster"),
        ("a fee", "maxFeePerGas"),
        ("call data too long", "callData"),
    ],
)
async def test_an_op_refused_reads_nothing(
    chain: LocalChain, served: Served, case: str, field: str
) -> None:
    """An op for another EntryPoint, one with a paymaster, one with a fee, and one whose call
    data is longer than a call's may be: each invalid params, naming the field, refused before
    the relay reads anything."""
    owner = fresh_client(f"a refused user operation's owner: {case}")
    op = await kernel_op(chain, owner, fresh_client("a refused op's payee").address)
    entry_point: str = ENTRY_POINT_0_7
    match case:
        case "another entry point":
            entry_point = "0x4337084D9E255Ff0702461CF8895CE9E3b5Ff108"
        case "a paymaster":
            op["paymaster"] = fresh_client("a paymaster").address
        case "a fee":
            op["maxFeePerGas"] = hex(10**9)
        case _:
            most = (await served.http.get(f"/{served.chain_id}/v1/config")).json()["maxCallBytes"]
            op["callData"] = "0x" + "00" * (int(most, 16) + 1)
    async with served.interface() as http:
        before = len(served.node)
        answer = await rpc(http, served.chain_id, "eth_estimateUserOperationGas", [op, entry_point])
        read = served.node[before:]

    assert answer["error"]["code"] == -32602, answer
    assert answer["error"]["data"] == field, answer
    assert read == [], read


async def test_only_its_own_methods_are_answered(served: Served) -> None:
    """A method the relay doesn't define (eth_call, eth_sendRawTransaction, an RPC gateway's) is
    refused as not found, reading nothing: the relay is no gateway to its nodes."""
    asked: list[tuple[str, list[object]]] = [
        ("eth_call", [{"to": USDC.address, "data": "0x"}, "latest"]),
        ("eth_sendRawTransaction", ["0x00"]),
        ("eth_getBalance", [USDC.address, "latest"]),
    ]
    async with served.interface() as http:
        before = len(served.node)
        answers = [await rpc(http, served.chain_id, method, params) for method, params in asked]
        read = served.node[before:]

    assert [a["error"]["code"] for a in answers] == [-32601] * 3, answers
    assert read == [], read
