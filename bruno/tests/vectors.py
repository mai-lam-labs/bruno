"""Golden vectors for the Worker's TypeScript port (worker/): what the Python reference emits
for a spread of cases, which the port must reproduce byte for byte and gas for gas.

    python -m bruno.tests.vectors > worker/test/vectors.json    (make vectors)

Deterministic: the cases' data and addresses come from keccak of fixed strings, which the
port's tests derive the same way (`data`, `address`), so the vectors carry each call's data
as its length, and each initcode as its length and keccak256."""

import base64
import json
import sys
from collections.abc import Iterator
from dataclasses import replace
from itertools import product

from eth_abi.abi import encode
from eth_account import Account
from eth_account.datastructures import SignedSetCodeAuthorization
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress, Hash32, HexStr
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from bruno import datafee
from bruno.accounts import derive
from bruno.builders import flashbots_signature
from bruno.bundle import Mode, TopUp, bundles, funding_initcode, sign_funding
from bruno.chain import TIP_BLOCKS, Latest, market_tip, risen
from bruno.config import Eip712Domain, PoolKind
from bruno.datafee import DataFee
from bruno.deployer import DEPLOYER, INITCODE, RUNTIME, overhead_gas, transaction_gas
from bruno.eip3009 import (
    TransferAuthorization,
    digest,
    last_block_before,
    quoted_calldata,
    transfer_gas,
)
from bruno.erc6492 import MAGIC, Deployment, WrapperError, unwrapped
from bruno.evm import NATIVE_ASSET, EncodedCall
from bruno.forks import SCHEDULES, GasSchedule
from bruno.router import Step, approve_calldata, swap_calldata, withdraw_calldata
from bruno.runner import (
    BALANCE_UNREADABLE,
    GAS_EXHAUSTED,
    UNDERPAID,
    Erc20,
    Payment,
    RunnerError,
    Transfer,
    in_call_runner,
)
from bruno.server.front import qr
from bruno.server.wire import MAX_SIGNATURE_BYTES
from bruno.server.x402 import MalformedPaymentError, params_of, payload_of
from bruno.submission import Submission
from bruno.tests.transfers import deployment_json
from bruno.transactions import (
    Fees,
    SignedTransaction,
    authorization_json,
    rlp,
    sign,
    sign_drain,
    unsigned_size,
)
from bruno.userops import (
    EIP7702_MARKER,
    ENTRY_POINT_0_7,
    ENTRY_POINT_0_8,
    Paymaster,
    UserOperation,
    balances_code,
    estimated,
    estimating,
    fees_paid,
    handle_ops,
    paid,
    receipt_of,
    refusal,
    sending,
    used,
    user_operation_hash,
    with_overhead,
)
from bruno.wallets import COINBASE_SMART_WALLET, COINBASE_SMART_WALLET_1_1, KERNEL_3_2, verification

DATA_LENGTHS = (0, 1, 31, 32, 33, 100, 1_000, 10_000)
AMOUNTS = (1, 1_000_000, 10**18, 2**256 - 1)
CLIENT_GAS = (0, 21_000, 45_000, 1_000_000)
# (authorizations, empty authorities)
AUTHORIZATIONS = ((0, 0), (1, 0), (1, 1), (2, 1))
BALANCE_GAS = 30_000
# a token's measured transferWithAuthorization, a key's
TRANSFER_GAS = 50_000


def address(name: str) -> ChecksumAddress:
    return to_checksum_address(keccak(text=name)[-20:])


def data(length: int) -> bytes:
    """`length` bytes, some zero and some not, so both calldata prices show."""
    out = b""
    counter = 0
    while len(out) < length:
        out += keccak(text=f"data {counter}")
        counter += 1
    return bytes(b if i % 3 else 0 for i, b in enumerate(out[:length]))


def runners(schedule: GasSchedule) -> Iterator[dict[str, object]]:
    payee = address("payee")
    target = address("target")
    for length in DATA_LENGTHS:
        for amount in AMOUNTS:
            for token in (None, Erc20(address("token"), BALANCE_GAS)):
                for delegated, client_gas in product(((), (target,)), CLIENT_GAS):
                    call = EncodedCall(target, HexStr("0x" + data(length).hex()))
                    runner = in_call_runner(
                        call,
                        Payment(token, payee, amount),
                        schedule,
                        delegated,
                        client_gas=client_gas,
                    )
                    yield {
                        "fork": schedule.name,
                        "call": {"to": call.to, "dataLength": length},
                        "payment": {
                            "token": None
                            if token is None
                            else {"address": token.address, "balanceGas": token.balance_gas},
                            "payee": payee,
                            "amount": str(amount),
                        },
                        "delegated": list(delegated),
                        "clientGas": client_gas,
                        "initcodeLength": len(runner.initcode),
                        "initcodeKeccak": "0x" + keccak(runner.initcode).hex(),
                        "overheadGas": runner.overhead_gas,
                        "frameGas": runner.frame_gas(client_gas),
                        "transactionGas": [
                            {
                                "authorizations": a,
                                "emptyAuthorities": e,
                                "gas": transaction_gas(
                                    runner.initcode, runner.frame_gas(client_gas), schedule, a, e
                                ),
                            }
                            for a, e in AUTHORIZATIONS
                        ],
                    }


def refusals(schedule: GasSchedule) -> Iterator[dict[str, object]]:
    """Runners the reference refuses to emit."""
    payee = address("payee")
    target = address("target")
    # (name, the call's data length, its value, the amount)
    cases = (("value", 0, 1, 1), ("zero amount", 0, 0, 0), ("too long", 49_152, 0, 1))
    for name, length, value, amount in cases:
        call = EncodedCall(target, HexStr("0x" + data(length).hex()), value)
        try:
            in_call_runner(call, Payment(None, payee, amount), schedule, client_gas=0)
        except RunnerError:
            yield {
                "name": name,
                "fork": schedule.name,
                "call": {"to": call.to, "dataLength": length, "value": str(value)},
                "amount": str(amount),
            }
        else:
            raise AssertionError(f"{name}: expected a RunnerError")


DOMAIN = Eip712Domain("USD Coin", "2")
# a contract's signature: longer than a key's 65 bytes, so sent through the bytes overload
CONTRACT_SIGNATURE = bytes(keccak(text=f"a contract's signature {i}")[i % 32] for i in range(224))
# a field left out of a case
_ABSENT = object()


def transfer_authorization(name: str, value: int, valid_before: int) -> TransferAuthorization:
    """`name`'s authorization of `value` of the vectors' token to the payee, signed."""
    payer = client(name)
    token, payee = address("token"), address("payee")
    nonce = keccak(text=f"nonce {name}")
    signed = payer.unsafe_sign_hash(
        Hash32(digest(DOMAIN, CHAIN_ID, token, payer.address, payee, value, 0, valid_before, nonce))
    )
    return TransferAuthorization(
        payer.address, payee, value, 0, valid_before, nonce, bytes(signed.signature)
    )


def merged(fields: dict[str, object], changes: dict[str, object] | None) -> dict[str, object]:
    """`fields` with `changes` made: each changed, added, or left out (_ABSENT)."""
    out = {**fields, **(changes or {})}
    return {k: v for k, v in out.items() if v is not _ABSENT}


def payload_cases() -> list[dict[str, object]]:
    """PAYMENT-SIGNATURE headers, each as the reference takes it (payload_of): taken, or
    refused as malformed. What the Worker must parse as strictly."""
    transfer = transfer_authorization("alice", 10**6, 1_800_000_000)
    signature = transfer.signature

    def payload(
        top: dict[str, object] | None = None,
        accepted: dict[str, object] | None = None,
        authorization: dict[str, object] | None = None,
        signed: str | None = None,
    ) -> dict[str, object]:
        """The whole payload, each part's fields changed as given (_ABSENT leaves one out)."""
        auth = merged(
            {
                "from": transfer.payer,
                "to": transfer.to,
                "value": str(transfer.value),
                "validAfter": "0",
                "validBefore": str(transfer.valid_before),
                "nonce": "0x" + transfer.nonce.hex(),
            },
            authorization,
        )
        requirement = merged(
            {
                "scheme": "exact",
                "network": f"eip155:{CHAIN_ID}",
                "amount": str(transfer.value),
                "asset": address("token"),
                "payTo": transfer.to,
                "maxTimeoutSeconds": 600,
                "extra": {"name": DOMAIN.name, "version": DOMAIN.version},
            },
            accepted,
        )
        sig = signed if signed is not None else "0x" + signature.hex()
        return merged(
            {
                "x402Version": 2,
                "accepted": requirement,
                "payload": {"signature": sig, "authorization": auth},
            },
            top,
        )

    def b64(raw: bytes) -> str:
        return base64.b64encode(raw).decode()

    cases: list[tuple[str, str]] = [
        ("whole", b64(json.dumps(payload()).encode())),
        (
            "from lowercase",
            b64(json.dumps(payload(authorization={"from": transfer.payer.lower()})).encode()),
        ),
        ("unknown fields", b64(json.dumps(payload({"anything": {"else": 1}})).encode())),
        (
            "a resource",
            b64(
                json.dumps(
                    payload({"resource": {"url": "https://relay.example/x", "tags": ["a"]}})
                ).encode()
            ),
        ),
        (
            "value the largest uint256",
            b64(json.dumps(payload(authorization={"value": str(2**256 - 1)})).encode()),
        ),
        ("no padding", b64(json.dumps(payload()).encode()).rstrip("=")),
        ("whitespace", " " + b64(json.dumps(payload()).encode())),
        ("not base64", "!" + b64(json.dumps(payload()).encode())[1:]),
        ("not UTF-8", b64(b"\xff\xfe" + json.dumps(payload()).encode())),
        ("not JSON", b64(b"{")),
        ("no accepted amount", b64(json.dumps(payload(accepted={"amount": _ABSENT})).encode())),
        (
            "maxTimeoutSeconds a string",
            b64(json.dumps(payload(accepted={"maxTimeoutSeconds": "600"})).encode()),
        ),
        ("x402Version a string", b64(json.dumps(payload({"x402Version": "2"})).encode())),
        (
            "extra's name a number",
            b64(json.dumps(payload(accepted={"extra": {"name": 1, "version": "2"}})).encode()),
        ),
        (
            "extra's assetTransferMethod echoed",
            b64(
                json.dumps(
                    payload(
                        accepted={
                            "extra": {
                                "name": DOMAIN.name,
                                "version": DOMAIN.version,
                                "assetTransferMethod": "permit2",
                                "paymentFlow": "authorization",
                            }
                        }
                    )
                ).encode()
            ),
        ),
        (
            "extra's paymentFlow a number",
            b64(
                json.dumps(
                    payload(
                        accepted={
                            "extra": {
                                "name": DOMAIN.name,
                                "version": DOMAIN.version,
                                "paymentFlow": 1,
                            }
                        }
                    )
                ).encode()
            ),
        ),
        (
            "a resource without url",
            b64(json.dumps(payload({"resource": {"description": "x"}})).encode()),
        ),
        (
            "tags not strings",
            b64(json.dumps(payload({"resource": {"url": "u", "tags": [1]}})).encode()),
        ),
        ("extensions a list", b64(json.dumps(payload({"extensions": []})).encode())),
        (
            "value with a leading zero",
            b64(json.dumps(payload(authorization={"value": "01"})).encode()),
        ),
        (
            "value 2**256",
            b64(json.dumps(payload(authorization={"value": str(2**256)})).encode()),
        ),
        (
            "value of 79 digits",
            b64(json.dumps(payload(authorization={"value": "1" * 79})).encode()),
        ),
        (
            "a short nonce",
            b64(json.dumps(payload(authorization={"nonce": "0x01"})).encode()),
        ),
        (
            "a 64-byte signature",
            b64(json.dumps(payload(signed="0x" + signature[:64].hex())).encode()),
        ),
        (
            "a contract's signature, 4096 bytes",
            b64(json.dumps(payload(signed="0x" + "ab" * MAX_SIGNATURE_BYTES)).encode()),
        ),
        (
            "a signature over 4096 bytes",
            b64(json.dumps(payload(signed="0x" + "ab" * (MAX_SIGNATURE_BYTES + 1))).encode()),
        ),
        ("an empty signature", b64(json.dumps(payload(signed="0x")).encode())),
        ("a signature of half a byte", b64(json.dumps(payload(signed="0xabc")).encode())),
        (
            "from not an address",
            b64(json.dumps(payload(authorization={"from": "0x12"})).encode()),
        ),
    ]
    outcomes: list[dict[str, object]] = []
    for name, header in cases:
        try:
            payload_of(header)
            taken = True
        except MalformedPaymentError:
            taken = False
        outcomes.append({"name": name, "header": header, "taken": taken})
    return outcomes


def body_cases() -> list[dict[str, object]]:
    """x402 request bodies, each as the reference reads it (params_of): a call, or none."""
    signed = authorization_json(authorization("alice", 0))
    call = {"to": address("target"), "data": "0x" + data(4).hex()}

    def body(**changes: object) -> str:
        raw: dict[str, object] = {"call": call, "gas": hex(45_000)}
        raw.update(changes)
        return json.dumps({k: v for k, v in raw.items() if v is not _ABSENT})

    cases: list[tuple[str, str]] = [
        ("a call", body()),
        ("with an authorization", body(authorizationList=[signed])),
        ("gas past a safe integer", body(gas=hex(2**53))),
        (
            "an authorization's nonce 2**64",
            body(authorizationList=[{**signed, "nonce": hex(2**64)}]),
        ),
        (
            "an authorization's yParity 2",
            body(authorizationList=[{**signed, "yParity": "0x2"}]),
        ),
        ("unknown fields", body(anything=1)),
        ("empty", ""),
        ("not JSON", "{"),
        ("a list", "[]"),
        ("no call", body(call=_ABSENT)),
        ("gas in capitals", body(gas="0xAFC8")),
        ("gas with a leading zero", body(gas="0x0afc8")),
        ("data of an odd length", body(call={**call, "data": "0x123"})),
        ("to not an address", body(call={**call, "to": "0x12"})),
        ("authorizationList null", body(authorizationList=None)),
    ]
    return [{"name": n, "body": b, "call": params_of(b.encode()) is not None} for n, b in cases]


def x402_vectors() -> dict[str, object]:
    """EIP-3009: each authorization's digest and calldata; a key's transfer's bounds; deadlines
    from validBefore."""
    token, payee = address("token"), address("payee")
    authorizations: list[dict[str, object]] = []
    # each signed as a key signs; with v as 0 or 1 (read as 27 or 28); and a contract's
    # signature, longer than 65 bytes (sent by the bytes overload)
    signed = [
        transfer_authorization(name, value, before)
        for name, value, before in (("alice", 1, 2**256 - 1), ("bob", 10**6, 1_800_000_000))
    ]
    transfers = [
        *signed,
        replace(
            signed[0], signature=signed[0].signature[:64] + bytes([signed[0].signature[64] - 27])
        ),
        replace(signed[1], signature=CONTRACT_SIGNATURE),
    ]
    for transfer in transfers:
        value, before = transfer.value, transfer.valid_before
        authorizations.append(
            {
                "domain": {"name": DOMAIN.name, "version": DOMAIN.version},
                "chainId": CHAIN_ID,
                "token": token,
                "payer": transfer.payer,
                "to": transfer.to,
                "value": str(value),
                "validBefore": str(before),
                "nonce": "0x" + transfer.nonce.hex(),
                "signature": "0x" + transfer.signature.hex(),
                "digest": "0x"
                + digest(
                    DOMAIN, CHAIN_ID, token, transfer.payer, payee, value, 0, before, transfer.nonce
                ).hex(),
                "calldata": "0x" + transfer.calldata().hex(),
            }
        )
    bounds = [
        {
            "fork": schedule.name,
            "measured": TRANSFER_GAS,
            "payeeHolds": holds,
            "gas": transfer_gas(TRANSFER_GAS, schedule, holds),
        }
        for schedule in SCHEDULES.values()
        for holds in (False, True)
    ]
    latest = Latest(100, 1_000_000, 0, 0, ())
    deadlines = [
        {"validBefore": str(b), "slotSeconds": 12, "block": last_block_before(latest, b, 12)}
        for b in (0, 999_999, 1_000_000, 1_000_001, 1_000_012, 1_000_013, 1_000_600)
    ]
    return {
        "payloads": payload_cases(),
        "bodies": body_cases(),
        "latest": {"number": latest.number, "timestamp": latest.timestamp},
        "authorizations": authorizations,
        "transferGas": bounds,
        "deadlines": deadlines,
    }


# the float the signing vectors use: test keys, from fixed strings
FLOAT = Account.from_key(keccak(text="the vectors' float"))
CHAIN_ID = 11_155_111
FEES = Fees(max_fee_per_gas=3 * 10**9 + 7, max_priority_fee_per_gas=10**9)


def client(name: str) -> LocalAccount:
    return Account.from_key(keccak(text=name))


def authorization(name: str, nonce: int, chain_id: int = CHAIN_ID) -> SignedSetCodeAuthorization:
    return Account.sign_authorization(
        {"chainId": chain_id, "address": address("delegate"), "nonce": nonce}, client(name).key
    )


def authorization_vector(auth: SignedSetCodeAuthorization) -> dict[str, object]:
    return {
        "chainId": str(auth.chain_id),
        "address": to_checksum_address(auth.address),
        "nonce": str(auth.nonce),
        "yParity": auth.y_parity,
        "r": str(auth.r),
        "s": str(auth.s),
    }


def signed(tx: SignedTransaction) -> dict[str, object]:
    return {
        "raw": "0x" + tx.raw.hex(),
        "hash": "0x" + tx.hash.hex(),
        "sender": tx.sender,
        "maxCost": str(tx.max_cost),
    }


def transactions() -> list[dict[str, object]]:
    """Signed transactions: type 2 to an address and creating, type 4 with authorizations."""
    out: list[dict[str, object]] = []
    cases = (
        ("to an address", 0, 50_000, address("to"), 100, 0, ()),
        ("with value", 7, 21_000, address("to"), 0, 10**18, ()),
        ("creation", 1, 100_000, None, 1_000, 0, ()),
        ("one authorization", 2**20, 500_000, DEPLOYER, 300, 0, (("alice", 0),)),
        ("two authorizations", 3, 500_000, DEPLOYER, 0, 0, (("alice", 5), ("bob", 0))),
    )
    for index, (name, nonce, gas, to, length, value, auths) in enumerate(cases):
        account = derive(FLOAT, index)
        authorizations = [authorization(n, k) for n, k in auths]
        for fee in (None, DATA_FEES[2]):
            fees = replace(FEES, data_fee=fee)
            tx = sign(account, CHAIN_ID, nonce, fees, gas, to, data(length), value, authorizations)
            out.append(
                {
                    "name": name,
                    "account": index,
                    "nonce": nonce,
                    "gas": gas,
                    "to": to,
                    "dataLength": length,
                    "value": str(value),
                    "authorizations": [authorization_vector(a) for a in authorizations],
                    "dataFee": None if fee is None else data_fee_vector(fee),
                    **signed(tx),
                }
            )
    return out


def drains() -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for index, balance in enumerate((10**16, 21_000 * 10**9 + 1, 21_000 * 10**9)):
        for fee in (None, DATA_FEES[0]):
            tx = sign_drain(
                derive(FLOAT, index), CHAIN_ID, 4, balance, 10**9, FLOAT.address, 21_000, fee
            )
            out.append(
                {
                    "account": index,
                    "balance": str(balance),
                    "dataFee": None if fee is None else data_fee_vector(fee),
                    **({} if tx is None else signed(tx)),
                }
            )
    return out


# an OP-stack chain's inputs: Base's on 2026-10-23, a testnet's (an L1 fee of almost
# nothing), and large ones with an operator fee
DATA_FEES = (
    DataFee(135_000_000, 8_849_538, 2269, 1_055_762, 0, 0),
    DataFee(15, 1, 1101, 659_851, 0, 0),
    DataFee(37_000_000_017, 10**12 + 3, 10**6, 10**7, 7, 10**15 + 1),
)


def data_fee_vector(fee: DataFee) -> dict[str, str]:
    return {
        "l1BaseFee": str(fee.l1_base_fee),
        "blobBaseFee": str(fee.blob_base_fee),
        "baseFeeScalar": str(fee.base_fee_scalar),
        "blobBaseFeeScalar": str(fee.blob_base_fee_scalar),
        "operatorFeeScalar": str(fee.operator_fee_scalar),
        "operatorFeeConstant": str(fee.operator_fee_constant),
    }


def data_fee_vectors() -> dict[str, object]:
    """The OP-stack fee formulas over sizes and gas either side of the regression's floor,
    their inputs risen, the oracle's calls, and transactions' most bytes unsigned."""
    return {
        "fees": [
            {
                "inputs": data_fee_vector(fee),
                "l1Fees": [
                    {"size": n, "fee": str(fee.l1_fee(n))}
                    for n in (0, 1, 32, 100, 199, 200, 1_000, 40_000, 131_072)
                ],
                "operatorFees": [
                    {"gas": g, "fee": str(fee.operator_fee(g))} for g in (0, 1, 21_000, 10**6)
                ],
                "risen": [
                    {"blocks": b, "inputs": data_fee_vector(fee.risen(b))} for b in (0, 1, 3, 10)
                ],
            }
            for fee in DATA_FEES
        ],
        "reading": [c.params for c in datafee.reading()],
        "checking": [c.params for c in datafee.checking("0x10")],
        "sizes": [
            {
                "to": to,
                "dataLength": length,
                "value": value,
                "authorizations": auths,
                "size": unsigned_size(CHAIN_ID, to, data(length), value, auths),
            }
            for to in (None, address("to"))
            for length in (0, 55, 56, 1_000, 40_960)
            for value in (0, 1)
            for auths in ((0,) if to is None else (0, 1, 3))
        ],
    }


def fundings() -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for schedule in SCHEDULES.values():
        for count in (1, 3):
            top_ups = [
                TopUp(derive(FLOAT, i).address, 10**15 + i, new_account=i % 2 == 0)
                for i in range(count)
            ]
            initcode, gas = funding_initcode(top_ups, schedule)
            tx = sign_funding(FLOAT, CHAIN_ID, 9, FEES, top_ups, schedule)
            out.append(
                {
                    "fork": schedule.name,
                    "topUps": [
                        {"account": t.account, "amount": str(t.amount), "newAccount": t.new_account}
                        for t in top_ups
                    ],
                    "initcode": "0x" + initcode.hex(),
                    "gas": gas,
                    "transaction": signed(tx),
                }
            )
    return out


def bundle_vectors() -> list[dict[str, object]]:
    """Both modes' eth_sendBundle parameters, over two signed transactions."""
    txs = [
        sign(derive(FLOAT, i), CHAIN_ID, 0, FEES, 50_000, address("to"), data(10)) for i in range(2)
    ]
    top_ups = [TopUp(tx.sender, 10**15, new_account=True) for tx in txs]
    funding = sign_funding(FLOAT, CHAIN_ID, 0, FEES, top_ups, SCHEDULES["amsterdam"])
    return [
        {
            "mode": mode.value,
            "block": 1_000_000,
            "funding": funding is not None and mode is Mode.DROPPING,
            "params": bundles(mode, 1_000_000, funding if mode is Mode.DROPPING else None, txs),
        }
        for mode in (Mode.DROPPING, Mode.STANDALONE)
    ]


def rlp_vectors() -> list[dict[str, object]]:
    """RLP on each side of its boundaries: a single byte under 0x80 and at it; strings and
    lists of 55 and 56 bytes (where the length moves into its own bytes), and of 255 and 256
    (where that length takes a second byte); integers 0, 127, 128 and a uint256's largest."""
    out: list[dict[str, object]] = []
    for length in (0, 1, 2, 55, 56, 255, 256, 1_024):
        string = data(length)
        out.append({"kind": "bytes", "length": length, "rlp": "0x" + rlp(string).hex()})
        # a list whose payload is `length` bytes: single bytes under 0x80, each encoding itself
        items = [b"\x01"] * length
        out.append({"kind": "list", "length": length, "rlp": "0x" + rlp(list(items)).hex()})
    out.extend(
        {"kind": "int", "value": str(value), "rlp": "0x" + rlp(value).hex()}
        for value in (0, 1, 127, 128, 255, 256, 2**64, 2**256 - 1)
    )
    return out


# the most bytes a QR code holds in byte mode at error correction level M, versions 1 to 10:
# the Worker's encoder goes no further
QR_CAPACITIES = (14, 26, 42, 62, 84, 106, 122, 152, 180, 213)


def qr_vectors() -> list[dict[str, object]]:
    """QR codes as the reference makes them: at each version's capacity and just past it
    (each mask chosen by its penalty), and payees' addresses."""
    texts = [address("payee"), address("another payee"), "a"]
    for n in QR_CAPACITIES:
        texts.append((keccak(text=f"qr {n}").hex() * 4)[:n])
        if n != QR_CAPACITIES[-1]:
            texts.append((keccak(text=f"qr {n} past").hex() * 4)[: n + 1])
    # printable bytes of every kind, so that each mask is the best for some
    for seed in range(24):
        raw = keccak(text=f"qr mask {seed}") * 4
        texts.append("".join(chr(33 + b % 94) for b in raw[: 10 + 9 * seed % 200]))
    return [
        {"text": text, "rows": ["".join(str(m) for m in row) for row in qr(text)]} for text in texts
    ]


def tip_samples() -> Iterator[list[int]]:
    """Blocks' median tips, oldest first: none, a few, a full window, ones whose recent
    blocks tip more (so the recent median wins) and less, and some blocks tipping nothing."""
    window = [int.from_bytes(keccak(text=f"tip {i}")[:4]) for i in range(TIP_BLOCKS)]
    yield []
    yield [7]
    yield window[:4]
    yield window[:5]
    yield window[:6]
    yield window
    yield window[:-5] + [10**12] * 5
    yield [10**12] * 15 + window[:5]
    yield [0] * 12 + window[:8]


def router_vectors() -> dict[str, object]:
    """The router's calldata: a swap along one v3 pool, two, a v2 pair, and v2 then v3 then
    v2 (three legs); an approval and a withdraw."""
    v3, v2 = PoolKind.UNISWAP_V3, PoolKind.UNISWAP_V2
    a, b, c, weth = address("token a"), address("token b"), address("token c"), address("weth")
    paths = {
        "one v3 pool": [Step(v3, a, weth, 3000)],
        "two v3 pools": [Step(v3, a, b, 500), Step(v3, b, weth, 3000)],
        "a v2 pair": [Step(v2, a, weth, 0)],
        "v2, v3, v2": [
            Step(v2, a, b, 0),
            Step(v3, b, c, 100),
            Step(v3, c, weth, 10000),
            Step(v2, weth, weth, 0),
        ],
    }
    return {
        "swaps": [
            {
                "name": name,
                "steps": [
                    {
                        "kind": s.kind.value,
                        "tokenIn": s.token_in,
                        "tokenOut": s.token_out,
                        "fee": s.fee,
                    }
                    for s in steps
                ],
                "amountIn": str(amount),
                "minOut": str(min_out),
                "recipient": address("recipient"),
                "calldata": "0x"
                + swap_calldata(steps, amount, min_out, address("recipient")).hex(),
            }
            for name, steps in paths.items()
            for amount, min_out in ((1, 0), (10**6, 4 * 10**14), (2**256 - 1, 2**200))
        ],
        "approve": {
            "spender": address("router"),
            "amount": str(2**256 - 1),
            "calldata": "0x" + approve_calldata(address("router"), 2**256 - 1).hex(),
        },
        "withdraw": {"amount": str(10**18), "calldata": "0x" + withdraw_calldata(10**18).hex()},
    }


def vectors() -> dict[str, object]:
    return {
        "qr": qr_vectors(),
        "deployer": {
            "runtime": "0x" + RUNTIME.hex(),
            "initcode": "0x" + INITCODE.hex(),
            "address": DEPLOYER,
            "overheadGas": [
                {"fork": s.name, "initcodeLength": n, "gas": overhead_gas(n, s)}
                for s in SCHEDULES.values()
                for n in (0, 1, 32, 33, 1_000, 49_152)
            ],
        },
        "errors": {
            "underpaid": "0x" + UNDERPAID.hex(),
            "balanceUnreadable": "0x" + BALANCE_UNREADABLE.hex(),
            "gasExhausted": "0x" + GAS_EXHAUSTED.hex(),
        },
        "nativeAsset": NATIVE_ASSET,
        "intrinsic": [
            {
                "fork": s.name,
                "calldataLength": n,
                "creation": creation,
                "authorizations": a,
                "standard": s.intrinsic(data(n), creation=creation, authorizations=a)[0],
                "floor": s.intrinsic(data(n), creation=creation, authorizations=a)[1],
            }
            for s in SCHEDULES.values()
            for n in (0, 1, 100, 1_000)
            for creation in (False, True)
            for a in ((0,) if creation else (0, 1, 2))
        ],
        "rlp": rlp_vectors(),
        "tips": [
            {"tips": [str(t) for t in tips], "tip": str(market_tip(tips))} for tips in tip_samples()
        ],
        "router": router_vectors(),
        "risen": [
            {"baseFee": str(base), "blocks": blocks, "fee": str(risen(base, blocks))}
            for base in (0, 1, 7, 10**9, 37_000_000_017)
            for blocks in (0, 1, 2, 3, 10)
        ],
        "chainId": CHAIN_ID,
        "float": {"key": "0x" + FLOAT.key.hex(), "address": FLOAT.address},
        "fees": {
            "maxFeePerGas": str(FEES.max_fee_per_gas),
            "maxPriorityFeePerGas": str(FEES.max_priority_fee_per_gas),
        },
        "accounts": [{"index": i, "address": derive(FLOAT, i).address} for i in range(5)],
        "authorities": [
            {
                "authorization": authorization_vector(authorization(name, nonce, chain_id)),
                "authority": client(name).address,
            }
            for name, nonce, chain_id in (("alice", 0, CHAIN_ID), ("bob", 3, 0))
        ],
        "transactions": transactions(),
        "dataFee": data_fee_vectors(),
        "drains": drains(),
        "fundings": fundings(),
        "bundles": bundle_vectors(),
        "flashbots": [
            {
                "body": body,
                "identity": "0x" + client("identity").key.hex(),
                "header": flashbots_signature(body.encode(), client("identity")),
            }
            for body in ('{"jsonrpc":"2.0","id":1,"method":"eth_sendBundle","params":[]}', "")
        ],
        "runners": [r for s in SCHEDULES.values() for r in runners(s)],
        "refusals": [r for s in SCHEDULES.values() for r in refusals(s)],
        "x402": x402_vectors(),
        "erc6492": erc6492_vectors(),
        "transferRunners": [r for s in SCHEDULES.values() for r in transfer_runners(s)],
        "submissionIds": submission_id_vectors(),
        "quotedCalldata": [
            {"signatureBytes": n, "length": len(quoted_calldata(n))}
            for n in (None, 0, 1, 65, 224, 225)
        ],
        "userOperations": user_operation_vectors(),
        "wallets": [
            {
                "fork": schedule.name,
                "deployment": None if deployment is None else deployment_json(deployment),
                "gas": verification(deployment, schedule).gas,
                "signatureBytes": verification(deployment, schedule).signature_bytes,
            }
            for schedule in SCHEDULES.values()
            for deployment in wallet_deployments()
        ],
    }


def user_operation_vectors() -> dict[str, object]:
    """UserOps as the relay simulates them: each op's simulateHandleOp calldata at a limit (an
    op paying by its fees given no deposit, an EIP-7702 account's given its authorization's
    delegation), and simulateHandleOp's answers read as estimates by each EntryPoint (the
    penalty on unused call gas taken out unless v0.8 leaves it, the margins added, an execution
    that comes out below zero taken as none; the rest of a transaction's gas as
    preVerificationGas)."""
    sender, factory = address("an op's account"), address("an op's factory")
    v07, v08 = ENTRY_POINT_0_7, ENTRY_POINT_0_8
    eoa = client("an EIP-7702 account")
    auth = Account.sign_authorization(
        {"chainId": CHAIN_ID, "address": address("a delegate"), "nonce": 3}, eoa.key
    )
    ops = {
        "undeployed": UserOperation(
            v07, sender, 0, factory, data(100), data(68), 0, 0, 0, 0, 0, data(65)
        ),
        "deployed": UserOperation(v07, sender, 7, None, b"", data(300), 1, 2, 3, 0, 0, data(86)),
        "empty": UserOperation(v07, sender, 2**64, None, b"", b"", 0, 0, 0, 0, 0, b""),
        "paying by its fees": UserOperation(
            v07, sender, 3, None, b"", data(40), 5, 6, 7, 3 * 10**9, 10**9, data(65)
        ),
        "a flat fee": UserOperation(
            v07, sender, 4, factory, data(20), data(40), 5, 6, 7, 2 * 10**9, 2 * 10**9, data(65)
        ),
        "v0.8, undeployed": UserOperation(
            v08, sender, 0, factory, data(100), data(68), 0, 0, 0, 0, 0, data(65)
        ),
        "v0.8, paying by its fees": UserOperation(
            v08, sender, 3, None, b"", data(40), 5, 6, 7, 3 * 10**9, 10**9, data(65)
        ),
        "an EIP-7702 account": UserOperation(
            v08, eoa.address, 0, EIP7702_MARKER, b"", data(68), 0, 0, 0, 0, 0, data(65), auth
        ),
        "an EIP-7702 account, initialised": UserOperation(
            v08, eoa.address, 1, EIP7702_MARKER, data(36), data(68), 1, 2, 3, 4, 4, data(65), auth
        ),
        "an EIP-7702 account, delegated": UserOperation(
            v08, eoa.address, 2, EIP7702_MARKER, b"", data(68), 1, 2, 3, 0, 0, data(65)
        ),
        "v0.8, a paymaster's": UserOperation(
            v08,
            sender,
            5,
            None,
            b"",
            data(68),
            0,
            0,
            0,
            3 * 10**9,
            10**9,
            data(65),
            paymaster=Paymaster(address("a paymaster"), None, None, data(117)),
        ),
        "v0.8, a paymaster's, its limits given": UserOperation(
            v08,
            sender,
            6,
            None,
            b"",
            data(68),
            1,
            2,
            3,
            3 * 10**9,
            10**9,
            data(65),
            paymaster=Paymaster(address("a paymaster"), 200_000, 35_000, data(117)),
        ),
        "v0.7, a paymaster's": UserOperation(
            v07,
            sender,
            7,
            None,
            b"",
            data(68),
            0,
            0,
            0,
            2 * 10**9,
            2 * 10**9,
            data(65),
            paymaster=Paymaster(address("a paymaster"), None, None, b""),
        ),
    }

    def op_json(op: UserOperation) -> dict[str, object]:
        fields: dict[str, object] = {
            "sender": op.sender,
            "nonce": hex(op.nonce),
            "factory": "0x7702" if op.eip7702 else op.factory,
            "factoryData": "0x" + op.factory_data.hex(),
            "callData": "0x" + op.call_data.hex(),
            "callGasLimit": hex(op.call_gas_limit),
            "verificationGasLimit": hex(op.verification_gas_limit),
            "preVerificationGas": hex(op.pre_verification_gas),
            "maxFeePerGas": hex(op.max_fee_per_gas),
            "maxPriorityFeePerGas": hex(op.max_priority_fee_per_gas),
            "signature": "0x" + op.signature.hex(),
        }
        if op.paymaster is not None:
            pm = op.paymaster
            fields["paymaster"] = pm.address
            fields["paymasterData"] = "0x" + pm.data.hex()
            if pm.verification_gas_limit is not None:
                fields["paymasterVerificationGasLimit"] = hex(pm.verification_gas_limit)
            if pm.post_op_gas_limit is not None:
                fields["paymasterPostOpGasLimit"] = hex(pm.post_op_gas_limit)
        if op.authorization is not None:
            # as viem writes it: its signature's numbers padded
            a = op.authorization
            fields["eip7702Auth"] = {
                "chainId": hex(a.chain_id),
                "address": to_checksum_address(a.address),
                "nonce": hex(a.nonce),
                "yParity": f"0x{a.y_parity:02x}",
                "r": f"0x{a.r:064x}",
                "s": f"0x{a.s:064x}",
            }
        return fields

    calldata = [
        {
            "name": name,
            "entryPoint": op.entry_point.address,
            "op": op_json(op),
            "limit": limit,
            "calls": [
                {"data": c.params[0]["data"], "overrides": c.params[2]}
                for c in estimating(op, limit)
            ],
        }
        for name, op in ops.items()
        for limit in (2_000_000, 1)
    ]

    def answer(pre_op_gas: int, spent: int) -> bytes:
        return encode(
            ["(uint256,uint256,uint256,uint256,bool,bytes)"],
            [(pre_op_gas, spent, 1, 0, False, data(5))],
        )

    # each op's simulations' answers (with a paymaster, with it then without it), at a limit
    cases: list[tuple[str, list[tuple[int, int]], int]] = [
        (name, [run], limit)
        for name in ("deployed", "v0.8, undeployed")
        for run, limit in (
            ((293_361, 528_882), 2_000_000),
            ((50_000, 50_000), 1_000_000),
            ((21_000, 100_000), 300_000),
            ((10_000, 90_000), 100_000),
            ((0, 0), 0),
        )
    ]
    cases += [
        ("v0.8, a paymaster's", [(160_000, 610_000), (90_000, 300_000)], 2_000_000),
        (
            "v0.8, a paymaster's, its limits given",
            [(160_000, 450_000), (90_000, 300_000)],
            2_000_000,
        ),
        ("v0.7, a paymaster's", [(160_000, 800_000), (90_000, 300_000)], 2_000_000),
        ("v0.8, a paymaster's", [(90_000, 90_000), (90_000, 300_000)], 2_000_000),
    ]
    answers: list[dict[str, object]] = []
    for name, runs, limit in cases:
        op = ops[name]
        encoded = [answer(*run) for run in runs]
        gas = used(encoded, op, limit)
        estimate = estimated(gas, op)
        gas_limit = 600_000
        answers.append(
            {
                "name": name,
                "answers": ["0x" + a.hex() for a in encoded],
                "limit": limit,
                "verificationGasLimit": estimate.verification_gas_limit,
                "callGasLimit": estimate.call_gas_limit,
                "paymasterVerificationGasLimit": estimate.paymaster_verification_gas_limit,
                "paymasterPostOpGasLimit": estimate.paymaster_post_op_gas_limit,
                "gasLimit": gas_limit,
                "preVerificationGas": with_overhead(estimate, gas, gas_limit).pre_verification_gas,
            }
        )
    payee, beneficiary = address("an op's payee"), address("a beneficiary")
    tokens = [NATIVE_ASSET, address("a token"), address("another token")]
    paying: list[dict[str, object]] = []
    for name, old, new in (
        ("paid", [1, 2, 3, 0], [1, 7, 3, 0]),
        ("lost", [5, 5, 5, 0], [4, 5, 6, 0]),
    ):
        before, after = encode(["uint256"] * 4, old), encode(["uint256"] * 4, new)
        paying.append(
            {
                "name": name,
                "before": "0x" + before.hex(),
                "after": "0x" + after.hex(),
                "paid": paid(before, after, tokens),
            }
        )
    fees: list[dict[str, object]] = []
    # each run's ETH paid and base fee: the least gas charged in either is what's paid
    for name, runs, base_fee in (
        ("paying by its fees", [(10**9 * 400_000, 0), (3 * 10**9 * 400_010, 2 * 10**9)], 7 * 10**8),
        ("paying by its fees", [(10**9 * 1_000, 0), (4 * 10**9 * 990 + 17, 3 * 10**9)], 10**10),
        ("a flat fee", [(2 * 10**9 * 300_015, 0), (2 * 10**9 * 300_000, 10**9)], 5 * 10**9),
    ):
        afters = [encode(["uint256"] * 4, [eth, 0, 0, run_base_fee]) for eth, run_base_fee in runs]
        payment = fees_paid(
            ops[name],
            [(eth, after) for (eth, _), after in zip(runs, afters, strict=True)],
            len(tokens),
        )
        fees.append(
            {
                "name": name,
                "runs": [
                    {"eth": eth, "after": "0x" + after.hex()}
                    for (eth, _), after in zip(runs, afters, strict=True)
                ],
                "gas": payment.gas,
                "baseFee": base_fee,
                "paid": payment.at(base_fee),
                "paidAnyway": payment.at(0),
            }
        )
    failed_op = keccak(text="FailedOp(uint256,string)")[:4]
    refusals: list[dict[str, object]] = []
    for name, revert in (
        ("its signature", failed_op + encode(["uint256", "string"], [0, "AA24 signature error"])),
        (
            "its paymaster's signature",
            failed_op + encode(["uint256", "string"], [0, "AA34 signature error"]),
        ),
        ("its prefund", failed_op + encode(["uint256", "string"], [0, "AA21 didn't pay prefund"])),
        ("another error", keccak(text="Other()")[:4] + data(32)),
        ("nothing", b""),
    ):
        refused = refusal(revert)
        refusals.append(
            {
                "name": name,
                "revert": "0x" + revert.hex(),
                "field": None if refused is None else refused.field,
            }
        )
    return {
        "calldata": calldata,
        "estimates": answers,
        "hashes": [
            {
                "name": name,
                "chainId": chain_id,
                "delegate": None if delegate is None else "0x" + delegate.hex(),
                "hash": "0x" + user_operation_hash(op, chain_id, delegate).hex(),
            }
            for name, op in ops.items()
            for chain_id in (1, 31337)
            for delegate in [bytes.fromhex(address("a delegate")[2:]) if op.eip7702 else None]
        ],
        "handleOps": [
            {
                "name": name,
                "beneficiary": beneficiary,
                "calldata": "0x" + handle_ops(op, beneficiary).hex(),
            }
            for name, op in ops.items()
        ],
        "balances": {
            "payee": payee,
            "tokens": tokens,
            "code": "0x" + balances_code(v07, payee, tokens).hex(),
            "sending": [
                {"name": name, "method": c.method, "params": c.params}
                for name in ("undeployed", "an EIP-7702 account")
                for c in sending(ops[name], payee, tokens)
            ],
        },
        "paid": paying,
        "feePayments": fees,
        "refusals": refusals,
        "receipts": user_operation_receipt_vectors(),
    }


def user_operation_receipt_vectors() -> list[dict[str, object]]:
    """Transactions' receipts read as an op's: its execution's logs (after BeforeExecution,
    before its event), its event's fields, the revert data if it failed; an op the receipt
    holds no event of, none."""
    op_hash = keccak(text="an op")
    sender, paymaster = address("an op's account"), "0x" + "00" * 20
    topic = "0x" + op_hash.hex()

    def word_of(account: str) -> str:
        return "0x" + "00" * 12 + account[2:].lower()

    def log(
        topics: list[str], data_: bytes, emitter: str = address("a log's contract")
    ) -> dict[str, object]:
        return {"address": emitter, "topics": topics, "data": "0x" + data_.hex()}

    before = log(["0x" + keccak(text="BeforeExecution()").hex()], b"")
    transfer = log(["0x" + keccak(text="Transfer(address,address,uint256)").hex()], data(32))
    event_topic = (
        "0x"
        + keccak(
            text="UserOperationEvent(bytes32,address,address,uint256,bool,uint256,uint256)"
        ).hex()
    )
    out: list[dict[str, object]] = []
    for name, success, reason, emitter in (
        ("succeeded", True, None, ENTRY_POINT_0_7.address),
        ("reverted", False, data(10), ENTRY_POINT_0_8.address.lower()),
        ("from another contract", True, None, address("not an EntryPoint")),
    ):
        event = log(
            [event_topic, topic, word_of(sender), word_of(paymaster)],
            encode(["uint256", "bool", "uint256", "uint256"], [3, success, 0, 302_741]),
            emitter,
        )
        logs = [transfer, before, transfer, event]
        if reason is not None:
            reverted = log(
                [
                    "0x"
                    + keccak(text="UserOperationRevertReason(bytes32,address,uint256,bytes)").hex(),
                    topic,
                    word_of(sender),
                ],
                encode(["uint256", "bytes"], [3, reason]),
                emitter,
            )
            logs = [transfer, before, transfer, reverted, event]
        receipt: dict[str, object] = {"status": "0x1", "logs": logs}
        read = receipt_of(op_hash, receipt)
        out.append(
            {
                "name": name,
                "opHash": topic,
                "receipt": receipt,
                "read": None
                if read is None
                else {
                    "entryPoint": read.entry_point,
                    "sender": read.sender,
                    "nonce": read.nonce,
                    "paymaster": read.paymaster,
                    "actualGasCost": read.actual_gas_cost,
                    "actualGasUsed": read.actual_gas_used,
                    "success": read.success,
                    "reason": "0x" + read.reason.hex(),
                    "logs": read.logs,
                },
            }
        )
    out.append(
        {
            "name": "another op",
            "opHash": "0x" + keccak(text="another op").hex(),
            "receipt": out[0]["receipt"],
            "read": None,
        }
    )
    return out


def wallet_deployments() -> list[Deployment | None]:
    """Deployments the wallet gas schedule finds a wallet for, or not: each Coinbase Smart
    Wallet's factory called; Kernel v3.2's through ZeroDev's meta-factory; the meta-factory
    calling another factory, or called otherwise; Kernel's factory called directly; another
    factory; and none (deployed)."""
    account = address("a wallet")
    meta_factory = KERNEL_3_2.through
    assert meta_factory is not None
    deploy_with_factory = keccak(text="deployWithFactory(address,bytes,bytes32)")[:4]

    def through_meta_factory(factory: ChecksumAddress) -> Deployment:
        calldata = deploy_with_factory + encode(
            ["address", "bytes", "bytes32"], [factory, data(100), bytes(32)]
        )
        return Deployment(account, meta_factory, calldata)

    return [
        Deployment(account, COINBASE_SMART_WALLET.factory, data(100)),
        Deployment(account, COINBASE_SMART_WALLET_1_1.factory, data(100)),
        through_meta_factory(KERNEL_3_2.factory),
        through_meta_factory(address("another factory")),
        Deployment(account, meta_factory, data(36)),
        Deployment(account, KERNEL_3_2.factory, data(100)),
        Deployment(account, address("an unknown factory"), data(100)),
        None,
    ]


def erc6492_vectors() -> list[dict[str, object]]:
    """Signatures unwrapped: ERC-6492's wrapper's factory, its data and the inner signature, or
    refused, as the reference's eth_abi decodes (padding must be zero, data within bounds, and
    no offset into the head; trailing bytes and shared offsets are taken); and one that isn't
    wrapped, as it is."""
    account, factory = address("a wallet"), address("a factory")
    good = encode(["address", "bytes", "bytes"], [factory, data(100), data(224)])
    padded_address = bytearray(good)
    padded_address[0] = 1
    past_the_end = bytearray(good)
    past_the_end[32:64] = (10**6).to_bytes(32)
    enormous = bytearray(good)
    enormous[32:64] = (2**255).to_bytes(32)
    padded_bytes = bytearray(good)
    padded_bytes[96 + 32 + 100] = 7
    shared = bytearray(good)
    shared[64:96] = good[32:64]
    into_head = bytearray(good)
    into_head[32:64] = (32).to_bytes(32)
    cases = {
        "good": good,
        "trailing bytes": good + bytes(32),
        "a shared offset": bytes(shared),
        "an address's padding": bytes(padded_address),
        "an offset past the end": bytes(past_the_end),
        "an enormous offset": bytes(enormous),
        "an offset into its head": bytes(into_head),
        "padding after bytes": bytes(padded_bytes),
        "too short": good[:50],
    }
    out: list[dict[str, object]] = []
    for name, payload in cases.items():
        signature = payload + MAGIC
        entry: dict[str, object] = {
            "name": name,
            "account": account,
            "signature": "0x" + signature.hex(),
        }
        try:
            inner, deployment = unwrapped(signature, account)
        except WrapperError:
            out.append({**entry, "refused": True})
            continue
        assert deployment is not None
        out.append(
            {
                **entry,
                "inner": "0x" + inner.hex(),
                "factory": deployment.factory,
                "factoryData": "0x" + deployment.factory_data.hex(),
            }
        )
    plain = data(65)
    out.append(
        {
            "name": "not wrapped",
            "account": account,
            "signature": "0x" + plain.hex(),
            "inner": "0x" + plain.hex(),
        }
    )
    return out


def transfer_vector(transfer: TransferAuthorization) -> dict[str, object]:
    """A signed transfer as a submission's payment carries it (its `to` and `value` the
    payment's payee and amount)."""
    return {
        "payer": transfer.payer,
        "validAfter": str(transfer.valid_after),
        "validBefore": str(transfer.valid_before),
        "nonce": "0x" + transfer.nonce.hex(),
        "signature": "0x" + transfer.signature.hex(),
    }


def deployment_vector(deployment: Deployment) -> dict[str, object]:
    return {
        "account": deployment.account,
        "factory": deployment.factory,
        "factoryData": "0x" + deployment.factory_data.hex(),
    }


def transfer_runners(schedule: GasSchedule) -> Iterator[dict[str, object]]:
    """Runners whose payment comes with a signed transfer, or that deploy an account first
    (prepared or not), or both: the transfer none, a key's (bounded, for a payee holding the
    token or not) or a contract's (given the gas left over). Each with all its inputs."""
    token, payee, target = address("token"), address("payee"), address("target")
    erc20 = Erc20(token, BALANCE_GAS, TRANSFER_GAS)
    calldata = transfer_authorization("alice", 1, 2**256 - 1).calldata()
    transfers: list[Transfer | None] = [
        None,
        *(Transfer(calldata, transfer_gas(TRANSFER_GAS, schedule, h)) for h in (False, True)),
        Transfer(calldata, None),
    ]
    deployment = Deployment(address("a wallet"), address("a factory"), data(100))
    call = EncodedCall(target, HexStr("0x" + data(100).hex()))
    for deploy, prepare in ((None, False), (deployment, False), (deployment, True)):
        for transfer in transfers:
            if deploy is None and transfer is None:
                continue  # a plain runner: in runners
            payment = Payment(erc20, payee, 1, transfer)
            runner = in_call_runner(
                call, payment, schedule, client_gas=45_000, deployment=deploy, prepare=prepare
            )
            yield {
                "fork": schedule.name,
                "call": {"to": call.to, "dataLength": 100},
                "clientGas": 45_000,
                "payment": {
                    "token": {
                        "address": token,
                        "balanceGas": BALANCE_GAS,
                        "transferGas": TRANSFER_GAS,
                    },
                    "payee": payee,
                    "amount": "1",
                },
                "transfer": None
                if transfer is None
                else {"calldata": "0x" + transfer.calldata.hex(), "gas": transfer.gas},
                "deployment": None if deploy is None else deployment_vector(deploy),
                "prepare": prepare,
                "initcodeKeccak": "0x" + keccak(runner.initcode).hex(),
                "overheadGas": runner.overhead_gas,
            }


def submission_id_vectors() -> list[dict[str, object]]:
    """Submissions' ids: paid by a signed transfer (a key's, or a contract's longer signature),
    with and without a deployment of their own, and a deployment alone."""
    token, payee, target = address("token"), address("payee"), address("target")
    key = transfer_authorization("alice", 1_000, 1_800_000_000)
    contract = replace(key, signature=CONTRACT_SIGNATURE)
    deployment = Deployment(address("a wallet"), address("a factory"), data(100))
    call = EncodedCall(target, HexStr("0x" + data(33).hex()))
    out: list[dict[str, object]] = []
    cases = ((key, False), (key, True), (contract, False), (None, True))
    for transfer, deployed in cases:
        submission = Submission(
            call,
            (),
            45_000,
            token,
            payee,
            key.value,
            123,
            transfer,
            deployment if deployed else None,
        )
        out.append(
            {
                "call": {"to": target, "dataLength": 33},
                "gas": 45_000,
                "token": token,
                "payee": payee,
                "amount": str(key.value),
                "maxBlockNumber": 123,
                "transfer": None if transfer is None else transfer_vector(transfer),
                "deployment": deployment_vector(deployment) if deployed else None,
                "id": "0x" + submission.id.hex(),
            }
        )
    return out


if __name__ == "__main__":
    json.dump(vectors(), sys.stdout, indent=1)
    sys.stdout.write("\n")
