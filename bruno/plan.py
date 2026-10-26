"""A submission's transaction: its runner and gas limit under a block's schedule, from what
the chain says of its call's target and its authorities, and its simulation. Many
submissions' reads, and their simulations, are each one batch."""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from eth_keys.exceptions import BadSignature, ValidationError
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address

from .chain import Chain
from .common import BrunoError
from .deployer import DEPLOYER, transaction_gas
from .eip3009 import measured, transfer_gas
from .forks import GasSchedule
from .net.jsonrpc import Answer, Call, JsonRpcError
from .runner import Erc20, Payment, Runner, RunnerError, Transfer, in_call_runner
from .submission import InvalidSubmissionError, Submission
from .transactions import authorization_json, unsigned_size

_DELEGATION_PREFIX = "0xef0100"
# how a node says it hasn't a block it's asked at: EIP-1474's code, and geth's message
_RESOURCE_NOT_FOUND = -32001
_HEADER_NOT_FOUND = "header not found"


class SimulationFailedError(BrunoError):
    """The submission's transaction reverts: with `revert_data`."""

    def __init__(self, revert_data: bytes) -> None:
        super().__init__("simulation failed")
        self.revert_data = revert_data


class BlockUnseenError(BrunoError):
    """The node simulating a submission hasn't the block it was asked at yet: nothing is known
    of whether it reverts."""

    def __init__(self, block: int) -> None:
        super().__init__(f"the nodes aren't at block {block} yet")
        self.block = block


@dataclass(frozen=True, slots=True)
class Plan:
    """A submission's runner, its transaction's gas limit under a block's schedule, and the
    most bytes its transaction takes unsigned (what's paid beyond gas is by size)."""

    runner: Runner
    gas_limit: int
    size: int


async def make_plan(
    chain: Chain,
    submission: Submission,
    schedule: GasSchedule,
    tokens: Mapping[ChecksumAddress, Erc20 | None],
    payee_holds: Collection[ChecksumAddress] = (),
) -> Plan:
    """The submission's runner and gas limit under `schedule`, paying in its token as
    `tokens` reads it; a transfer's gas bounded knowing which tokens the payee holds some of
    (`payee_holds`)."""
    (planned,) = await make_plans(chain, [submission], schedule, tokens, payee_holds)
    if isinstance(planned, Exception):
        raise planned
    return planned


async def make_plans(
    chain: Chain,
    submissions: Sequence[Submission],
    schedule: GasSchedule,
    tokens: Mapping[ChecksumAddress, Erc20 | None],
    payee_holds: Collection[ChecksumAddress] = (),
) -> list[Plan | InvalidSubmissionError | JsonRpcError]:
    """Each submission's runner and gas limit under `schedule`, their reads in one batch (its
    call's target's code; each authority's balance, nonce and code; a payer's code): each its
    plan, or why not. A key's transfer's gas is bounded from its token's measurement, knowing
    which tokens the payee holds some of; a contract payer's (code, or a signature that isn't a
    key's) is its own transfer's, estimated in a second batch: its signature check is the
    contract's own, of any cost. An estimate that fails leaves the key's bound, and the
    simulation the refusal."""
    authorities: list[tuple[ChecksumAddress, ...] | InvalidSubmissionError] = []
    for submission in submissions:
        try:
            authorities.append(
                tuple(to_checksum_address(a.authority) for a in submission.authorizations)
            )
        except (BadSignature, ValidationError) as e:
            authorities.append(InvalidSubmissionError("authorizationList", f"unrecoverable: {e}"))
    calls: list[Call] = []
    firsts: list[int] = []
    for submission, of in zip(submissions, authorities, strict=True):
        firsts.append(len(calls))
        if not isinstance(of, InvalidSubmissionError):
            calls.append(Call("eth_getCode", [submission.call.to, "latest"]))
            calls += [c for authority in of for c in _authority_reads(authority)]
            if submission.transfer is not None:
                calls.append(Call("eth_getCode", [submission.transfer.payer, "latest"]))
    answers = await chain.batch(calls)
    measured_gas = await _contract_payers(
        chain, submissions, authorities, firsts, answers, schedule
    )
    plans: list[Plan | InvalidSubmissionError | JsonRpcError] = []
    for submission, of, first in zip(submissions, authorities, firsts, strict=True):
        if isinstance(of, InvalidSubmissionError):
            plans.append(of)
            continue
        try:
            delegated = set(of)
            if answers[first].value().startswith(_DELEGATION_PREFIX):
                delegated.add(submission.call.to)
            empty = _count_empty(answers[first + 1 : first + 1 + 3 * len(of)])
            plans.append(
                _planned(
                    chain.chain_id,
                    submission,
                    schedule,
                    tokens,
                    payee_holds,
                    of,
                    delegated,
                    empty,
                    measured_gas.get(id(submission)),
                )
            )
        except (InvalidSubmissionError, JsonRpcError) as e:
            plans.append(e)
    return plans


def _planned(
    chain_id: int,
    submission: Submission,
    schedule: GasSchedule,
    tokens: Mapping[ChecksumAddress, Erc20 | None],
    payee_holds: Collection[ChecksumAddress],
    authorities: tuple[ChecksumAddress, ...],
    delegated: set[ChecksumAddress],
    empty: int,
    contract_transfer_gas: int | None = None,
) -> Plan:
    if submission.token not in tokens:
        raise InvalidSubmissionError("payment", f"{submission.token} is suspended")
    token = tokens[submission.token]
    transfer = None
    if submission.transfer is not None:
        if token is None or token.transfer_gas is None:
            raise InvalidSubmissionError(
                "payment", f"{submission.token} takes no EIP-3009 transfer"
            )
        gas = contract_transfer_gas
        if gas is None:
            gas = transfer_gas(token.transfer_gas, schedule, submission.token in payee_holds)
        transfer = Transfer(submission.transfer.calldata(), gas)
    payment = Payment(token, submission.payee, submission.amount, transfer)
    try:
        runner = in_call_runner(submission.call, payment, schedule, delegated)
    except RunnerError as e:
        raise InvalidSubmissionError("call", str(e)) from e
    gas_limit = transaction_gas(
        runner.initcode, runner.frame_gas(submission.gas), schedule, len(authorities), empty
    )
    size = unsigned_size(chain_id, DEPLOYER, runner.initcode, authorizations=len(authorities))
    return Plan(runner, gas_limit, size)


async def _contract_payers(
    chain: Chain,
    submissions: Sequence[Submission],
    authorities: Sequence[tuple[ChecksumAddress, ...] | InvalidSubmissionError],
    firsts: Sequence[int],
    answers: Sequence[Answer],
    schedule: GasSchedule,
) -> dict[int, int]:
    """Each contract payer's transfer's gas, by its submission's id(): estimated, as it will run
    (its value, the state now), less its intrinsic gas (eip3009.measured). A payer is a
    contract if it has code, or signed with something other than a key's 65 bytes."""
    estimating: list[tuple[Submission, bytes]] = []
    for submission, of, first in zip(submissions, authorities, firsts, strict=True):
        transfer = submission.transfer
        if transfer is None or isinstance(of, InvalidSubmissionError):
            continue
        try:
            code = answers[first + 1 + 3 * len(of)].value()
        except JsonRpcError:
            continue  # unread: the key's bound, and the simulation decides
        if not transfer.ecdsa or code not in ("0x", "0x0"):
            estimating.append((submission, transfer.calldata()))
    if not estimating:
        return {}
    estimates = await chain.batch(
        [
            Call("eth_estimateGas", [{"to": s.token, "data": "0x" + data.hex()}])
            for s, data in estimating
        ]
    )
    gas: dict[int, int] = {}
    for (submission, data), estimate in zip(estimating, estimates, strict=True):
        if estimate.error is None:
            gas[id(submission)] = measured(int(estimate.value(), 16), data, schedule)
    return gas


async def empty(chain: Chain, authorities: Sequence[ChecksumAddress]) -> int:
    """How many of `authorities` are empty (no balance, no nonce, no code), read in one
    batch."""
    return _count_empty(
        await chain.batch([c for authority in authorities for c in _authority_reads(authority)])
    )


def _authority_reads(authority: ChecksumAddress) -> list[Call]:
    """An authority's reads, to tell whether it's empty: its balance, nonce and code."""
    return [
        Call("eth_getBalance", [authority, "latest"]),
        Call("eth_getTransactionCount", [authority, "latest"]),
        Call("eth_getCode", [authority, "latest"]),
    ]


def _count_empty(answers: Sequence[Answer]) -> int:
    """How many of the authorities whose reads `answers` are, three each, are empty."""
    return sum(
        1
        for i in range(0, len(answers), 3)
        if int(answers[i].value(), 16) == 0
        and int(answers[i + 1].value(), 16) == 0
        and answers[i + 2].value() == "0x"
    )


@dataclass(frozen=True, slots=True)
class Simulation:
    """A submission's transaction, as planned, from `sender`."""

    submission: Submission
    plan: Plan
    sender: ChecksumAddress


async def simulate(
    chain: Chain, submission: Submission, plan: Plan, sender: ChecksumAddress
) -> None:
    """The submission's transaction as eth_call from `sender`: raise its revert data if it
    reverts. A node without the block is passed over for another, as many times as there are
    nodes: BlockUnseenError if none had it."""
    revert: bytes | BlockUnseenError | None = BlockUnseenError(chain.seen)
    for _ in chain.nodes():
        (revert,) = await simulations(chain, [Simulation(submission, plan, sender)])
        if not isinstance(revert, BlockUnseenError):
            break
    if isinstance(revert, BlockUnseenError):
        raise revert
    if revert is not None:
        raise SimulationFailedError(revert)


async def simulations(
    chain: Chain, sims: Sequence[Simulation]
) -> list[bytes | BlockUnseenError | None]:
    """Each submission's transaction as eth_call from its sender, in one batch, at the highest
    block the chain has seen (a node behind it answers that it hasn't the block, never an older
    state): each its revert data, None if it deploys its runner (the deployer answering its
    address, 32 bytes), or BlockUnseenError if the node hadn't the block."""
    at = hex(chain.seen) if chain.seen else "latest"
    calls: list[Call] = []
    for sim in sims:
        tx: dict[str, object] = {
            "from": sim.sender,
            "to": DEPLOYER,
            "data": "0x" + sim.plan.runner.initcode.hex(),
            "gas": hex(sim.plan.gas_limit),
        }
        if sim.submission.authorizations:
            tx["authorizationList"] = [authorization_json(a) for a in sim.submission.authorizations]
        calls.append(Call("eth_call", [tx, at]))
    reverts: list[bytes | BlockUnseenError | None] = []
    for answer in await chain.batch(calls):
        if _unseen(answer):
            reverts.append(BlockUnseenError(chain.seen))
        elif answer.error is None:
            # the deployer answers the runner's address: anything else (an address without code
            # answers nothing) ran no runner, so checked no payment
            deployed = isinstance(answer.result, str) and len(answer.result) == 2 + 64
            reverts.append(None if deployed else b"")
        else:
            data = answer.error.data if isinstance(answer.error.data, str) else "0x"
            reverts.append(bytes.fromhex(data.removeprefix("0x")))
    return reverts


def _unseen(answer: Answer) -> bool:
    """Whether a node answered an eth_call that it hasn't the block asked at: EIP-1474's
    resource not found (-32001), geth's "header not found", or no result at all (Sepolia's
    nodes, 2026-10-26: publicnode and Tenderly the first, ethpandaops the last). A revert is
    another error (code 3)."""
    if answer.error is None:
        return answer.result is None
    return answer.error.code == _RESOURCE_NOT_FOUND or answer.error.message == _HEADER_NOT_FOUND
