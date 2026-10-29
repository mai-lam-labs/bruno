"""UserOperations with another's paymaster, against both relays, two of different kinds, each
vendored from Ethereum with its deposit in EntryPoint v0.8; an EOA delegating to
Simple7702Account in its op. Circle's paymaster, with its oracle (Chainlink's ETH / USD feed),
as Circle's quickstart uses it: the EOA, holding only USDC, pays its gas in USDC by an EIP-2612
permit in its paymaster data, settled in its postOp. Pimlico's singleton paymaster in its
verifying mode, its signer the tests' own: the op sponsored, no postOp. Either way the
EntryPoint pays the relay's payee the op's gas cost in ETH from the paymaster's deposit, so the
op pays the relay by its fees."""

from typing import Any

from eth_abi.abi import encode
from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data
from eth_account.signers.local import LocalAccount
from eth_utils.crypto import keccak

from bruno.tests.calibur import fresh_client
from bruno.tests.interface import rpc
from bruno.tests.localchain import (
    CIRCLE_PAYMASTER,
    ENTRY_POINT_0_8,
    PIMLICO_PAYMASTER,
    USDC,
    USDC_DOMAIN,
    LocalChain,
)
from bruno.tests.served import Served
from bruno.tests.test_eip7702_user_operations import (
    DELEGATION,
    OP,
    SENT,
    STUB_SIGNATURE,
    authorization,
    landed,
    op_of,
    paymaster_and_data,
    signed,
    transfer,
)

# what the permit lets Circle's paymaster take: its prefund is the op's whole maxCost in USDC
# (refunded but its cost), over Circle's quickstart's 10 USDC for a first op on Amsterdam
PERMITTED = 20_000_000
# the paymaster's postOp limit, as Circle's quickstart gives it: its least (its gas charge). Its
# quickstart's validation limit, 200,000, is too little on Amsterdam for an account's first
# permit (a new allowance slot, EIP-8037's state gas), so it's left to the estimate
POST_OP_GAS = 35_000
MAX_DEADLINE = 2**256 - 1


async def permit(chain: LocalChain, owner: LocalAccount) -> bytes:
    """`owner`'s EIP-2612 permit for Circle's paymaster to take PERMITTED of its USDC, the
    deadline the most there is, as Circle's quickstart signs it."""
    nonce = int.from_bytes(
        await chain.call(USDC.address, "nonces(address)", ["address"], [owner.address])
    )
    signed_permit = Account.sign_message(
        encode_typed_data(
            full_message={
                "types": {
                    "EIP712Domain": [
                        {"name": "name", "type": "string"},
                        {"name": "version", "type": "string"},
                        {"name": "chainId", "type": "uint256"},
                        {"name": "verifyingContract", "type": "address"},
                    ],
                    "Permit": [
                        {"name": "owner", "type": "address"},
                        {"name": "spender", "type": "address"},
                        {"name": "value", "type": "uint256"},
                        {"name": "nonce", "type": "uint256"},
                        {"name": "deadline", "type": "uint256"},
                    ],
                },
                "primaryType": "Permit",
                "domain": {
                    "name": USDC_DOMAIN.name,
                    "version": USDC_DOMAIN.version,
                    "chainId": await chain.chain_id(),
                    "verifyingContract": USDC.address,
                },
                "message": {
                    "owner": owner.address,
                    "spender": CIRCLE_PAYMASTER,
                    "value": PERMITTED,
                    "nonce": nonce,
                    "deadline": MAX_DEADLINE,
                },
            }
        ),
        owner.key,
    )
    return signed_permit.signature


async def circle(chain: LocalChain, owner: LocalAccount, limits: bool = True) -> dict[str, str]:
    """An op's fields for Circle's paymaster: its data (mode 0, USDC, the amount permitted and
    `owner`'s permit), and its postOp limit as its quickstart gives it, unless not `limits`."""
    data = (
        bytes(1)
        + bytes.fromhex(USDC.address[2:])
        + PERMITTED.to_bytes(32)
        + await permit(chain, owner)
    )
    fields = {"paymaster": CIRCLE_PAYMASTER, "paymasterData": "0x" + data.hex()}
    if limits:
        fields["paymasterPostOpGasLimit"] = hex(POST_OP_GAS)
    return fields


def without_hash(op: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in op.items() if k != "hash"}


async def test_an_eoa_holding_only_usdc_pays_through_circles_paymaster(
    chain: LocalChain, served: Served
) -> None:
    """An EOA holding USDC and no ETH sends some, delegating in its op, its gas paid to Circle's
    paymaster in USDC by a permit, at the relay's gas price (as Circle's quickstart asks
    pimlico_getUserOperationGasPrice): estimated, its paymaster's postOp limit as given, its
    validation's measured; taken as it's first sent. Once landed, the EOA is delegated and still holds no ETH, the recipient has
    what was sent, Circle took some of the EOA's USDC, and the payee gained the op's gas cost
    in ETH."""
    owner = fresh_client("an EOA paying through Circle's paymaster")
    recipient = fresh_client("Circle's paymaster's op's recipient").address
    held = 12 * SENT
    await chain.mint(USDC, owner.address, held)
    auth = await authorization(chain, owner)
    async with served.interface() as http:
        price = await rpc(http, served.chain_id, "pimlico_getUserOperationGasPrice", [])
        op = {
            **await op_of(chain, owner, [transfer(recipient, SENT)]),
            **price["result"]["standard"],
            **await circle(chain, owner),
            "eip7702Auth": auth,
        }
        estimate = await rpc(
            http, served.chain_id, "eth_estimateUserOperationGas", [op, ENTRY_POINT_0_8]
        )
        assert "result" in estimate, estimate
        op = await signed(chain, owner, {**op, **estimate["result"]})
        before = await chain.eth_balance(served.payee)
        answer = await rpc(
            http, served.chain_id, "eth_sendUserOperation", [without_hash(op), ENTRY_POINT_0_8]
        )
        assert "result" in answer, answer
        receipt = await landed(served, http, answer["result"])

    assert int(estimate["result"]["paymasterVerificationGasLimit"], 16) > 0
    assert estimate["result"]["paymasterPostOpGasLimit"] == hex(POST_OP_GAS)
    assert answer["result"] == op["hash"]
    assert receipt is not None
    assert receipt["success"] is True, receipt
    assert receipt["paymaster"] == CIRCLE_PAYMASTER
    assert await chain.code(owner.address) == DELEGATION
    assert await chain.eth_balance(owner.address) == 0
    assert await chain.balance_of(USDC, recipient) == SENT
    assert 0 < held - SENT - await chain.balance_of(USDC, owner.address) < PERMITTED
    assert await chain.eth_balance(served.payee) - before == int(receipt["actualGasCost"], 16)


async def test_a_paymaster_with_no_fees_is_refused(chain: LocalChain, served: Served) -> None:
    """An op with a paymaster whose fees are zero would pay the relay nothing: invalid params,
    naming maxFeePerGas, read nothing."""
    owner = fresh_client("an EOA with a paymaster and no fees")
    async with served.interface() as http:
        op = {
            **await op_of(chain, owner, [transfer(owner.address, SENT)]),
            **await circle(chain, owner),
        }
        before = len(served.node)
        answer = await rpc(
            http, served.chain_id, "eth_estimateUserOperationGas", [op, ENTRY_POINT_0_8]
        )
        read = served.node[before:]

    assert answer["error"]["code"] == -32602, answer
    assert answer["error"]["data"] == "maxFeePerGas", answer
    assert read == [], read


# Pimlico's singleton paymaster's signer, as vendored (pimlico_paymaster_0_8's storage)
PIMLICO_SIGNER = fresh_client("Pimlico's paymaster's signer")
# its verifying mode (0), any bundler allowed (the lowest bit), valid from 0 until 0 (always)
SPONSORED = bytes([0 << 1 | 1]) + bytes(6) + bytes(6)


async def sponsorship(chain: LocalChain, op: dict[str, Any], signer: LocalAccount) -> str:
    """Pimlico's paymaster's data for `op` (its limits set): sponsored, signed by `signer` over
    the paymaster's own getHash of it (its account delegated, by override, if it isn't yet), as
    an Ethereum signed message."""
    unsigned = {**op, "paymasterData": "0x" + SPONSORED.hex()}
    packed = (
        unsigned["sender"],
        int(unsigned["nonce"], 16),
        bytes.fromhex("7702" + "00" * 18 + unsigned["factoryData"][2:]),
        bytes.fromhex(unsigned["callData"][2:]),
        int(unsigned["verificationGasLimit"], 16).to_bytes(16)
        + int(unsigned["callGasLimit"], 16).to_bytes(16),
        int(unsigned["preVerificationGas"], 16),
        int(unsigned["maxPriorityFeePerGas"], 16).to_bytes(16)
        + int(unsigned["maxFeePerGas"], 16).to_bytes(16),
        paymaster_and_data(unsigned),
        b"",
    )
    data = keccak(text=f"getHash(uint8,{OP})")[:4] + encode(["uint8", OP], [0, packed])
    hash_: str = await chain.rpc(
        "eth_call",
        [
            {"to": PIMLICO_PAYMASTER, "data": "0x" + data.hex()},
            "latest",
            {op["sender"]: {"code": DELEGATION}},
        ],
    )
    signature = signer.sign_message(encode_defunct(hexstr=hash_)).signature
    return "0x" + (SPONSORED + signature).hex()


async def sponsored_op(
    chain: LocalChain, http: Any, served: Served, owner: LocalAccount, signer: LocalAccount
) -> tuple[dict[str, Any], dict[str, Any]]:
    """`owner`'s first op, sending SENT of its USDC, sponsored by Pimlico's paymaster: estimated
    with a stub sponsorship and its paymaster's limits left to the estimate, then those set, the
    sponsorship signed by `signer`, and the op by `owner`. The estimate's answer, and the op."""
    # a sponsorship signed by no one, well formed (the paymaster recovers its signer)
    stub = "0x" + SPONSORED.hex() + STUB_SIGNATURE[2:]
    price = await rpc(http, served.chain_id, "pimlico_getUserOperationGasPrice", [])
    op = {
        **await op_of(
            chain, owner, [transfer(fresh_client("a sponsored op's recipient").address, SENT)]
        ),
        **price["result"]["standard"],
        "paymaster": PIMLICO_PAYMASTER,
        "paymasterData": stub,
        "eip7702Auth": await authorization(chain, owner),
    }
    estimate = await rpc(
        http, served.chain_id, "eth_estimateUserOperationGas", [op, ENTRY_POINT_0_8]
    )
    assert "result" in estimate, estimate
    op = {**op, **estimate["result"]}
    op["paymasterData"] = await sponsorship(chain, op, signer)
    return estimate["result"], await signed(chain, owner, op)


async def test_an_op_sponsored_by_pimlicos_paymaster_lands(
    chain: LocalChain, served: Served
) -> None:
    """An EOA holding no ETH sends USDC in its first op, sponsored by Pimlico's paymaster in its
    verifying mode (no postOp), its paymaster's limits measured by the estimate. Taken as it's
    first sent; once landed, the EOA is delegated and still holds no ETH, and the payee gained
    the op's gas cost in ETH, from the paymaster's deposit."""
    owner = fresh_client("an EOA Pimlico's paymaster sponsors")
    await chain.mint(USDC, owner.address, SENT)
    async with served.interface() as http:
        estimate, op = await sponsored_op(chain, http, served, owner, PIMLICO_SIGNER)
        before = await chain.eth_balance(served.payee)
        answer = await rpc(
            http, served.chain_id, "eth_sendUserOperation", [without_hash(op), ENTRY_POINT_0_8]
        )
        assert "result" in answer, answer
        receipt = await landed(served, http, answer["result"])

    assert int(estimate["paymasterVerificationGasLimit"], 16) > 0
    assert answer["result"] == op["hash"]
    assert receipt is not None
    assert receipt["success"] is True, receipt
    assert receipt["paymaster"] == PIMLICO_PAYMASTER
    assert await chain.code(owner.address) == DELEGATION
    assert await chain.eth_balance(owner.address) == 0
    assert await chain.balance_of(USDC, owner.address) == 0
    assert await chain.eth_balance(served.payee) - before == int(receipt["actualGasCost"], 16)


async def test_a_sponsorship_another_key_signed_is_refused(
    chain: LocalChain, served: Served
) -> None:
    """An op whose sponsorship a key Pimlico's paymaster doesn't know signed: the paymaster
    refuses it (AA34), answered as invalid params naming paymasterData; nothing is taken."""
    owner = fresh_client("an EOA with a forged sponsorship")
    await chain.mint(USDC, owner.address, SENT)
    async with served.interface() as http:
        _, op = await sponsored_op(chain, http, served, owner, fresh_client("not Pimlico's signer"))
        answer = await rpc(
            http, served.chain_id, "eth_sendUserOperation", [without_hash(op), ENTRY_POINT_0_8]
        )

    assert answer["error"]["code"] == -32602, answer
    assert answer["error"]["data"] == "paymasterData", answer
