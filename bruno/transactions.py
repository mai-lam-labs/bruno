"""Signing the relay's transactions: EIP-1559 (type 2), or EIP-7702 (type 4) when a
submission carries authorizations; to an address, or (type 2 only) a contract creation. A
drain is EIP-2930 (type 1): it pays exactly gas x gasPrice.

Encoded here as the EIPs define them, an RLP list after the type byte, signed over
keccak(type ‖ rlp(fields)): eth-account's transaction dict has no type for an authorization
list, and the rlp package has no types at all."""

from collections.abc import Sequence
from dataclasses import dataclass

from eth_account.datastructures import SignedSetCodeAuthorization
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress, Hash32
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from .common import BrunoError
from .datafee import DataFee

# the transaction types' leading bytes (EIP-1559, EIP-7702)
_ACCESS_LIST = 0x01
_DYNAMIC_FEE = 0x02
_SET_CODE = 0x04
# the widest a field not known yet can be: a nonce (EIP-2681) and gas limit 64 bits; a fee,
# a value, a signature's r and s 256
_WIDEST_64 = 2**64 - 1
_WIDEST_256 = 2**256 - 1


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
        beyond = 0 if self.data_fee is None else self.data_fee.fee(unsigned_size, gas)
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


type _Rlp = int | bytes | list[_Rlp]


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
        fields.append([_authorization(a) for a in authorizations])
    beyond = 0
    if fees.data_fee is not None:
        beyond = fees.data_fee.fee(len(_unsigned(kind, fields)), gas)
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
    beyond = 0 if data_fee is None else data_fee.fee(len(_unsigned(_ACCESS_LIST, fields)), gas)
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


def _unsigned(kind: int, fields: list[_Rlp]) -> bytes:
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


def _authorization(auth: SignedSetCodeAuthorization) -> list[_Rlp]:
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
