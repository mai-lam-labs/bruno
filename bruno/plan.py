"""A submission's transaction: its runner and gas limit under a block's schedule, from what
the chain says of its call's target and its authorities, and its simulation. Many
submissions' reads, and their simulations, are each one batch."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from eth_keys.exceptions import BadSignature, ValidationError
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address

from .chain import Chain
from .common import BrunoError
from .deployer import DEPLOYER, transaction_gas
from .forks import GasSchedule
from .net.jsonrpc import Answer, Call, JsonRpcError
from .runner import Erc20, Payment, Runner, RunnerError, in_call_runner
from .submission import InvalidSubmissionError, Submission
from .transactions import authorization_json

_DELEGATION_PREFIX = "0xef0100"


class SimulationFailedError(BrunoError):
    """The submission's transaction reverts: with `revert_data`."""

    def __init__(self, revert_data: bytes) -> None:
        super().__init__("simulation failed")
        self.revert_data = revert_data


@dataclass(frozen=True, slots=True)
class Plan:
    """A submission's runner and its transaction's gas limit, under a block's schedule."""

    runner: Runner
    gas_limit: int


async def make_plan(
    chain: Chain,
    submission: Submission,
    schedule: GasSchedule,
    tokens: Mapping[ChecksumAddress, Erc20 | None],
) -> Plan:
    """The submission's runner and gas limit under `schedule`, paying in its token as
    `tokens` reads it."""
    (planned,) = await make_plans(chain, [submission], schedule, tokens)
    if isinstance(planned, Exception):
        raise planned
    return planned


async def make_plans(
    chain: Chain,
    submissions: Sequence[Submission],
    schedule: GasSchedule,
    tokens: Mapping[ChecksumAddress, Erc20 | None],
) -> list[Plan | InvalidSubmissionError | JsonRpcError]:
    """Each submission's runner and gas limit under `schedule`, their reads in one batch (its
    call's target's code; each authority's balance, nonce and code): each its plan, or why
    not."""
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
    answers = await chain.batch(calls)
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
            plans.append(_planned(submission, schedule, tokens, of, delegated, empty))
        except (InvalidSubmissionError, JsonRpcError) as e:
            plans.append(e)
    return plans


def _planned(
    submission: Submission,
    schedule: GasSchedule,
    tokens: Mapping[ChecksumAddress, Erc20 | None],
    authorities: tuple[ChecksumAddress, ...],
    delegated: set[ChecksumAddress],
    empty: int,
) -> Plan:
    payment = Payment(tokens[submission.token], submission.payee, submission.amount)
    try:
        runner = in_call_runner(submission.call, payment, schedule, delegated)
    except RunnerError as e:
        raise InvalidSubmissionError("call", str(e)) from e
    gas_limit = transaction_gas(
        runner.initcode, runner.frame_gas(submission.gas), schedule, len(authorities), empty
    )
    return Plan(runner, gas_limit)


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
    reverts."""
    (revert,) = await simulations(chain, [Simulation(submission, plan, sender)])
    if revert is not None:
        raise SimulationFailedError(revert)


async def simulations(chain: Chain, sims: Sequence[Simulation]) -> list[bytes | None]:
    """Each submission's transaction as eth_call from its sender, in one batch: each its revert
    data, or None if it doesn't revert."""
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
        calls.append(Call("eth_call", [tx, "latest"]))
    reverts: list[bytes | None] = []
    for answer in await chain.batch(calls):
        if answer.error is None:
            reverts.append(None)
        else:
            data = answer.error.data if isinstance(answer.error.data, str) else "0x"
            reverts.append(bytes.fromhex(data.removeprefix("0x")))
    return reverts
