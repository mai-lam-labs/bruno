"""A submission's transaction: its runner and gas limit under a block's schedule, from what
the chain says of its call's target and its authorities, and its simulation. Many
submissions' reads, and their simulations, are each one batch."""

import logging
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace

from eth_account.datastructures import SignedSetCodeAuthorization
from eth_typing import ChecksumAddress

from .chain import Chain, ChainError, code_of, has_code, quantity, revert_data, word_bytes
from .common import BrunoError
from .deployer import DEPLOYER, runner_gas_within, transaction_gas
from .eip3009 import transfer_gas
from .erc6492 import Deployment
from .evm import EncodedCall
from .forks import GasSchedule
from .jsonrpc import REVERTED, Answer, Call, JsonRpcError
from .runner import (
    Erc20,
    Payment,
    Runner,
    RunnerError,
    Transfer,
    client_gas_within,
    estimating_runner,
    in_call_runner,
)
from .submission import InvalidSubmissionError, Submission
from .submission import authorities as authorities_of
from .transactions import authorization_json, unsigned_size

logger = logging.getLogger(__name__)

_DELEGATION_PREFIX = bytes.fromhex("ef0100")
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


# a submission's plan, or why it has none: the client's mistake (InvalidSubmissionError), its
# simulation failing, a node's failure (JsonRpcError, ChainError), or a bug of ours, logged
type Planned = Plan | Exception


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
            if code_of(answers[first].value()).startswith(_DELEGATION_PREFIX):
                delegated.add(submission.call.to)
            empty = _count_empty(answers[first + 1 : first + 1 + 3 * len(of)])
            payer_code = None
            if submission.transfer is not None:
                payer_code = has_code(answers[first + 1 + 3 * len(of)].value())
            draft = _Draft(
                len(plans), submission, of, delegated, empty, payer_has_code=payer_code is True
            )
            plan = _planned(chain.chain_id, draft, schedule, tokens, payee_holds)
        except (InvalidSubmissionError, JsonRpcError, ChainError) as e:
            plans.append(e)
            continue
        except Exception as e:  # a bug of ours: this submission's alone, the rest planned
            logger.exception("planning 0x%s: internal error", submission.id.hex())
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
    at = chain.seen_tag
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
        try:
            again += _estimated(
                chain.chain_id, draft, plan, answer, plans, schedule, tokens, payee_holds
            )
        except ChainError as e:  # a node's malformed answer: this submission's alone
            plans[draft.index] = e
        except Exception as e:  # a bug of ours: this submission's alone
            logger.exception("estimating 0x%s: internal error", draft.submission.id.hex())
            plans[draft.index] = e
    await _estimate(chain, again, plans, schedule, tokens, sender, payee_holds)


def _estimated(
    chain_id: int,
    draft: _Draft,
    plan: Plan,
    answer: Answer,
    plans: list[Planned],
    schedule: GasSchedule,
    tokens: Mapping[ChecksumAddress, Erc20 | None],
    payee_holds: Collection[ChecksumAddress],
) -> list[tuple[_Draft, Plan]]:
    """`draft`'s estimate `answer` read into its plan, in `plans`: its gas limit, or why it has
    none; or, its payer's wrapped signature to be prepared first, it drafted again (returned)."""
    if answer.error is None:
        plans[draft.index] = replace(plan, gas_limit=quantity(answer.value()))
        return []
    if answer.error.code != REVERTED:
        plans[draft.index] = answer.error
        return []
    signed = draft.submission.transfer
    if (
        not draft.prepare
        and signed is not None
        and signed.deployment is not None
        and draft.payer_has_code
    ):
        prepared = replace(draft, prepare=True)
        return [(prepared, _planned(chain_id, prepared, schedule, tokens, payee_holds))]
    plans[draft.index] = SimulationFailedError(revert_data(answer.error))
    return []


def _transaction(
    submission: Submission, runner: Runner, sender: ChecksumAddress, gas_limit: int | None = None
) -> dict[str, object]:
    """The submission's transaction with `runner`, from `sender`, as eth_call and
    eth_estimateGas take it: no fees, so no balance is needed; at `gas_limit`, unless it's
    being estimated (a limit would cap the estimate's search)."""
    return _deployer_call(runner, sender, submission.authorizations, gas_limit)


def _deployer_call(
    runner: Runner,
    sender: ChecksumAddress,
    authorizations: Sequence[SignedSetCodeAuthorization],
    gas_limit: int | None = None,
) -> dict[str, object]:
    """A transaction deploying `runner`, from `sender`, with `authorizations`, as eth_call and
    eth_estimateGas take it (_transaction)."""
    tx: dict[str, object] = {"from": sender, "to": DEPLOYER, "data": "0x" + runner.initcode.hex()}
    if gas_limit is not None:
        tx["gas"] = hex(gas_limit)
    if authorizations:
        tx["authorizationList"] = [authorization_json(a) for a in authorizations]
    return tx


async def empty(chain: Chain, authorities: Sequence[ChecksumAddress]) -> int:
    """How many of `authorities` are empty (no balance, no nonce, no code), read in one
    batch."""
    return _count_empty(
        await chain.batch([c for authority in authorities for c in _authority_reads(authority)])
    )


@dataclass(frozen=True, slots=True)
class CallAccounts:
    """What a quote reads of its call's accounts: those EIP-7702 delegated in its transaction
    (its authorities, and its target if delegated already), how many of its authorities are
    empty, and which of them are delegated already (`delegating`)."""

    delegated: frozenset[ChecksumAddress]
    empty: int
    delegating: frozenset[ChecksumAddress]


async def call_accounts(
    chain: Chain, target: ChecksumAddress, authorities: Sequence[ChecksumAddress]
) -> CallAccounts:
    """`target`'s code, and each authority's balance, nonce and code, read in one batch."""
    calls = [Call("eth_getCode", [target, "latest"])]
    calls += [c for authority in authorities for c in _authority_reads(authority)]
    answers = await chain.batch(calls)
    delegated = set(authorities)
    if code_of(answers[0].value()).startswith(_DELEGATION_PREFIX):
        delegated.add(target)
    delegating = frozenset(
        authority
        for i, authority in enumerate(authorities)
        if code_of(answers[3 + 3 * i].value()).startswith(_DELEGATION_PREFIX)
    )
    return CallAccounts(frozenset(delegated), _count_empty(answers[1:]), delegating)


async def call_gas(
    chain: Chain,
    call: EncodedCall,
    payment: Payment,
    authorizations: Sequence[SignedSetCodeAuthorization],
    accounts: CallAccounts,
    schedule: GasSchedule,
    sender: ChecksumAddress,
    *,
    deployment: Deployment | None = None,
    deploy_gas: int = 0,
) -> int:
    """The gas `call` needs as it will run: made by a runner paying `payment`, in `sender`'s
    transaction with `authorizations`, after `deployment` (whose factory call takes
    `deploy_gas`). One eth_estimateGas of the runner less its payment's transfer and check
    (estimating_runner), the gas left for the call in a transaction of that estimate: the runner's
    own steps each at their bound, as the runner sent has them, so what they don't use is the
    call's in both. SimulationFailedError if the call reverts."""
    try:
        runner = estimating_runner(
            call, payment, schedule, accounts.delegated, deployment=deployment
        )
    except RunnerError as e:
        raise InvalidSubmissionError("call", str(e)) from e
    tx = _deployer_call(runner, sender, authorizations)
    (answer,) = await chain.batch([Call("eth_estimateGas", [tx, "latest"])])
    if answer.error is not None:
        if answer.error.code == REVERTED:
            raise SimulationFailedError(revert_data(answer.error))
        raise answer.error
    runner_gas = runner_gas_within(
        runner.initcode, quantity(answer.value()), schedule, len(authorizations), accounts.empty
    )
    return client_gas_within(runner_gas - runner.overhead_gas - deploy_gas)


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
        if quantity(answers[i].value()) == 0
        and quantity(answers[i + 1].value()) == 0
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
    revert: bytes | BlockUnseenError | ChainError | None = BlockUnseenError(chain.seen)
    for _ in chain.nodes():
        (revert,) = await simulations(chain, [Simulation(submission, plan, sender)])
        if not isinstance(revert, BlockUnseenError):
            break
    if isinstance(revert, BlockUnseenError | ChainError):
        raise revert
    if revert is not None:
        raise SimulationFailedError(revert)


async def simulations(
    chain: Chain, sims: Sequence[Simulation]
) -> list[bytes | BlockUnseenError | ChainError | None]:
    """Each submission's transaction as eth_call from its sender, in one batch, at the highest
    block the chain has seen (a node behind it answers that it hasn't the block, never an older
    state): each its revert data, None if it deploys its runner (the deployer answering its
    address, 32 bytes), BlockUnseenError if the node hadn't the block, or ChainError if its
    answer isn't one."""
    at = chain.seen_tag
    calls = [
        Call(
            "eth_call",
            [_transaction(sim.submission, sim.plan.runner, sim.sender, sim.plan.gas_limit), at],
        )
        for sim in sims
    ]
    reverts: list[bytes | BlockUnseenError | ChainError | None] = []
    for answer in await chain.batch(calls):
        try:
            reverts.append(_revert(chain, answer))
        except ChainError as e:  # a node's malformed answer: this simulation's alone
            reverts.append(e)
    return reverts


def _revert(chain: Chain, answer: Answer) -> bytes | BlockUnseenError | None:
    """A simulation's answer: its revert data, None if it went through, or BlockUnseenError if
    its node hadn't the block. The deployer answers the runner's address, a word; an address
    without code answers nothing, and ran no runner, so checked no payment; anything else
    isn't an answer (ChainError)."""
    if _unseen(answer):
        return BlockUnseenError(chain.seen)
    if answer.error is not None:
        return revert_data(answer.error)
    if answer.result == "0x":
        return b""
    word_bytes(answer.result)
    return None


def _unseen(answer: Answer) -> bool:
    """Whether a node answered an eth_call that it hasn't the block asked at: EIP-1474's
    resource not found (-32001), geth's "header not found", or no result at all (Sepolia's
    nodes, 2026-10-26: publicnode and Tenderly the first, ethpandaops the last). A revert is
    another error (code 3)."""
    if answer.error is None:
        return answer.result is None
    return answer.error.code == _RESOURCE_NOT_FOUND or answer.error.message == _HEADER_NOT_FOUND
