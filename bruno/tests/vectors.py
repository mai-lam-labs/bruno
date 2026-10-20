"""Golden vectors for the Worker's TypeScript port (worker/): what the Python reference emits
for a spread of cases, which the port must reproduce byte for byte and gas for gas.

    python -m bruno.tests.vectors > worker/test/vectors.json    (make vectors)

Deterministic: the cases' data and addresses come from keccak of fixed strings, which the
port's tests derive the same way (`data`, `address`), so the vectors carry each call's data
as its length, and each initcode as its length and keccak256."""

import json
import sys
from collections.abc import Iterator

from eth_account import Account
from eth_account.datastructures import SignedSetCodeAuthorization
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress, HexStr
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from bruno.accounts import derive
from bruno.builders import flashbots_signature
from bruno.bundle import Mode, TopUp, bundles, funding_initcode, sign_funding
from bruno.deployer import DEPLOYER, INITCODE, RUNTIME, overhead_gas, transaction_gas
from bruno.evm import NATIVE_ASSET, EncodedCall
from bruno.forks import SCHEDULES, GasSchedule
from bruno.runner import (
    BALANCE_UNREADABLE,
    GAS_EXHAUSTED,
    UNDERPAID,
    Erc20,
    Payment,
    RunnerError,
    in_call_runner,
)
from bruno.server.front import qr
from bruno.transactions import Fees, SignedTransaction, rlp, sign, sign_drain

DATA_LENGTHS = (0, 1, 31, 32, 33, 100, 1_000, 10_000)
AMOUNTS = (1, 1_000_000, 10**18, 2**256 - 1)
CLIENT_GAS = (0, 21_000, 45_000, 1_000_000)
# (authorizations, empty authorities)
AUTHORIZATIONS = ((0, 0), (1, 0), (1, 1), (2, 1))
BALANCE_GAS = 30_000


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
                for delegated in ((), (target,)):
                    call = EncodedCall(target, HexStr("0x" + data(length).hex()))
                    runner = in_call_runner(
                        call, Payment(token, payee, amount), schedule, delegated
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
                        "initcodeLength": len(runner.initcode),
                        "initcodeKeccak": "0x" + keccak(runner.initcode).hex(),
                        "overheadGas": runner.overhead_gas,
                        "frameGas": {str(g): runner.frame_gas(g) for g in CLIENT_GAS},
                        "transactionGas": [
                            {
                                "clientGas": g,
                                "authorizations": a,
                                "emptyAuthorities": e,
                                "gas": transaction_gas(
                                    runner.initcode, runner.frame_gas(g), schedule, a, e
                                ),
                            }
                            for g in CLIENT_GAS
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
            in_call_runner(call, Payment(None, payee, amount), schedule)
        except RunnerError:
            yield {
                "name": name,
                "fork": schedule.name,
                "call": {"to": call.to, "dataLength": length, "value": str(value)},
                "amount": str(amount),
            }
        else:
            raise AssertionError(f"{name}: expected a RunnerError")


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
        tx = sign(account, CHAIN_ID, nonce, FEES, gas, to, data(length), value, authorizations)
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
                **signed(tx),
            }
        )
    return out


def drains() -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for index, balance in enumerate((10**16, 21_000 * 10**9 + 1, 21_000 * 10**9)):
        tx = sign_drain(derive(FLOAT, index), CHAIN_ID, 4, balance, 10**9, FLOAT.address, 21_000)
        out.append(
            {"account": index, "balance": str(balance), **({} if tx is None else signed(tx))}
        )
    return out


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
    }


if __name__ == "__main__":
    json.dump(vectors(), sys.stdout, indent=1)
    sys.stdout.write("\n")
