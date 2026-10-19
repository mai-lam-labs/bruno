"""Gas schedules, per fork, for what the relay emits and sends; and which fork each network
runs when.

Only the prices on the relay's path are here; what both forks share (memory, copying,
keccak, initcode words, the stipend, warm access, SLOAD) is the Yellow Paper's, in the
modules that use it. Prague's figures hold for Osaka too: Fusaka changed none of them.
Amsterdam's are Glamsterdam's (EIP-2780, EIP-7976, EIP-8037, EIP-8038; execution-specs
19d4a72, geth v1.17.7), each measured exactly on Sepolia or an Amsterdam anvil on
2026-10-19 (scratchpad/gas_probe.py, scratchpad/tx_gas_receipts.py). EIP-8037 meters state
gas apart, but only above 2**24 gas a transaction: the relay's transactions are far under
it, so every charge here comes out of ordinary gas."""

from dataclasses import dataclass

from .common import BrunoError

# calldata's per byte, both forks (EIP-2028)
ZERO_BYTE_GAS = 4
NONZERO_BYTE_GAS = 16
# initcode's per word (EIP-3860), both forks
INITCODE_WORD_GAS = 2


class ForkError(BrunoError):
    pass


@dataclass(frozen=True, slots=True)
class GasSchedule:
    name: str
    # in-EVM: an account's first access, and CALL's charge for sending value (Amsterdam's
    # includes the stipend it passes on)
    cold_account_access: int
    call_value: int
    # sending value to an empty account creates it: from a CALL, and from a transaction
    # (Prague charges a transaction nothing for it)
    new_account: int
    tx_new_account: int
    # CREATE2, or a creation transaction, to an address with nothing at it
    create: int
    # a transaction's: base, its recipient's access (unless it's the sender), sending value
    # (likewise), and the floor's per zero and nonzero calldata byte
    tx_base: int
    tx_recipient: int
    tx_value: int
    floor_zero_byte: int
    floor_nonzero_byte: int
    # an EIP-7702 authorization's: intrinsic, then in execution for a new delegation (the
    # account write and the delegation's state), and for an authority that's empty
    authorization: int
    new_delegation: int
    empty_authority: int

    def intrinsic(
        self,
        calldata: bytes,
        *,
        value: bool = False,
        to_self: bool = False,
        creation: bool = False,
        authorizations: int = 0,
    ) -> tuple[int, int]:
        """A transaction's intrinsic gas, and the floor on what it's charged (EIP-7623,
        EIP-7976): its gas limit must cover both, the first with its execution added. For a
        creation, `calldata` is the initcode."""
        zeros = calldata.count(0)
        nonzeros = len(calldata) - zeros
        base = self.tx_base
        if not creation and not to_self:
            base += self.tx_recipient + (self.tx_value if value else 0)
        standard = base + ZERO_BYTE_GAS * zeros + NONZERO_BYTE_GAS * nonzeros
        standard += self.authorization * authorizations
        if creation:
            standard += self.create + INITCODE_WORD_GAS * ((len(calldata) + 31) // 32)
        floor = base + self.floor_zero_byte * zeros + self.floor_nonzero_byte * nonzeros
        return standard, floor

    def transfer(self, *, new_recipient: bool) -> int:
        """A plain transfer of value to another account: the gas it uses, exactly."""
        standard, floor = self.intrinsic(b"", value=True)
        return max(standard, floor) + (self.tx_new_account if new_recipient else 0)

    def authorizations(self, empty_authorities: int, authorizations: int) -> int:
        """What `authorizations` cost in execution, at most: each a new delegation, and
        `empty_authorities` of them for empty accounts."""
        return self.new_delegation * authorizations + self.empty_authority * empty_authorities


PRAGUE = GasSchedule(
    name="prague",
    cold_account_access=2_600,  # EIP-2929
    call_value=9_000,  # Yellow Paper
    new_account=25_000,  # Yellow Paper
    tx_new_account=0,
    create=32_000,  # Yellow Paper
    tx_base=21_000,
    tx_recipient=0,
    tx_value=0,
    floor_zero_byte=10,  # EIP-7623: 10 a token, a nonzero byte 4 tokens
    floor_nonzero_byte=40,
    authorization=25_000,  # EIP-7702's PER_EMPTY_ACCOUNT_COST, charged up front
    new_delegation=0,
    empty_authority=0,
)

AMSTERDAM = GasSchedule(
    name="amsterdam",
    cold_account_access=3_000,  # EIP-8038
    call_value=11_300,  # EIP-8038: ACCOUNT_WRITE 9,000 and the 2,300 stipend
    new_account=183_600,  # EIP-8037: 120 bytes at 1,530 a state byte
    tx_new_account=183_600,
    create=12_000 + 183_600,  # EIP-8038 CREATE_ACCESS, EIP-8037 the new account
    tx_base=12_000,  # EIP-2780
    tx_recipient=3_000,
    tx_value=6_000,
    floor_zero_byte=64,  # EIP-7976: 64 a byte, any byte
    floor_nonzero_byte=64,
    authorization=7_816,  # EIP-2780: 101 x 16 + 3,000 + 3,000 + 2 x 100
    new_delegation=9_000 + 35_190,  # EIP-8038 ACCOUNT_WRITE, EIP-8037 23 x 1,530
    empty_authority=183_600,  # EIP-8037's new account
)

SCHEDULES = {s.name: s for s in (PRAGUE, AMSTERDAM)}

# each network's forks that change the schedule: (from timestamp, schedule), in order, the
# times from github.com/eth-clients/{sepolia,hoodi} (Amsterdam), Ethereum's still Prague's
NETWORK_FORKS: dict[int, tuple[tuple[int, GasSchedule], ...]] = {
    1: ((0, PRAGUE),),
    11_155_111: ((0, PRAGUE), (1_791_294_816, AMSTERDAM)),
    560_048: ((0, PRAGUE), (1_793_036_568, AMSTERDAM)),
}


def schedule_at(forks: tuple[tuple[int, GasSchedule], ...], timestamp: int) -> GasSchedule:
    """The schedule of the last of `forks` active at `timestamp`."""
    active = [s for start, s in forks if start <= timestamp]
    if not active:
        raise ForkError(f"no fork active at {timestamp}")
    return active[-1]


def network_forks(chain_id: int) -> tuple[tuple[int, GasSchedule], ...]:
    if chain_id not in NETWORK_FORKS:
        raise ForkError(f"no fork schedule for chain {chain_id}")
    return NETWORK_FORKS[chain_id]
