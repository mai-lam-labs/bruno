"""UserOperations (ERC-4337, EntryPoint v0.7), against both relays: the relay estimates an op's
gas as ERC-7769's eth_estimateUserOperationGas, over JSON-RPC and REST, and takes it, the op
paying either by its own calls (its fees zero, what they pay quoted before it's signed) or by
its fees, as at any bundler, at the price the relay asks; the op signed with those limits runs through the EntryPoint's handleOps, as a
call that pays for itself would. What the relay refuses, it refuses reading nothing."""

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
from bruno.tests.localchain import ENTRY_POINT_0_7, ENTRY_POINT_0_8, USDC, LocalChain
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
    chain: LocalChain, owner: LocalAccount, payee: ChecksumAddress, amount: int = PAID
) -> dict[str, Any]:
    """An undeployed Kernel's op, as ERC-7769 writes one, before it's signed: its batch pays
    `payee` `amount` of USDC (the Kernel given ten times PAID), its fees zero, its signature the
    stub."""
    account = await kernel.address_of(chain, owner)
    if await chain.balance_of(USDC, account) < 10 * PAID:
        await chain.mint(USDC, account, 10 * PAID)
    transfer = keccak(text="transfer(address,uint256)")[:4] + encode(
        ["address", "uint256"], [payee, amount]
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
    """`op` packed as the EntryPoint takes it, its limits `estimate`'s."""
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
        int(op["maxPriorityFeePerGas"], 16).to_bytes(16) + int(op["maxFeePerGas"], 16).to_bytes(16),
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
        ("an EIP-7702 account for v0.7", "factory"),
        ("a paymaster's data but no paymaster", "paymaster"),
        ("call data too long", "callData"),
    ],
)
@pytest.mark.parametrize("method", ["eth_estimateUserOperationGas", "postpaid_quoteUserOperation"])
async def test_an_op_refused_reads_nothing(
    chain: LocalChain, served: Served, case: str, field: str, method: str
) -> None:
    """An op for another EntryPoint (v0.6), an EIP-7702 account's for v0.7, one with a
    paymaster's data but no paymaster, and one whose call data is longer than a call's may be:
    each invalid params, naming the field, refused before the relay reads anything, estimated
    or quoted."""
    owner = fresh_client(f"a refused user operation's owner: {case}")
    op = await kernel_op(chain, owner, fresh_client("a refused op's payee").address)
    entry_point: str = ENTRY_POINT_0_7
    match case:
        case "another entry point":
            entry_point = "0x5FF137D4b0FDCD49DcA30c7CF57E578a026d2789"
        case "an EIP-7702 account for v0.7":
            op["factory"] = "0x7702"
        case "a paymaster's data but no paymaster":
            op["paymasterData"] = "0x00"
        case _:
            most = (await served.http.get(f"/{served.chain_id}/v1/config")).json()["maxCallBytes"]
            op["callData"] = "0x" + "00" * (int(most, 16) + 1)
    async with served.interface() as http:
        before = len(served.node)
        answer = await rpc(http, served.chain_id, method, [op, entry_point])
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


async def signed_op(
    chain: LocalChain,
    http: Any,
    served: Served,
    owner: LocalAccount,
    amount: int,
) -> dict[str, Any]:
    """The Kernel's op paying the relay's payee `amount` of USDC: estimated by the relay, its
    limits set, signed by its owner."""
    op = await kernel_op(chain, owner, served.payee, amount)
    estimate = (
        await rpc(http, served.chain_id, "eth_estimateUserOperationGas", [op, ENTRY_POINT_0_7])
    )["result"]
    op = {**op, **estimate}
    op_hash = await chain.call(
        ENTRY_POINT_0_7, f"getUserOpHash({OP})", [OP], [packed(op, estimate, b"")]
    )
    return {**op, "signature": "0x" + kernel.sign_user_operation(owner, op_hash).hex()}


async def test_an_op_quoted_is_signed_once_and_lands(chain: LocalChain, served: Served) -> None:
    """An undeployed Kernel's op, as a client sends it knowing its price before it signs:
    quoted (postpaid_quoteUserOperation, over JSON-RPC and REST alike) with its payment a
    placeholder of a unit, in each token the relay takes, for as long as a quote lasts; signed
    once paying what's quoted in USDC, it's taken, its hash the EntryPoint's own; sent again,
    taken once. Paying a unit, it's underpaid, saying what's required. Once landed, its receipt
    says it succeeded, the Kernel deployed and the payee paid in the op."""
    owner = fresh_client("a sent user operation's owner")
    before = await chain.balance_of(USDC, served.payee)
    async with served.interface() as http:
        draft = await kernel_op(chain, owner, served.payee, 1)
        quoted = await rpc(
            http, served.chain_id, "postpaid_quoteUserOperation", [draft, ENTRY_POINT_0_7]
        )
        rest = await http.post(
            f"/{served.chain_id}/v1/user-operations/quote",
            json={"userOperation": draft, "entryPoint": ENTRY_POINT_0_7},
        )
        terms = (await http.get(f"/{served.chain_id}/v1/config")).json()
        head = int(await chain.rpc("eth_blockNumber", []), 16)
        short = await signed_op(chain, http, served, owner, 1)
        underpaid = await rpc(
            http, served.chain_id, "eth_sendUserOperation", [short, ENTRY_POINT_0_7]
        )
        assert "result" in quoted, quoted
        amounts = {q["token"]: int(q["amount"], 16) for q in quoted["result"]["quotes"]}
        # the Kernel holds what's quoted, whatever the fork's gas costs
        await chain.mint(USDC, draft["sender"], amounts[USDC.address])
        op = await signed_op(chain, http, served, owner, amounts[USDC.address])
        sent = await rpc(http, served.chain_id, "eth_sendUserOperation", [op, ENTRY_POINT_0_7])
        assert "result" in sent, sent
        again = await http.post(
            f"/{served.chain_id}/v1/user-operations",
            json={"userOperation": op, "entryPoint": ENTRY_POINT_0_7},
        )
        op_hash = sent["result"]
        pending = await rpc(http, served.chain_id, "eth_getUserOperationReceipt", [op_hash])
        receipt: dict[str, Any] | None = None
        for _ in range(5):
            await served.land()
            receipt = (await rpc(http, served.chain_id, "eth_getUserOperationReceipt", [op_hash]))[
                "result"
            ]
            if receipt is not None:
                break
        rest_receipt = (await http.get(f"/{served.chain_id}/v1/user-operations/{op_hash}")).json()

    assert rest.status_code == 200, rest.text
    assert rest.json() == quoted["result"]
    assert list(amounts) == terms["tokens"]
    assert int(quoted["result"]["validUntilBlock"], 16) > head
    assert underpaid["error"]["code"] == -32010, underpaid
    assert int(underpaid["error"]["data"]["required"], 16) <= amounts[USDC.address]
    expected = await chain.call(
        ENTRY_POINT_0_7,
        f"getUserOpHash({OP})",
        [OP],
        [packed(op, op, bytes.fromhex(op["signature"][2:]))],
    )
    assert op_hash == "0x" + expected.hex(), sent
    assert again.status_code == 200, again.text
    assert again.json() == {"userOpHash": op_hash}
    assert pending["result"] is None, pending
    assert receipt is not None
    assert (receipt["userOpHash"], receipt["sender"], receipt["success"]) == (
        op_hash,
        op["sender"],
        True,
    )
    assert receipt["receipt"]["status"] == "0x1"
    assert rest_receipt == receipt
    assert await chain.code(op["sender"]) != "0x"
    assert await chain.balance_of(USDC, served.payee) - before == amounts[USDC.address]


async def test_an_op_paying_by_its_fees_is_not_quoted(chain: LocalChain, served: Served) -> None:
    """An op paying by its fees is priced by its estimate, not quoted: invalid params naming its
    max fee, refused before the relay reads anything."""
    owner = fresh_client("an op quoted with fees' owner")
    op = {
        **await kernel_op(chain, owner, fresh_client("its recipient").address),
        "maxFeePerGas": "0x3b9aca00",
        "maxPriorityFeePerGas": "0x3b9aca00",
    }
    async with served.interface() as http:
        before = len(served.node)
        answer = await rpc(
            http, served.chain_id, "postpaid_quoteUserOperation", [op, ENTRY_POINT_0_7]
        )
        read = served.node[before:]

    assert answer["error"]["code"] == -32602, answer
    assert answer["error"]["data"] == "maxFeePerGas", answer
    assert read == [], read


async def node_fees(chain: LocalChain) -> dict[str, str]:
    """The fees viem gives an op when not told any: its node's, as viem's estimateFeesPerGas
    has them (the base fee a fifth more and the node's priority fee), both doubled."""
    priority = int(await chain.rpc("eth_maxPriorityFeePerGas", []), 16)
    base_fee = await chain.base_fee()
    return {
        "maxFeePerGas": hex(2 * (base_fee * 12 // 10 + priority)),
        "maxPriorityFeePerGas": hex(2 * priority),
    }


async def fee_op(
    chain: LocalChain,
    http: Any,
    served: Served,
    owner: LocalAccount,
    recipient: ChecksumAddress,
    fees: dict[str, str],
) -> dict[str, Any]:
    """The Kernel's op sending `recipient` PAID of USDC, paying the relay by its `fees`:
    estimated by the relay, its limits set, signed by its owner."""
    op = {**await kernel_op(chain, owner, recipient), **fees}
    answer = await rpc(http, served.chain_id, "eth_estimateUserOperationGas", [op, ENTRY_POINT_0_7])
    assert "result" in answer, answer
    estimate = answer["result"]
    op = {**op, **estimate}
    op_hash = await chain.call(
        ENTRY_POINT_0_7, f"getUserOpHash({OP})", [OP], [packed(op, estimate, b"")]
    )
    return {**op, "signature": "0x" + kernel.sign_user_operation(owner, op_hash).hex()}


@pytest.mark.parametrize("fees", ["the relay's", "the node's"])
async def test_an_op_paying_by_its_fees_lands(chain: LocalChain, served: Served, fees: str) -> None:
    """An undeployed Kernel holding ETH sends USDC to someone else in an op paying by its fees:
    at the price the relay asks (pimlico_ and zd_getUserOperationGasPrice alike, both fees one
    price), or at the node's fees as viem gives them unasked, its preVerificationGas making up
    the price. Taken as it's first sent; once landed, its receipt says it succeeded, the
    EntryPoint paid the payee its gas cost in ETH, and that's more than the relay's transaction
    cost."""
    owner = fresh_client(f"an op paying by its fees' owner, at {fees}")
    recipient = fresh_client(f"an op paying by its fees' recipient, at {fees}").address
    await chain.set_eth_balance(await kernel.address_of(chain, owner), 10**17)
    async with served.interface() as http:
        pimlico = await rpc(http, served.chain_id, "pimlico_getUserOperationGasPrice", [])
        zerodev = await rpc(http, served.chain_id, "zd_getUserOperationGasPrice", [])
        offered = pimlico["result"]["standard"] if fees == "the relay's" else await node_fees(chain)
        op = await fee_op(chain, http, served, owner, recipient, offered)
        before = await chain.eth_balance(served.payee)
        sent = await rpc(http, served.chain_id, "eth_sendUserOperation", [op, ENTRY_POINT_0_7])
        assert "result" in sent, sent
        receipt: dict[str, Any] | None = None
        for _ in range(5):
            await served.land()
            receipt = (
                await rpc(http, served.chain_id, "eth_getUserOperationReceipt", [sent["result"]])
            )["result"]
            if receipt is not None:
                break

    prices = pimlico["result"]
    assert zerodev["result"] == prices
    assert prices["slow"] == prices["standard"] == prices["fast"]
    assert prices["standard"]["maxFeePerGas"] == prices["standard"]["maxPriorityFeePerGas"]
    assert int(op["preVerificationGas"], 16) > 0
    assert receipt is not None
    assert receipt["success"] is True, receipt
    assert await chain.balance_of(USDC, recipient) == PAID
    gained = await chain.eth_balance(served.payee) - before
    assert gained == int(receipt["actualGasCost"], 16)
    cost = int(receipt["receipt"]["gasUsed"], 16) * int(receipt["receipt"]["effectiveGasPrice"], 16)
    assert gained > cost, (gained, cost)


async def test_an_op_that_cannot_pay_its_fees_fails_its_estimate(
    chain: LocalChain, served: Served
) -> None:
    """An op paying by its fees from an account holding no ETH can't pay its prefund: its
    estimate is simulation failed, the EntryPoint's reason in its data."""
    owner = fresh_client("an op that can't pay its fees' owner")
    async with served.interface() as http:
        price = (await rpc(http, served.chain_id, "pimlico_getUserOperationGasPrice", []))["result"]
        op = {
            **await kernel_op(chain, owner, fresh_client("its recipient").address),
            **price["standard"],
        }
        answer = await rpc(
            http, served.chain_id, "eth_estimateUserOperationGas", [op, ENTRY_POINT_0_7]
        )

    assert answer["error"]["code"] == -32011, answer
    assert b"AA21" in bytes.fromhex(answer["error"]["data"]["revertData"][2:]), answer


async def test_an_op_paying_nothing_is_refused(chain: LocalChain, served: Served) -> None:
    """An op whose calls pay the relay's payee nothing (they pay someone else) is invalid
    params, naming its call data; nothing is taken."""
    owner = fresh_client("an op paying someone else's owner")
    elsewhere = fresh_client("not the relay's payee").address
    async with served.interface() as http:
        op = await kernel_op(chain, owner, elsewhere)
        estimate = (
            await rpc(http, served.chain_id, "eth_estimateUserOperationGas", [op, ENTRY_POINT_0_7])
        )["result"]
        op = {**op, **estimate}
        op_hash = await chain.call(
            ENTRY_POINT_0_7, f"getUserOpHash({OP})", [OP], [packed(op, estimate, b"")]
        )
        op["signature"] = "0x" + kernel.sign_user_operation(owner, op_hash).hex()
        answer = await rpc(http, served.chain_id, "eth_sendUserOperation", [op, ENTRY_POINT_0_7])

    assert answer["error"]["code"] == -32602, answer
    assert answer["error"]["data"] == "callData", answer


async def test_an_op_another_key_signed_is_refused(chain: LocalChain, served: Served) -> None:
    """An op signed by a key that isn't its account's owner: the EntryPoint refuses it (AA24),
    answered as invalid params naming its signature; nothing is taken."""
    owner = fresh_client("a wrongly signed op's owner")
    stranger = fresh_client("not the op's owner")
    async with served.interface() as http:
        op = await signed_op(chain, http, served, owner, PAID)
        op_hash = await chain.call(
            ENTRY_POINT_0_7, f"getUserOpHash({OP})", [OP], [packed(op, op, b"")]
        )
        op["signature"] = "0x" + kernel.sign_user_operation(stranger, op_hash).hex()
        answer = await rpc(http, served.chain_id, "eth_sendUserOperation", [op, ENTRY_POINT_0_7])

    assert answer["error"]["code"] == -32602, answer
    assert answer["error"]["data"] == "signature", answer


async def test_what_is_answered_reading_nothing(served: Served) -> None:
    """The EntryPoints taken, the chain's id, and the receipt of an op the relay never took
    (null): each answered without a read."""
    async with served.interface() as http:
        before = len(served.node)
        entry_points = await rpc(http, served.chain_id, "eth_supportedEntryPoints", [])
        chain_id = await rpc(http, served.chain_id, "eth_chainId", [])
        unknown = await rpc(
            http, served.chain_id, "eth_getUserOperationReceipt", ["0x" + "ab" * 32]
        )
        read = served.node[before:]

    assert entry_points["result"] == [ENTRY_POINT_0_8, ENTRY_POINT_0_7]
    assert chain_id["result"] == hex(served.chain_id)
    assert unknown["result"] is None
    assert read == []
