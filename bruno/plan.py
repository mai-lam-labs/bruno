"""A submission's transaction: its runner and gas limit under a block's schedule, from what
the chain says of its call's target and its authorities, and its simulation."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from eth_keys.exceptions import BadSignature, ValidationError
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address

from .chain import Chain
from .common import BrunoError
from .deployer import DEPLOYER, transaction_gas
from .forks import GasSchedule
from .net.jsonrpc import JsonRpcError
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
    try:
        authorities = tuple(to_checksum_address(a.authority) for a in submission.authorizations)
    except (BadSignature, ValidationError) as e:
        raise InvalidSubmissionError("authorizationList", f"unrecoverable: {e}") from e
    delegated = set(authorities)
    if (await chain.code(submission.call.to)).startswith(_DELEGATION_PREFIX):
        delegated.add(submission.call.to)
    payment = Payment(tokens[submission.token], submission.payee, submission.amount)
    try:
        runner = in_call_runner(submission.call, payment, schedule, delegated)
    except RunnerError as e:
        raise InvalidSubmissionError("call", str(e)) from e
    gas_limit = transaction_gas(
        runner.initcode,
        runner.frame_gas(submission.gas),
        schedule,
        len(authorities),
        await empty(chain, authorities),
    )
    return Plan(runner, gas_limit)


async def empty(chain: Chain, authorities: Sequence[ChecksumAddress]) -> int:
    """How many of `authorities` are empty: no balance, no nonce, no code."""
    count = 0
    for authority in authorities:
        if (
            await chain.balance(authority) == 0
            and await chain.nonce(authority) == 0
            and await chain.code(authority) == "0x"
        ):
            count += 1
    return count


async def simulate(
    chain: Chain, submission: Submission, plan: Plan, sender: ChecksumAddress
) -> None:
    """The submission's transaction as eth_call from `sender`: raise its revert data if it
    reverts."""
    tx: dict[str, object] = {
        "from": sender,
        "to": DEPLOYER,
        "data": "0x" + plan.runner.initcode.hex(),
        "gas": hex(plan.gas_limit),
    }
    if submission.authorizations:
        tx["authorizationList"] = [authorization_json(a) for a in submission.authorizations]
    try:
        await chain.rpc("eth_call", [tx, "latest"])
    except JsonRpcError as e:
        data = e.data if isinstance(e.data, str) else "0x"
        raise SimulationFailedError(bytes.fromhex(data.removeprefix("0x"))) from e
