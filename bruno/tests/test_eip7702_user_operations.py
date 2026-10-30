"""UserOperations for EntryPoint v0.8 from EIP-7702 accounts, against both relays: an EOA
delegating to eth-infinitism's Simple7702Account (the delegate viem's toSimple7702SmartAccount
names) sends ops through the relay as ERC-7769 writes them, its factory 0x7702. Its first op
brings its authorization (eip7702Auth), which the relay's transaction carries, estimated with
viem's stub authorization; once delegated, its ops bring none. The op's hash is the
EntryPoint's own (EIP-712's, its delegate in place of its init code)."""

from typing import Any

from eth_abi.abi import encode
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_typing import Hash32
from eth_utils.crypto import keccak

from bruno.tests.calibur import fresh_client
from bruno.tests.interface import rpc
from bruno.tests.localchain import ENTRY_POINT_0_8, SIMPLE_7702_ACCOUNT, USDC, LocalChain
from bruno.tests.served import Served

# a packed UserOperation, as EntryPoint v0.8 takes it
OP = "(address,uint256,bytes,bytes,bytes32,uint256,bytes32,bytes,bytes)"
# viem's stub signature, the op's and its authorization's while it's estimated
STUB_SIGNATURE = (
    "0xfffffffffffffffffffffffffffffff0000000000000000000000000000000007aaa"
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1c"
)
STUB_R = "0xfffffffffffffffffffffffffffffff000000000000000000000000000000000"
STUB_S = "0x7aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SENT = 2_500_000
DELEGATION = "0xef0100" + SIMPLE_7702_ACCOUNT[2:].lower()


def transfer(to: str, amount: int) -> tuple[str, int, bytes]:
    return (
        USDC.address,
        0,
        keccak(text="transfer(address,uint256)")[:4] + encode(["address", "uint256"], [to, amount]),
    )


def execute_batch(calls: list[tuple[str, int, bytes]]) -> str:
    """Simple7702Account's executeBatch of `calls`."""
    selector = keccak(text="executeBatch((address,uint256,bytes)[])")[:4]
    return "0x" + (selector + encode(["(address,uint256,bytes)[]"], [calls])).hex()


async def authorization(chain: LocalChain, owner: LocalAccount) -> dict[str, str]:
    """`owner`'s authorization of Simple7702Account, at its nonce, as viem writes it."""
    signed = Account.sign_authorization(
        {
            "chainId": await chain.chain_id(),
            "address": SIMPLE_7702_ACCOUNT,
            "nonce": await chain.nonce(owner.address),
        },
        owner.key,
    )
    return {
        "chainId": hex(signed.chain_id),
        "address": SIMPLE_7702_ACCOUNT,
        "nonce": hex(signed.nonce),
        "yParity": f"0x{signed.y_parity:02x}",
        "r": f"0x{signed.r:064x}",
        "s": f"0x{signed.s:064x}",
    }


def stubbed(auth: dict[str, str]) -> dict[str, str]:
    """`auth` as viem estimates with it: its signature the stub."""
    return {**auth, "yParity": "0x01", "r": STUB_R, "s": STUB_S}


async def op_of(
    chain: LocalChain, owner: LocalAccount, calls: list[tuple[str, int, bytes]]
) -> dict[str, Any]:
    """`owner`'s op making `calls`, as ERC-7769 writes it before it's estimated: its factory
    0x7702, its fees zero, its signature the stub."""
    nonce = await chain.call(
        ENTRY_POINT_0_8, "getNonce(address,uint192)", ["address", "uint192"], [owner.address, 0]
    )
    return {
        "sender": owner.address,
        "nonce": hex(int.from_bytes(nonce)),
        "factory": "0x7702",
        "factoryData": "0x",
        "callData": execute_batch(calls),
        "maxFeePerGas": "0x0",
        "maxPriorityFeePerGas": "0x0",
        "signature": STUB_SIGNATURE,
    }


def paymaster_and_data(op: dict[str, Any]) -> bytes:
    """`op`'s paymaster, its limits and its data, packed: empty without one."""
    if "paymaster" not in op:
        return b""
    return (
        bytes.fromhex(op["paymaster"][2:])
        + int(op["paymasterVerificationGasLimit"], 16).to_bytes(16)
        + int(op["paymasterPostOpGasLimit"], 16).to_bytes(16)
        + bytes.fromhex(op["paymasterData"][2:])
    )


async def signed(chain: LocalChain, owner: LocalAccount, op: dict[str, Any]) -> dict[str, Any]:
    """`op` signed by `owner`: its hash as EntryPoint v0.8's getUserOpHash computes it, the
    account delegated (by override, if it isn't yet), signed as it is (Simple7702Account
    recovers the hash itself)."""
    packed = (
        op["sender"],
        int(op["nonce"], 16),
        bytes.fromhex("7702" + "00" * 18 + op["factoryData"][2:]),
        bytes.fromhex(op["callData"][2:]),
        int(op["verificationGasLimit"], 16).to_bytes(16) + int(op["callGasLimit"], 16).to_bytes(16),
        int(op["preVerificationGas"], 16),
        int(op["maxPriorityFeePerGas"], 16).to_bytes(16) + int(op["maxFeePerGas"], 16).to_bytes(16),
        paymaster_and_data(op),
        b"",
    )
    data = keccak(text=f"getUserOpHash({OP})")[:4] + encode([OP], [packed])
    op_hash: str = await chain.rpc(
        "eth_call",
        [
            {"to": ENTRY_POINT_0_8, "data": "0x" + data.hex()},
            "latest",
            {owner.address: {"code": DELEGATION}},
        ],
    )
    signature = owner.unsafe_sign_hash(Hash32(bytes.fromhex(op_hash[2:]))).signature
    return {**op, "signature": "0x" + signature.hex(), "hash": op_hash}


async def landed(served: Served, http: Any, op_hash: str) -> dict[str, Any] | None:
    """The op's receipt once it lands, within five blocks."""
    for _ in range(5):
        await served.land()
        answer = await rpc(http, served.chain_id, "eth_getUserOperationReceipt", [op_hash])
        if answer["result"] is not None:
            return answer["result"]
    return None


async def test_an_eoa_delegating_in_its_op_pays_by_its_calls(
    chain: LocalChain, served: Served
) -> None:
    """An EOA holding only USDC (an empty account, but for it) sends some to someone else in its
    first op, delegating to Simple7702Account by the authorization it brings, its last call
    paying the relay: quoted with viem's stub authorization and a placeholder paying a unit,
    then estimated and signed once paying what's quoted, it's taken, its hash the EntryPoint's
    own. Once landed, its receipt says it succeeded, the EOA is delegated, and it holds no
    ETH."""
    owner = fresh_client("an EOA delegating in its user operation")
    recipient = fresh_client("an EIP-7702 op's recipient").address
    await chain.mint(USDC, owner.address, 10 * SENT)
    auth = await authorization(chain, owner)
    before = await chain.balance_of(USDC, served.payee)
    async with served.interface() as http:

        def unsigned(paying: int) -> list[tuple[str, int, bytes]]:
            return [transfer(recipient, SENT), transfer(served.payee, paying)]

        draft = {**await op_of(chain, owner, unsigned(1)), "eip7702Auth": stubbed(auth)}
        quoted = await rpc(
            http, served.chain_id, "postpaid_quoteUserOperation", [draft, ENTRY_POINT_0_8]
        )
        assert "result" in quoted, quoted
        amounts = {q["token"]: int(q["amount"], 16) for q in quoted["result"]["quotes"]}
        paying = amounts[USDC.address]
        op = {**await op_of(chain, owner, unsigned(paying)), "eip7702Auth": stubbed(auth)}
        estimate = await rpc(
            http, served.chain_id, "eth_estimateUserOperationGas", [op, ENTRY_POINT_0_8]
        )
        assert "result" in estimate, estimate
        op = await signed(chain, owner, {**op, **estimate["result"], "eip7702Auth": auth})
        answer = await rpc(
            http,
            served.chain_id,
            "eth_sendUserOperation",
            [{k: v for k, v in op.items() if k != "hash"}, ENTRY_POINT_0_8],
        )
        assert "result" in answer, answer
        receipt = await landed(served, http, answer["result"])

    assert answer["result"] == op["hash"]
    assert receipt is not None
    assert (receipt["entryPoint"], receipt["sender"], receipt["success"]) == (
        ENTRY_POINT_0_8,
        owner.address,
        True,
    )
    assert await chain.code(owner.address) == DELEGATION
    assert await chain.balance_of(USDC, recipient) == SENT
    assert await chain.balance_of(USDC, served.payee) - before == paying
    assert await chain.eth_balance(owner.address) == 0


async def test_a_delegated_eoa_pays_by_its_fees(chain: LocalChain, served: Served) -> None:
    """An EOA delegated to Simple7702Account already, holding ETH, sends USDC in an op paying
    by its fees at the relay's price, its factory 0x7702 and no authorization: its hash the
    EntryPoint's own, read from its delegation. Taken as it's first sent; once landed, the
    EntryPoint paid the payee its gas cost in ETH."""
    owner = fresh_client("a delegated EOA paying by its fees")
    recipient = fresh_client("a delegated EOA's recipient").address
    await chain.rpc("anvil_setCode", [owner.address, DELEGATION])
    await chain.set_eth_balance(owner.address, 10**17)
    await chain.mint(USDC, owner.address, SENT)
    async with served.interface() as http:
        price = await rpc(http, served.chain_id, "pimlico_getUserOperationGasPrice", [])
        op = {
            **await op_of(chain, owner, [transfer(recipient, SENT)]),
            **price["result"]["standard"],
        }
        estimate = await rpc(
            http, served.chain_id, "eth_estimateUserOperationGas", [op, ENTRY_POINT_0_8]
        )
        assert "result" in estimate, estimate
        op = await signed(chain, owner, {**op, **estimate["result"]})
        before = await chain.eth_balance(served.payee)
        answer = await rpc(
            http,
            served.chain_id,
            "eth_sendUserOperation",
            [{k: v for k, v in op.items() if k != "hash"}, ENTRY_POINT_0_8],
        )
        assert "result" in answer, answer
        receipt = await landed(served, http, answer["result"])

    assert answer["result"] == op["hash"]
    assert receipt is not None
    assert receipt["success"] is True, receipt
    assert await chain.balance_of(USDC, recipient) == SENT
    assert await chain.eth_balance(served.payee) - before == int(receipt["actualGasCost"], 16)


async def test_an_authorization_another_key_signed_is_refused(
    chain: LocalChain, served: Served
) -> None:
    """An op whose authorization isn't its account's own (another key signed it): estimated
    as any, its authorization's signature unchecked; sent, it's invalid params naming
    eip7702Auth."""
    owner = fresh_client("an EOA whose op brings another's authorization")
    stranger = fresh_client("not the op's account")
    await chain.mint(USDC, owner.address, 10 * SENT)
    async with served.interface() as http:
        calls = [transfer(served.payee, SENT)]
        auth = stubbed(await authorization(chain, owner))
        op = {**await op_of(chain, owner, calls), "eip7702Auth": auth}
        estimate = await rpc(
            http, served.chain_id, "eth_estimateUserOperationGas", [op, ENTRY_POINT_0_8]
        )
        assert "result" in estimate, estimate
        theirs = await authorization(chain, stranger)
        op = await signed(chain, owner, {**op, **estimate["result"], "eip7702Auth": theirs})
        answer = await rpc(
            http,
            served.chain_id,
            "eth_sendUserOperation",
            [{k: v for k, v in op.items() if k != "hash"}, ENTRY_POINT_0_8],
        )

    assert answer["error"]["code"] == -32602, answer
    assert answer["error"]["data"] == "eip7702Auth", answer


async def test_an_eoa_delegating_in_its_op_pays_by_its_fees(
    chain: LocalChain, served: Served
) -> None:
    """An EOA holding ETH delegates in its first op, which pays by its fees as viem gives them
    unasked (its node's: a price that isn't flat): the transaction carrying its authorization
    warms the account, and the base fee moves what the EntryPoint charges, yet what the send
    measured is what it pays. Taken as it's first sent; once landed, the EOA is delegated and
    the EntryPoint paid the payee its gas cost in ETH."""
    owner = fresh_client("an EOA delegating in an op paying by its fees")
    recipient = fresh_client("an EOA paying by its fees' recipient").address
    await chain.set_eth_balance(owner.address, 10**17)
    await chain.mint(USDC, owner.address, SENT)
    auth = await authorization(chain, owner)
    priority = int(await chain.rpc("eth_maxPriorityFeePerGas", []), 16)
    fees = {
        "maxFeePerGas": hex(2 * (await chain.base_fee() * 12 // 10 + priority)),
        "maxPriorityFeePerGas": hex(2 * priority),
    }
    async with served.interface() as http:
        op = {
            **await op_of(chain, owner, [transfer(recipient, SENT)]),
            **fees,
            "eip7702Auth": stubbed(auth),
        }
        estimate = await rpc(
            http, served.chain_id, "eth_estimateUserOperationGas", [op, ENTRY_POINT_0_8]
        )
        assert "result" in estimate, estimate
        op = await signed(chain, owner, {**op, **estimate["result"], "eip7702Auth": auth})
        before = await chain.eth_balance(served.payee)
        answer = await rpc(
            http,
            served.chain_id,
            "eth_sendUserOperation",
            [{k: v for k, v in op.items() if k != "hash"}, ENTRY_POINT_0_8],
        )
        assert "result" in answer, answer
        receipt = await landed(served, http, answer["result"])

    assert answer["result"] == op["hash"]
    assert receipt is not None
    assert receipt["success"] is True, receipt
    assert await chain.code(owner.address) == DELEGATION
    assert await chain.balance_of(USDC, recipient) == SENT
    assert await chain.eth_balance(served.payee) - before == int(receipt["actualGasCost"], 16)


async def test_an_op_estimated_again_is_estimated_alike(chain: LocalChain, served: Served) -> None:
    """An op paying by its fees estimated, then estimated again with what the first estimate
    gave it (its preVerificationGas large: the relay's price over its fees): the gas it gives
    isn't read, so the second estimate's limits are the first's."""
    owner = fresh_client("an EOA estimated twice")
    await chain.set_eth_balance(owner.address, 10**17)
    async with served.interface() as http:
        price = await rpc(http, served.chain_id, "pimlico_getUserOperationGasPrice", [])
        op = {
            **await op_of(chain, owner, [transfer(owner.address, 0)]),
            **price["result"]["standard"],
            "eip7702Auth": stubbed(await authorization(chain, owner)),
        }
        first = await rpc(
            http, served.chain_id, "eth_estimateUserOperationGas", [op, ENTRY_POINT_0_8]
        )
        assert "result" in first, first
        again = await rpc(
            http,
            served.chain_id,
            "eth_estimateUserOperationGas",
            [{**op, **first["result"]}, ENTRY_POINT_0_8],
        )

    assert "result" in again, again
    for limit in ("verificationGasLimit", "callGasLimit"):
        assert again["result"][limit] == first["result"][limit], (first, again)
