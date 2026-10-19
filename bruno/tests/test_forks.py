"""The gas schedule against the local chain's fork, exactly: each transaction's gasUsed is
what the schedule says it is. make check runs these on Prague and on Amsterdam."""

from collections.abc import Sequence

from eth_account import Account
from eth_account.datastructures import SignedSetCodeAuthorization
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from bruno.accounts import derive
from bruno.bundle import TopUp, sign_funding
from bruno.deployer import DEPLOYER, overhead_gas
from bruno.forks import GasSchedule
from bruno.tests.localchain import CALIBUR, TEST_FLOAT, LocalChain
from bruno.transactions import Fees, SignedTransaction, sign

FLOAT_ETH = 10**18
# the gas the measured transactions are sent with: ample, so each uses what it needs
AMPLE_GAS = 1_000_000
# what a value CALL's callee is given, and an EOA hands back unused (Yellow Paper)
STIPEND = 2_300


async def fees(chain: LocalChain) -> Fees:
    base = await chain.base_fee()
    return Fees(max_fee_per_gas=2 * base + 10**9, max_priority_fee_per_gas=10**9)


async def gas_used(chain: LocalChain, tx: SignedTransaction) -> int:
    receipt = await chain.submit(tx.raw)
    assert receipt["status"] == "0x1"
    return int(receipt["gasUsed"], 16)


async def from_float(
    chain: LocalChain,
    to: ChecksumAddress | None,
    data: bytes = b"",
    value: int = 0,
    authorizations: Sequence[SignedSetCodeAuthorization] = (),
) -> SignedTransaction:
    """A transaction from the funded test float, with ample gas."""
    await chain.set_eth_balance(TEST_FLOAT.address, FLOAT_ETH)
    nonce = await chain.nonce(TEST_FLOAT.address)
    return sign(
        TEST_FLOAT,
        await chain.chain_id(),
        nonce,
        await fees(chain),
        AMPLE_GAS,
        to,
        data,
        value,
        authorizations,
    )


def fresh(name: str) -> ChecksumAddress:
    return to_checksum_address(keccak(text=name)[-20:])


async def test_transfers(chain: LocalChain, schedule: GasSchedule) -> None:
    existing = await chain.account(9)
    assert await gas_used(chain, await from_float(chain, existing, value=1)) == (
        schedule.transfer(new_recipient=False)
    )
    assert await gas_used(chain, await from_float(chain, fresh("a fresh recipient"), value=1)) == (
        schedule.transfer(new_recipient=True)
    )


async def test_calldata_and_its_floor(chain: LocalChain, schedule: GasSchedule) -> None:
    """Calldata to an account with no code: no execution, so the larger of the intrinsic gas
    and the floor (the floor, for 100 bytes)."""
    existing = await chain.account(9)
    for data in (bytes(100), b"\x11" * 100):
        assert await gas_used(chain, await from_float(chain, existing, data)) == max(
            schedule.intrinsic(data)
        )


async def test_creation(chain: LocalChain, schedule: GasSchedule) -> None:
    assert await gas_used(chain, await from_float(chain, None)) == max(
        schedule.intrinsic(b"", creation=True)
    )


async def test_authorization_of_an_empty_account(chain: LocalChain, schedule: GasSchedule) -> None:
    """An authorization from an account that's empty (no ETH, no nonce: a client holding only
    tokens), to a delegate, in a transaction to an existing account."""
    authority = Account.from_key(keccak(text="an empty authority"))
    auth = Account.sign_authorization(
        {"chainId": await chain.chain_id(), "address": CALIBUR, "nonce": 0}, authority.key
    )
    tx = await from_float(chain, await chain.account(9), authorizations=[auth])
    standard, floor = schedule.intrinsic(b"", authorizations=1)
    assert await gas_used(chain, tx) == max(standard, floor) + schedule.authorizations(1, 1)


async def test_funding_uses_its_gas_less_the_stipends(
    chain: LocalChain, schedule: GasSchedule
) -> None:
    """The funding transaction's gas limit is exactly what it uses, but for the stipend each
    top-up's account hands back: one top-up to a new account, one to an existing one."""
    await chain.set_eth_balance(TEST_FLOAT.address, FLOAT_ETH)
    existing = derive(TEST_FLOAT, 300)
    await chain.set_eth_balance(existing.address, 1)
    top_ups = [
        TopUp(derive(TEST_FLOAT, 301).address, 10**15, new_account=True),
        TopUp(existing.address, 10**15, new_account=False),
    ]
    nonce = await chain.nonce(TEST_FLOAT.address)
    funding = sign_funding(
        TEST_FLOAT, await chain.chain_id(), nonce, await fees(chain), top_ups, schedule
    )

    assert await gas_used(chain, funding) == funding.gas - STIPEND * len(top_ups)


async def test_deployer_overhead(chain: LocalChain, schedule: GasSchedule) -> None:
    """Empty initcode through the deployer: its own gas, exactly (the runner runs nothing)."""
    tx = await from_float(chain, DEPLOYER)
    assert await gas_used(chain, tx) == max(schedule.intrinsic(b"")) + overhead_gas(0, schedule)
