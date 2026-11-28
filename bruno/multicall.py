"""Multicall3's aggregate3: several calls made in order in one transaction, each required to
succeed. Where the concurrency account would send consecutive calls the relay bounds each in a
transaction of its own, it sends them as one aggregate3 (plan.py): one transaction's own gas
where there were several. Multicall3 treats every caller alike and keeps nothing, so it's a
target the account may call directly (targets.json).

OWN_GAS is what Multicall3 spends itself making n calls, beside what each call uses and its
target's access: measured exactly in the EVM (bruno/tests/test_multicall.py, a probe's GAS
before and after, on an anvil of each fork), for calls of a bounded call's calldata
(eip3009.CALLDATA_BYTES) that answer nothing. It isn't a line: Multicall3 keeps every call's
data and answer in memory, which costs a little more the more there is. The figures hold for
Multicall3's own code (CODE_HASH), which a chain is measured to have before it's served."""

from collections.abc import Sequence

from eth_abi.abi import encode
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address

from .abi import selector
from .common import BrunoError
from .eip3009 import CALLDATA_BYTES
from .evm import WARM_ACCOUNT_ACCESS, EncodedCall
from .forks import GasSchedule

MULTICALL3: ChecksumAddress = to_checksum_address("0xcA11bde05977b3631167028862bE2a173976CA11")
# keccak256 of its code, as it is at that address on Ethereum (mds1/multicall3)
CODE_HASH = bytes.fromhex("d5c15df687b16f2ff992fc8d767b4216323184a2bbc6ee2f9c398c318e770891")
_AGGREGATE3 = selector("aggregate3((address,bool,bytes)[])")
# Multicall3's own gas making n calls, by n
OWN_GAS = (893, 2_160, 3_386, 4_622, 5_861, 7_100, 8_339, 9_579, 10_818, 12_058)
# the most calls one aggregate3 is planned for
MOST_CALLS = len(OWN_GAS) - 1


class GroupError(BrunoError):
    """Calls that can't be one aggregate3 as the relay plans it: a bug of ours."""


def aggregate3(calls: Sequence[EncodedCall]) -> EncodedCall:
    """Multicall3's aggregate3 of `calls`, in order, none allowed to fail: one that does
    reverts them all. GroupError for a call sending ETH, which aggregate3 doesn't."""
    if any(call.value for call in calls):
        raise GroupError("a call sending ETH isn't grouped")
    entries = [(call.to, False, call.data) for call in calls]
    return EncodedCall(MULTICALL3, _AGGREGATE3 + encode(["(address,bool,bytes)[]"], [entries]))


def own_gas(calls: Sequence[EncodedCall], schedule: GasSchedule) -> int:
    """What an aggregate3 of `calls` takes beside what each call uses: Multicall3's own
    (OWN_GAS), and each call's target's access, cold the first time and warm after. GroupError
    for more calls than it's measured for, or one whose calldata isn't the length measured."""
    if len(calls) > MOST_CALLS or any(len(call.data) != CALLDATA_BYTES for call in calls):
        raise GroupError("an aggregate3 whose own gas isn't measured")
    targets = len({call.to for call in calls})
    access = schedule.cold_account_access * targets + WARM_ACCOUNT_ACCESS * (len(calls) - targets)
    return OWN_GAS[len(calls)] + access
