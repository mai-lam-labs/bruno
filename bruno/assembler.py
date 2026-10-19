"""EVM bytecode assembly with gas bounds: the runner's initcode and the funding initcode are
emitted with it, so each knows the most gas its success path can take when it's emitted."""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum
from math import ceil

from eth_typing import ChecksumAddress

from .common import BrunoError
from .evm import memory_gas


class AssemblyError(BrunoError):
    pass


class Op(IntEnum):
    """The opcodes the runner uses (the Yellow Paper's, and EIP-3855's PUSH0)."""

    ADD = 0x01
    SUB = 0x03
    LT = 0x10
    EQ = 0x14
    ISZERO = 0x15
    AND = 0x16
    NOT = 0x19
    BALANCE = 0x31
    CODECOPY = 0x39
    RETURNDATASIZE = 0x3D
    RETURNDATACOPY = 0x3E
    POP = 0x50
    MLOAD = 0x51
    MSTORE = 0x52
    JUMPI = 0x57
    GAS = 0x5A
    JUMPDEST = 0x5B
    PUSH0 = 0x5F
    PUSH1 = 0x60
    PUSH2 = 0x61
    PUSH3 = 0x62
    PUSH20 = 0x73
    DUP2 = 0x81
    SWAP1 = 0x90
    CALL = 0xF1
    RETURN = 0xF3
    STATICCALL = 0xFA
    REVERT = 0xFD


# each opcode's gas where it's a constant (Yellow Paper, EIP-3855), memory expansion aside:
# the rest are emitted by the methods that bound them
_STATIC_GAS: dict[Op, int] = {
    Op.ADD: 3,
    Op.SUB: 3,
    Op.LT: 3,
    Op.EQ: 3,
    Op.ISZERO: 3,
    Op.AND: 3,
    Op.NOT: 3,
    Op.RETURNDATASIZE: 2,
    Op.POP: 2,
    Op.JUMPI: 10,
    Op.GAS: 2,
    Op.DUP2: 3,
    Op.SWAP1: 3,
    Op.RETURN: 0,
}
_PUSH0_GAS = 2
_PUSH_GAS = 3
_JUMPDEST_GAS = 1
_COPY_WORD_GAS = 3


@dataclass(frozen=True, slots=True)
class _Code:
    """Code, its most gas (None: unbounded, allowed only after the success path), and the
    memory it touches up to, in bytes (a bounded piece's expansion is charged from it)."""

    code: bytes
    gas: int | None
    memory_end: int = 0


@dataclass(frozen=True, slots=True)
class _Label:
    name: str


@dataclass(frozen=True, slots=True)
class _Jump:
    """PUSH2 of a label's offset."""

    to: str


@dataclass(frozen=True, slots=True)
class _Data:
    """PUSH2 of an appended blob's offset in the initcode."""

    index: int


@dataclass(frozen=True, slots=True)
class _Reserve:
    """PUSH3 of call `call`'s R: the success path's gas after its _GasRead."""

    call: int


@dataclass(frozen=True, slots=True)
class _GasRead:
    """The GAS that call `call`'s R is measured from."""

    call: int


@dataclass(frozen=True, slots=True)
class _End:
    """The end of the success path: what follows is reverts, never summed."""


type _Piece = _Code | _Label | _Jump | _Data | _Reserve | _GasRead | _End

# a label or data offset is pushed in 2 bytes: initcode is under 49,152 bytes (EIP-3860)
_OFFSET_PUSH_BYTES = 3
# a reserve is pushed in 3 bytes: under 2**24 gas, past any block's limit
_RESERVE_PUSH_BYTES = 4


class Assembly:
    """Bytecode with labels, data appended after the code, and the success path's gas: the
    most gas the code can take on its way to `end()`, each piece's own worst case summed,
    memory expansion included. What follows `end()` is reverts, never summed."""

    def __init__(self) -> None:
        self._pieces: list[_Piece] = []

    def _add(self, piece: _Piece) -> Assembly:
        self._pieces.append(piece)
        return self

    def op(self, *ops: Op) -> Assembly:
        for op in ops:
            self._add(_Code(bytes([op]), _STATIC_GAS.get(op)))
        return self

    def push(self, value: int) -> Assembly:
        if value == 0:
            return self._add(_Code(bytes([Op.PUSH0]), _PUSH0_GAS))
        width = (value.bit_length() + 7) // 8
        return self._add(_Code(bytes([Op.PUSH1 + width - 1]) + value.to_bytes(width), _PUSH_GAS))

    def push_address(self, address: ChecksumAddress) -> Assembly:
        return self._add(_Code(bytes([Op.PUSH20]) + bytes.fromhex(address[2:]), _PUSH_GAS))

    def push_data(self, index: int) -> Assembly:
        return self._add(_Data(index))

    def mstore_at(self, offset: int) -> Assembly:
        """MSTORE of the value on the stack at a fixed offset."""
        self.push(offset)
        return self._add(_Code(bytes([Op.MSTORE]), 3, offset + 32))

    def mload_at(self, offset: int) -> Assembly:
        self.push(offset)
        return self._add(_Code(bytes([Op.MLOAD]), 3, offset + 32))

    def codecopy(self, length: int) -> Assembly:
        """CODECOPY of `length` bytes to memory[0:length], its arguments pushed."""
        gas = 3 + _COPY_WORD_GAS * ceil(length / 32)
        return self._add(_Code(bytes([Op.CODECOPY]), gas, length))

    def balance(self, access: int) -> Assembly:
        return self._add(_Code(bytes([Op.BALANCE]), access))

    def staticcall(self, access: int, cap: int) -> Assembly:
        """STATICCALL given `cap` gas, its arguments pushed: its access, and what EIP-150's
        63/64 rule needs left for it to be given the whole cap."""
        return self._add(_Code(bytes([Op.STATICCALL]), access + ceil(cap * 64 / 63)))

    def value_call(self, charge: int) -> Assembly:
        """A CALL sending value with no gas but the stipend, its arguments pushed: `charge` is
        all it's charged (its access, the value, a new account's creation: the fork's)."""
        return self._add(_Code(bytes([Op.CALL]), charge))

    def client_call(self, access: int) -> Assembly:
        """A client's CALL, its arguments pushed: only its access counts, since what it's
        given is whatever's left above its R."""
        return self._add(_Code(bytes([Op.CALL]), access))

    def reserve(self, call: int) -> Assembly:
        return self._add(_Reserve(call))

    def gas_read(self, call: int) -> Assembly:
        return self._add(_GasRead(call))

    def jumpi(self, label: str) -> Assembly:
        self._add(_Jump(label))
        return self.op(Op.JUMPI)

    def label(self, name: str) -> Assembly:
        return self._add(_Label(name))

    def end(self) -> Assembly:
        return self._add(_End())

    def _success_path_gas(self) -> list[int]:
        """Each success-path piece's most gas, its memory expansion included."""
        costs: list[int] = []
        words = 0
        for piece in self._pieces:
            match piece:
                case _End():
                    return costs
                case _Code(gas=None, code=code):
                    raise AssemblyError(f"opcode {code.hex()} has no bound on the success path")
                case _Code(gas=int(gas), memory_end=end):
                    grown = max(words, ceil(end / 32))
                    costs.append(gas + memory_gas(grown) - memory_gas(words))
                    words = grown
                case _Label():
                    costs.append(_JUMPDEST_GAS)
                case _Jump() | _Data() | _Reserve():
                    costs.append(_PUSH_GAS)
                case _GasRead():
                    costs.append(_STATIC_GAS[Op.GAS])
        raise AssemblyError("the success path has no end")

    def assemble(self, blobs: Sequence[bytes]) -> tuple[bytes, int]:
        """The initcode, and the success path's most gas."""
        costs = self._success_path_gas()
        reserves = {
            piece.call: sum(costs[k + 1 :])
            for k, piece in enumerate(self._pieces[: len(costs)])
            if isinstance(piece, _GasRead)
        }
        labels: dict[str, int] = {}
        position = 0
        for piece in self._pieces:
            match piece:
                case _Code(code=code):
                    position += len(code)
                case _Label(name):
                    labels[name] = position
                    position += 1  # its JUMPDEST
                case _Jump() | _Data():
                    position += _OFFSET_PUSH_BYTES
                case _Reserve():
                    position += _RESERVE_PUSH_BYTES
                case _GasRead():
                    position += 1
                case _End():
                    pass
        offsets: list[int] = []
        for blob in blobs:
            offsets.append(position)
            position += len(blob)
        code = bytearray()
        for piece in self._pieces:
            match piece:
                case _Code(code=c):
                    code += c
                case _Label():
                    code.append(Op.JUMPDEST)
                case _Jump(to):
                    code += bytes([Op.PUSH2]) + labels[to].to_bytes(2)
                case _Data(index):
                    code += bytes([Op.PUSH2]) + offsets[index].to_bytes(2)
                case _Reserve(call):
                    code += bytes([Op.PUSH3]) + reserves[call].to_bytes(3)
                case _GasRead():
                    code.append(Op.GAS)
                case _End():
                    pass
        return bytes(code) + b"".join(blobs), sum(costs)
