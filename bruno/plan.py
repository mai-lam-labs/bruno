"""A submission's transaction: its runner and gas limit under a block's schedule, from what
the chain says of its call's target and its authorities, and its simulation. Many
submissions' reads, and their simulations, are each one batch."""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace

from eth_typing import ChecksumAddress

from .chain import Chain
from .common import BrunoError
from .deployer import DEPLOYER, transaction_gas
from .eip3009 import transfer_gas
from .evm import has_code
from .forks import GasSchedule
from .net.jsonrpc import REVERTED, Answer, Call, JsonRpcError
from .runner import Erc20, Payment, Runner, RunnerError, Transfer, in_call_runner
from .submission import InvalidSubmissionError, Submission
from .submission import authorities as authorities_of
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
    most bytes its transaction takes unsigned (what's paid beyond gas is by size); `estimated`
    if its gas limit is its transaction's estimate (a step runs code the client chooses)."""

    runner: Runner
    gas_limit: int
    size: int
    estimated: bool = False


async def make_plan(
    chain: Chain,
    submission: Submission,
    schedule: GasSchedule,
    tokens: Mapping[ChecksumAddress, Erc20 | None],
    sender: ChecksumAddress,
    payee_holds: Collection[ChecksumAddress] = (),
) -> Plan:
    """The submission's runner and gas limit under `schedule`, paying in its token as
    `tokens` reads it, sent by `sender`; a transfer's gas bounded knowing which tokens the payee
    holds some of (`payee_holds`)."""
    (planned,) = await make_plans(chain, [submission], schedule, tokens, sender, payee_holds)
    if isinstance(planned, Exception):
        raise planned
    return planned


type Planned = Plan | InvalidSubmissionError | SimulationFailedError | JsonRpcError


async def make_plans(
    chain: Chain,
    submissions: Sequence[Submission],
    schedule: GasSchedule,
    tokens: Mapping[ChecksumAddress, Erc20 | None],
    sender: ChecksumAddress,
    payee_holds: Collection[ChecksumAddress] = (),
) -> list[Planned]:
    """Each submission's runner and gas limit under `schedule`, sent by `sender`, their reads
    in one batch (its call's target's code; each authority's balance, nonce and code; a payer's
    code): each its plan, or why not.

    A runner whose steps are all the relay's, a key's transfer among them (bounded from its
    token's measurement, knowing which tokens the payee holds some of), has its gas limit from
    the gas schedule. One with a step that runs code the client chooses (a deployment, or a
    contract payer's transfer, whose signature check is its own: a payer with code, or a
    signature that isn't a key's) has it from one eth_estimateGas of its transaction as it will
    be sent, in a second batch: an estimate that reverts is the submission's simulation failing.
    A wrapped signature from a payer with code is estimated first without its deployment, and
    only if that reverts with it, to prepare the payer (ERC-6492)."""
    authorities: list[tuple[ChecksumAddress, ...] | InvalidSubmissionError] = []
    for submission in submissions:
        try:
            authorities.append(authorities_of(submission.authorizations))
        except InvalidSubmissionError as e:
            authorities.append(e)
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
    plans: list[Planned] = []
    drafts: list[tuple[_Draft, Plan]] = []
    for submission, of, first in zip(submissions, authorities, firsts, strict=True):
        if isinstance(of, InvalidSubmissionError):
            plans.append(of)
            continue
        try:
            delegated = set(of)
            if answers[first].value().startswith(_DELEGATION_PREFIX):
                delegated.add(submission.call.to)
            empty = _count_empty(answers[first + 1 : first + 1 + 3 * len(of)])
            payer_code = None
            if submission.transfer is not None:
                payer_code = has_code(answers[first + 1 + 3 * len(of)].value())
            draft = _Draft(
                len(plans), submission, of, delegated, empty, payer_has_code=payer_code is True
            )
            plan = _planned(chain.chain_id, draft, schedule, tokens, payee_holds)
        except (InvalidSubmissionError, JsonRpcError) as e:
            plans.append(e)
            continue
        plans.append(plan)
        if plan.estimated:
            drafts.append((draft, plan))
    await _estimate(chain, drafts, plans, schedule, tokens, sender, payee_holds)
    return plans


@dataclass(frozen=True, slots=True)
class _Draft:
    """A submission as read, its plan at `index` of make_plans'; `prepare` to call its wrapped
    signature's factory however its payer's code reads."""

    index: int
    submission: Submission
    authorities: tuple[ChecksumAddress, ...]
    delegated: set[ChecksumAddress]
    empty: int
    payer_has_code: bool
    prepare: bool = False


def _planned(
    chain_id: int,
    draft: _Draft,
    schedule: GasSchedule,
    tokens: Mapping[ChecksumAddress, Erc20 | None],
    payee_holds: Collection[ChecksumAddress],
) -> Plan:
    """The draft's runner, and its gas limit from the gas schedule: for one with a step that
    runs code the client chooses, only a placeholder until it's estimated."""
    submission = draft.submission
    if submission.token not in tokens:
        raise InvalidSubmissionError("payment", f"{submission.token} is suspended")
    token = tokens[submission.token]
    deployment = submission.to_deploy
    transfer = None
    contract_payer = False
    if submission.transfer is not None:
        if token is None or token.transfer_gas is None:
            raise InvalidSubmissionError(
                "payment", f"{submission.token} takes no EIP-3009 transfer"
            )
        signed = submission.transfer
        contract_payer = not signed.ecdsa or draft.payer_has_code or signed.deployment is not None
        gas = None
        if not contract_payer:
            gas = transfer_gas(token.transfer_gas, schedule, submission.token in payee_holds)
        transfer = Transfer(signed.calldata(), gas)
        # a wrapped signature's payer that has code: deployed, so prepared only if asked to
        if signed.deployment is not None and draft.payer_has_code and not draft.prepare:
            deployment = submission.deployment
    payment = Payment(token, submission.payee, submission.amount, transfer)
    try:
        runner = in_call_runner(
            submission.call,
            payment,
            schedule,
            draft.delegated,
            client_gas=submission.gas,
            deployment=deployment,
            prepare=draft.prepare,
        )
    except RunnerError as e:
        raise InvalidSubmissionError("call", str(e)) from e
    return runner_plan(
        chain_id,
        runner,
        submission.gas,
        schedule,
        len(draft.authorities),
        draft.empty,
        estimated=deployment is not None or contract_payer,
    )


def runner_plan(
    chain_id: int,
    runner: Runner,
    client_gas: int,
    schedule: GasSchedule,
    authorizations: int,
    empty: int,
    *,
    extra_gas: int = 0,
    estimated: bool = False,
) -> Plan:
    """`runner`'s plan: its transaction's gas limit from the gas schedule, its call given
    `client_gas`, with `authorizations` of which `empty` are from empty accounts, and
    `extra_gas` more in its frame; and its transaction's most bytes unsigned."""
    gas_limit = transaction_gas(
        runner.initcode,
        runner.frame_gas(client_gas) + extra_gas,
        schedule,
        authorizations,
        empty,
    )
    size = unsigned_size(chain_id, DEPLOYER, runner.initcode, authorizations=authorizations)
    return Plan(runner, gas_limit, size, estimated=estimated)


async def _estimate(
    chain: Chain,
    drafts: Sequence[tuple[_Draft, Plan]],
    plans: list[Planned],
    schedule: GasSchedule,
    tokens: Mapping[ChecksumAddress, Erc20 | None],
    sender: ChecksumAddress,
    payee_holds: Collection[ChecksumAddress],
) -> None:
    """Each draft's plan, put in `plans` at its index, given its gas limit by one
    eth_estimateGas of its transaction, in one batch: one that reverts is its simulation
    failing, unless a wrapped signature's payer may need preparing, which is then estimated
    again, prepared."""
    if not drafts:
        return
    at = hex(chain.seen) if chain.seen else "latest"
    answers = await chain.batch(
        [
            Call(
                "eth_estimateGas",
                [_transaction(draft.submission, plan.runner, sender), at],
            )
            for draft, plan in drafts
        ]
    )
    again: list[tuple[_Draft, Plan]] = []
    for (draft, plan), answer in zip(drafts, answers, strict=True):
        if answer.error is None:
            plans[draft.index] = replace(plan, gas_limit=int(answer.value(), 16))
            continue
        if answer.error.code != REVERTED:
            plans[draft.index] = answer.error
            continue
        signed = draft.submission.transfer
        if (
            not draft.prepare
            and signed is not None
            and signed.deployment is not None
            and draft.payer_has_code
        ):
            prepared = replace(draft, prepare=True)
            again.append(
                (prepared, _planned(chain.chain_id, prepared, schedule, tokens, payee_holds))
            )
        else:
            data = answer.error.data if isinstance(answer.error.data, str) else "0x"
            plans[draft.index] = SimulationFailedError(bytes.fromhex(data.removeprefix("0x")))
    await _estimate(chain, again, plans, schedule, tokens, sender, payee_holds)


def _transaction(
    submission: Submission, runner: Runner, sender: ChecksumAddress, gas_limit: int | None = None
) -> dict[str, object]:
    """The submission's transaction with `runner`, from `sender`, as eth_call and
    eth_estimateGas take it: no fees, so no balance is needed; at `gas_limit`, unless it's
    being estimated (a limit would cap the estimate's search)."""
    tx: dict[str, object] = {"from": sender, "to": DEPLOYER, "data": "0x" + runner.initcode.hex()}
    if gas_limit is not None:
        tx["gas"] = hex(gas_limit)
    if submission.authorizations:
        tx["authorizationList"] = [authorization_json(a) for a in submission.authorizations]
    return tx


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
        and not has_code(answers[i + 2].value())
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
    calls = [
        Call(
            "eth_call",
            [_transaction(sim.submission, sim.plan.runner, sim.sender, sim.plan.gas_limit), at],
        )
        for sim in sims
    ]
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
