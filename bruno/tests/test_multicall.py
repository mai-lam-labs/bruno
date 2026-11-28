"""Calls grouped through Multicall3 (bruno/multicall.py): its own gas measured in the EVM, to
the gas, at this fork; and a group of a key's transfers sent on anvil at exactly the gas planned
for it, which is less than the same transfers take a transaction each."""

from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from bruno.accountreads import CallAccounts
from bruno.eip3009 import CALLDATA_BYTES, transfer_gas
from bruno.evm import EncodedCall
from bruno.forks import GasSchedule
from bruno.multicall import CODE_HASH, MOST_CALLS, MULTICALL3, aggregate3, own_gas
from bruno.plan import PaidByTransfer, Plan, drafted
from bruno.runner import PlannedCall, call_need
from bruno.submission import Submission, SubmittedCall
from bruno.tests.calibur import fresh_client
from bruno.tests.deployments import SENDER_ACCOUNT, measured_exactly
from bruno.tests.localchain import USDC, LocalChain
from bruno.tests.transfers import by_key, usdc_transfer

# anvil's dev account #8: the payee
PAYEE_ACCOUNT = 8
# installed by eth_call's state override: copies its calldata to memory, reads GAS, calls
# Multicall3 with it, reads GAS again, and returns the call's success and the difference
_PROBE = to_checksum_address("0x" + "be" * 20)
_PROBE_CODE = (
    "0x365f5f37"  # CALLDATACOPY(0, 0, CALLDATASIZE)
    + "5a"  # GAS: g1
    + "5f5f365f5f73"
    + MULTICALL3.removeprefix("0x").lower()
    + "5af1"  # CALL(GAS, Multicall3, 0, ...)
    + "5a905f52"  # GAS: g2; success to memory[0]
    + "9003602052"  # g1 - g2 to memory[32]
    + "60405ff3"  # RETURN 64 bytes
)
# what's between the probe's two GAS readings beside the call: five pushes of 2 gas, a PUSH20
# of 3, the GAS the call is given, and the second reading's own
_PROBE_OWN_GAS = 5 * 2 + 3 + 2 + 2
_PROBE_GAS = 10_000_000
# an account with no code: a call to it uses no gas of its own, and answers nothing
_NOBODY = to_checksum_address("0x" + "d0" * 20)


async def test_multicall3s_own_gas_is_as_measured(chain: LocalChain, schedule: GasSchedule) -> None:
    """Multicall3's aggregate3 of nothing to nine calls, each of a bounded call's calldata
    length, to an account with no code, so nothing is spent below it: what it takes, less its
    own cold access from the probe, is the schedule's figure for that many calls. And its code
    is the code the figures are for."""
    code = bytes.fromhex((await chain.code(MULTICALL3))[2:])
    assert keccak(code) == CODE_HASH

    for count in range(MOST_CALLS + 1):
        calls = [EncodedCall(_NOBODY, b"\x11" * CALLDATA_BYTES)] * count
        data = "0x" + aggregate3(calls).data.hex()
        tx = {"to": _PROBE, "data": data, "gas": hex(_PROBE_GAS)}
        overrides = {_PROBE: {"code": _PROBE_CODE}}
        answer = bytes.fromhex((await chain.rpc("eth_call", [tx, "latest", overrides]))[2:])
        assert answer[:32] == (1).to_bytes(32), "aggregate3 failed"
        taken = int.from_bytes(answer[32:64]) - _PROBE_OWN_GAS - schedule.cold_account_access

        assert taken == own_gas(calls, schedule), count


async def test_grouped_transfers_land_in_the_gas_planned_for_less_than_apart(
    chain: LocalChain, schedule: GasSchedule
) -> None:
    """Two transfers a key signed, as a submission's calls, and a third that pays for them,
    planned from a transfer measured to the gas: they're one transaction, Multicall3's, which
    lands at its gas limit, every transfer made. The same three planned without the relay's
    bound of the calls are a transaction each, and land at theirs. The one transaction's limit
    is under the three's together, and so is the gas it used."""
    payer = fresh_client("pays for two transfers, grouped")
    friends = [fresh_client(f"is sent a grouped transfer {i}").address for i in range(2)]
    payee = await chain.account(PAYEE_ACCOUNT)
    sender = await chain.account(SENDER_ACCOUNT)
    chain_id = await chain.chain_id()
    amount = 1_000_000
    # everyone holds some before and after: each balance is written, none set or cleared
    await chain.mint(USDC, payer.address, 10 * amount)
    for holder in (*friends, payee):
        await chain.mint(USDC, holder, 1)
    usdc = await measured_exactly(chain, payee, schedule)
    assert usdc.transfer_gas is not None
    bound = call_need(transfer_gas(usdc.transfer_gas, schedule, payee_holds=True))

    async def planned(*, bounded: bool) -> Plan:
        """The two transfers and their payment, signed afresh: grouped if `bounded`."""
        gifts = [
            await usdc_transfer(chain_id, payer.address, friend, amount, by_key(payer))
            for friend in friends
        ]
        paying = await usdc_transfer(chain_id, payer.address, payee, amount, by_key(payer))
        calls = [EncodedCall(USDC.address, gift.calldata()) for gift in gifts]
        submission = Submission(
            tuple(SubmittedCall(call, bound) for call in calls),
            (),
            USDC.address,
            payee,
            amount,
            0,
            paying,
        )
        proof = PaidByTransfer()
        draft = drafted(
            submission,
            proof,
            (),
            CallAccounts(frozenset(), 0, frozenset()),
            usdc,
            schedule,
            tuple(PlannedCall(call, bound, bounded) for call in calls),
            payer_has_code=False,
            payee_holds=True,
        )
        return proof.plan(chain_id, draft, schedule)

    async def landed(plan: Plan) -> int:
        """`plan`'s transactions sent, each at its gas limit: the gas they used together."""
        used = 0
        for step in plan.own:
            receipt = await chain.send_call(sender, step.transaction, "a step", step.gas_limit)
            used += int(receipt["gasUsed"], 16)
        return used

    held = [await chain.balance_of(USDC, holder) for holder in (*friends, payee)]
    grouped, apart = await planned(bounded=True), await planned(bounded=False)

    used_grouped = await landed(grouped)
    used_apart = await landed(apart)

    assert [step.transaction.to for step in grouped.own] == [MULTICALL3]
    assert [step.transaction.to for step in apart.own] == [USDC.address] * 3
    assert grouped.gas_limit < apart.gas_limit
    assert used_grouped < used_apart
    # every transfer was made, twice over
    now = [await chain.balance_of(USDC, holder) for holder in (*friends, payee)]
    assert [after - before for before, after in zip(held, now, strict=True)] == [2 * amount] * 3
