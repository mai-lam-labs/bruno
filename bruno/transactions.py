"""Signing the relay's transactions: EIP-1559 (type 2), or EIP-7702 (type 4) when a
submission carries authorizations; to an address, or (type 2 only) a contract creation. A
drain is EIP-2930 (type 1): it pays exactly gas x gasPrice.

Encoded here as the EIPs define them, an RLP list after the type byte, signed over
keccak(type ‖ rlp(fields)): eth-account's transaction dict has no type for an authorization
list, and the rlp package has no types at all.

And reading back a transaction another account signed (decoded): a sponsorship's, which the
relay sends as it is."""

from collections.abc import Sequence
from dataclasses import dataclass

from eth_account.datastructures import SignedSetCodeAuthorization
from eth_account.signers.local import LocalAccount
from eth_account.typed_transactions.set_code_transaction import Authorization
from eth_keys.datatypes import Signature
from eth_keys.exceptions import BadSignature, ValidationError
from eth_typing import ChecksumAddress, Hash32
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from .common import BrunoError
from .datafee import DataFee
from .evm import SECP256K1_N

# the transaction types' leading bytes (EIP-1559, EIP-7702)
_ACCESS_LIST = 0x01
_DYNAMIC_FEE = 0x02
_SET_CODE = 0x04
# the widest a field not known yet can be: a nonce (EIP-2681) and gas limit 64 bits; a fee,
# a value, a signature's r and s 256
_WIDEST_64 = 2**64 - 1
_WIDEST_256 = 2**256 - 1
# the largest nonce or gas limit a decoded transaction may have: the largest integer a float64
# holds exactly, read alike by both relays (no account's nonce, and no block's gas, gets near it)
_MOST_EXACT = 2**53 - 1


class TransactionError(BrunoError):
    pass


@dataclass(frozen=True, slots=True)
class Fees:
    """EIP-1559's two fees per gas, in wei; and, on an OP-stack chain, the inputs of what a
    transaction pays beyond gas, as high as they can be while these fees are (lasting_fees)."""

    max_fee_per_gas: int
    max_priority_fee_per_gas: int
    data_fee: DataFee | None = None

    def cost(self, gas: int, unsigned_size: int, value: int = 0) -> int:
        """The most a transaction given `gas`, of `unsigned_size` bytes unsigned, sending
        `value`, costs its sender at these fees: its gas at the max fee, its value, and what the
        chain charges beyond gas."""
        beyond = 0 if self.data_fee is None else self.data_fee.beyond_gas(unsigned_size, gas)
        return gas * self.max_fee_per_gas + value + beyond


@dataclass(frozen=True, slots=True)
class SignedTransaction:
    """A signed transaction, and what the relay needs to know of it: who sends it at which
    nonce, and the most it can cost its sender."""

    raw: bytes
    hash: bytes
    sender: ChecksumAddress
    nonce: int
    gas: int
    max_fee_per_gas: int
    value: int
    # what it pays beyond gas at most (an OP-stack chain's data and operator fees), from its
    # own size
    beyond_gas: int = 0

    @property
    def max_cost(self) -> int:
        """The balance its sender needs for it to be valid: all its gas at the max fee, its
        value, and what it pays beyond gas."""
        return self.gas * self.max_fee_per_gas + self.value + self.beyond_gas


@dataclass(frozen=True, slots=True)
class Access:
    """An access list's entry (EIP-2930): an address and its storage keys."""

    address: ChecksumAddress
    storage_keys: tuple[bytes, ...]


@dataclass(frozen=True, slots=True)
class Decoded:
    """A transaction another account signed (EIP-1559 or EIP-7702), read back from its bytes:
    `signed`, as the relay holds any signed transaction (its sender recovered from the
    signature), and the fields it's simulated by; `to` None for a contract creation."""

    signed: SignedTransaction
    chain_id: int
    max_priority_fee_per_gas: int
    to: ChecksumAddress | None
    data: bytes
    access_list: tuple[Access, ...]
    authorizations: tuple[SignedSetCodeAuthorization, ...]


type _Rlp = int | bytes | Sequence[_Rlp]
# what RLP decodes to: a byte string, or a list of them
type _Item = bytes | list[_Item]


def sign(
    account: LocalAccount,
    chain_id: int,
    nonce: int,
    fees: Fees,
    gas: int,
    to: ChecksumAddress | None,
    data: bytes,
    value: int = 0,
    authorizations: Sequence[SignedSetCodeAuthorization] = (),
) -> SignedTransaction:
    """Sign a transaction from `account`; `to` None for a contract creation."""
    if authorizations and to is None:
        raise TransactionError("a type-4 transaction can't create a contract (EIP-7702)")
    fields: list[_Rlp] = [
        chain_id,
        nonce,
        fees.max_priority_fee_per_gas,
        fees.max_fee_per_gas,
        gas,
        b"" if to is None else bytes.fromhex(to[2:]),
        value,
        data,
        [],  # access list
    ]
    kind = _DYNAMIC_FEE
    if authorizations:
        kind = _SET_CODE
        fields.append([_authorization_fields(a) for a in authorizations])
    beyond = 0
    if fees.data_fee is not None:
        beyond = fees.data_fee.beyond_gas(len(_unsigned(kind, fields)), gas)
    return _signed(account, kind, fields, nonce, gas, fees.max_fee_per_gas, value, beyond)


def unsigned_size(
    chain_id: int,
    to: ChecksumAddress | None,
    data: bytes,
    value: int = 0,
    authorizations: int = 0,
) -> int:
    """The most bytes a transaction to `to` carrying `data` (and `authorizations`) can take
    unsigned, before its nonce, fees, gas and authorizations are known: each at its widest."""
    fields: list[_Rlp] = [
        chain_id,
        _WIDEST_64,
        _WIDEST_256,
        _WIDEST_256,
        _WIDEST_64,
        b"" if to is None else bytes.fromhex(to[2:]),
        _WIDEST_256 if value else 0,
        data,
        [],
    ]
    kind = _DYNAMIC_FEE
    if authorizations:
        kind = _SET_CODE
        widest: list[_Rlp] = [_WIDEST_256, bytes(20), _WIDEST_64, 1, _WIDEST_256, _WIDEST_256]
        tuples: list[_Rlp] = [widest] * authorizations
        fields.append(tuples)
    return len(_unsigned(kind, fields))


def sign_drain(
    account: LocalAccount,
    chain_id: int,
    nonce: int,
    balance: int,
    gas_price: int,
    to: ChecksumAddress,
    gas: int,
    data_fee: DataFee | None = None,
) -> SignedTransaction | None:
    """A type-1 transfer of all `balance` to `to`: given `gas`, exactly what the transfer uses
    (GasSchedule.transfer), it pays exactly gas x `gas_price`, so the account ends at 0. On an
    OP-stack chain (`data_fee`) what it pays beyond gas is only bounded: the account ends with
    the bound's slack. None if `balance` doesn't cover the gas."""
    fields: list[_Rlp] = [
        chain_id,
        nonce,
        gas_price,
        gas,
        bytes.fromhex(to[2:]),
        _WIDEST_256,
        b"",
        [],  # access list
    ]
    beyond = (
        0 if data_fee is None else data_fee.beyond_gas(len(_unsigned(_ACCESS_LIST, fields)), gas)
    )
    value = balance - gas * gas_price - beyond
    if value <= 0:
        return None
    fields[5] = value
    return _signed(account, _ACCESS_LIST, fields, nonce, gas, gas_price, value, beyond)


def _signed(
    account: LocalAccount,
    kind: int,
    fields: list[_Rlp],
    nonce: int,
    gas: int,
    max_fee_per_gas: int,
    value: int,
    beyond_gas: int = 0,
) -> SignedTransaction:
    signature = account.unsafe_sign_hash(Hash32(keccak(_unsigned(kind, fields))))
    y_parity = signature.v - 27 if signature.v >= 27 else signature.v
    raw = bytes([kind]) + rlp([*fields, y_parity, signature.r, signature.s])
    return SignedTransaction(
        raw=raw,
        hash=keccak(raw),
        sender=account.address,
        nonce=nonce,
        gas=gas,
        max_fee_per_gas=max_fee_per_gas,
        value=value,
        beyond_gas=beyond_gas,
    )


def _unsigned(kind: int, fields: Sequence[_Rlp]) -> bytes:
    """A typed transaction unsigned: its type byte and its fields' RLP, what's signed."""
    return bytes([kind]) + rlp(fields)


def authorization_json(auth: SignedSetCodeAuthorization) -> dict[str, str]:
    """An EIP-7702 authorization in a transaction's JSON (eth_sendTransaction's shape)."""
    return {
        "chainId": hex(auth.chain_id),
        "address": to_checksum_address(auth.address),
        "nonce": hex(auth.nonce),
        "yParity": hex(auth.y_parity),
        "r": hex(auth.r),
        "s": hex(auth.s),
    }


def _authorization_fields(auth: SignedSetCodeAuthorization) -> list[_Rlp]:
    """An authorization tuple as EIP-7702 lists it: chain_id, address, nonce, y_parity, r, s."""
    return [auth.chain_id, auth.address, auth.nonce, auth.y_parity, auth.r, auth.s]


def rlp(item: _Rlp) -> bytes:
    """RLP (Yellow Paper, appendix B): an integer as its minimal big-endian bytes (0 as
    none), a byte string under 0x80 as itself, else a length-prefixed string or list."""
    if isinstance(item, int):
        item = item.to_bytes((item.bit_length() + 7) // 8)
    if isinstance(item, bytes):
        if len(item) == 1 and item[0] < 0x80:
            return item
        return _rlp_length(len(item), 0x80) + item
    payload = b"".join(rlp(x) for x in item)
    return _rlp_length(len(payload), 0xC0) + payload


def _rlp_length(length: int, offset: int) -> bytes:
    if length < 56:
        return bytes([offset + length])
    encoded = length.to_bytes((length.bit_length() + 7) // 8)
    return bytes([offset + 55 + len(encoded)]) + encoded


def decoded(raw: bytes) -> Decoded:
    """The signed EIP-1559 or EIP-7702 transaction `raw` is, its sender recovered:
    TransactionError if it isn't one, strictly (its RLP canonical, every field in range, its
    signature's s low, EIP-2), so `raw` is the only encoding of what's returned."""
    if not raw or raw[0] not in (_DYNAMIC_FEE, _SET_CODE):
        raise TransactionError("not an EIP-1559 (type 2) or EIP-7702 (type 4) transaction")
    kind = raw[0]
    fields = unrlp(raw[1:])
    count = 13 if kind == _SET_CODE else 12
    if not isinstance(fields, list) or len(fields) != count:
        raise TransactionError(f"a type-{kind} transaction is a list of {count} fields")
    chain_id, nonce = (
        _uint(fields[0], _WIDEST_256, "chainId"),
        _uint(fields[1], _MOST_EXACT, "nonce"),
    )
    tip, max_fee = (
        _uint(fields[2], _WIDEST_256, "maxPriorityFeePerGas"),
        _uint(fields[3], _WIDEST_256, "maxFeePerGas"),
    )
    gas, value = _uint(fields[4], _MOST_EXACT, "gas"), _uint(fields[6], _WIDEST_256, "value")
    to, data = fields[5], fields[7]
    if not isinstance(to, bytes) or len(to) not in ((20,) if kind == _SET_CODE else (0, 20)):
        raise TransactionError("to isn't an address")
    if not isinstance(data, bytes):
        raise TransactionError("data isn't a byte string")
    access_list = _access_list(fields[8])
    authorizations: tuple[SignedSetCodeAuthorization, ...] = ()
    if kind == _SET_CODE:
        listed = fields[9]
        if not isinstance(listed, list) or not listed:
            raise TransactionError("a type-4 transaction lists at least one authorization")
        authorizations = tuple(_authorization(item) for item in listed)
    y_parity, r, s = (
        _uint(f, _WIDEST_256, name) for f, name in zip(fields[-3:], "yrs", strict=True)
    )
    signature = _signature(y_parity, r, s)
    try:
        sender = signature.recover_public_key_from_msg_hash(
            keccak(_unsigned(kind, fields[:-3]))
        ).to_checksum_address()
    except (BadSignature, ValidationError) as e:
        raise TransactionError(f"its signer can't be recovered: {e}") from e
    return Decoded(
        signed=SignedTransaction(
            raw=raw,
            hash=keccak(raw),
            sender=sender,
            nonce=nonce,
            gas=gas,
            max_fee_per_gas=max_fee,
            value=value,
        ),
        chain_id=chain_id,
        max_priority_fee_per_gas=tip,
        to=to_checksum_address(to) if to else None,
        data=data,
        access_list=access_list,
        authorizations=authorizations,
    )


def _signature(y_parity: int, r: int, s: int) -> Signature:
    """A signature whose yParity is 0 or 1, its r and s in range and its s low (EIP-2):
    TransactionError otherwise."""
    if y_parity not in (0, 1):
        raise TransactionError(f"yParity is 0 or 1, not {y_parity}")
    if not (0 < r < SECP256K1_N and 0 < s <= SECP256K1_N // 2):
        raise TransactionError("r and s aren't a signature's, s in the lower half (EIP-2)")
    return Signature(vrs=(y_parity, r, s))


def _access_list(item: _Item) -> tuple[Access, ...]:
    """An access list (EIP-2930) as decoded: each entry an address and its 32-byte keys."""
    if not isinstance(item, list):
        raise TransactionError("the access list isn't a list")
    entries: list[Access] = []
    for entry in item:
        if not isinstance(entry, list) or len(entry) != 2:
            raise TransactionError("an access list's entry is an address and its storage keys")
        address, keys = entry
        if not isinstance(address, bytes) or len(address) != 20 or not isinstance(keys, list):
            raise TransactionError("an access list's entry is an address and its storage keys")
        storage_keys: list[bytes] = []
        for key in keys:
            if not isinstance(key, bytes) or len(key) != 32:
                raise TransactionError("a storage key is 32 bytes")
            storage_keys.append(key)
        entries.append(Access(to_checksum_address(address), tuple(storage_keys)))
    return tuple(entries)


def _authorization(item: _Item) -> SignedSetCodeAuthorization:
    """An authorization tuple as EIP-7702 lists it, decoded: chain_id, address, nonce, y_parity,
    r, s."""
    if not isinstance(item, list) or len(item) != 6:
        raise TransactionError("an authorization is a list of 6 fields")
    address = item[1]
    if not isinstance(address, bytes) or len(address) != 20:
        raise TransactionError("an authorization's address isn't one")
    chain_id, nonce = _uint(item[0], _WIDEST_256, "chainId"), _uint(item[2], _MOST_EXACT, "nonce")
    y_parity, r, s = (_uint(f, _WIDEST_256, name) for f, name in zip(item[3:], "yrs", strict=True))
    return SignedSetCodeAuthorization(
        chain_id=chain_id,
        address=address,
        nonce=nonce,
        y_parity=y_parity,
        r=r,
        s=s,
        signature=_signature(y_parity, r, s),
        authorization_hash=Authorization(chain_id, address, nonce).hash(),
    )


def _uint(item: _Item, most: int, name: str) -> int:
    """An integer as RLP has it, its minimal big-endian bytes, at most `most`."""
    if not isinstance(item, bytes) or (item and item[0] == 0) or len(item) > 32:
        raise TransactionError(f"{name} isn't an integer, minimally encoded")
    value = int.from_bytes(item)
    if value > most:
        raise TransactionError(f"{name} is over {most}")
    return value


def unrlp(data: bytes) -> _Item:
    """The item `data` is the RLP of, and only that: TransactionError unless it's canonical
    (every length minimal, a single byte under 0x80 as itself) with nothing after it."""
    item, end = _unrlp_at(data, 0)
    if end != len(data):
        raise TransactionError("bytes after the RLP item")
    return item


def _unrlp_at(data: bytes, at: int) -> tuple[_Item, int]:
    """The RLP item starting at `at`, and where it ends."""
    if at >= len(data):
        raise TransactionError("the RLP ends early")
    first = data[at]
    if first < 0x80:
        return data[at : at + 1], at + 1
    is_list = first >= 0xC0
    start, length = _rlp_payload(data, at, 0xC0 if is_list else 0x80)
    end = start + length
    if end > len(data):
        raise TransactionError("the RLP ends early")
    if not is_list:
        if length == 1 and data[start] < 0x80:
            raise TransactionError("a single byte under 0x80 is its own RLP")
        return data[start:end], end
    items: list[_Item] = []
    while start < end:
        item, start = _unrlp_at(data[:end], start)
        items.append(item)
    return items, end


def _rlp_payload(data: bytes, at: int, offset: int) -> tuple[int, int]:
    """Where the payload of the string or list at `at` starts, and its length."""
    short = data[at] - offset
    if short < 56:
        return at + 1, short
    width = short - 55
    encoded = data[at + 1 : at + 1 + width]
    if len(encoded) != width or encoded[0] == 0:
        raise TransactionError("an RLP length isn't minimal")
    length = int.from_bytes(encoded)
    if length < 56:
        raise TransactionError("an RLP length isn't minimal")
    return at + 1 + width, length
