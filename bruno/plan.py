"""A submission's transaction, by its proof of payment (the runner measuring it, or the
EntryPoint's own rules), and its gas limit under a block's schedule, from what the chain says
of its call's target and its authorities; and its simulation. Many submissions' reads, and
their simulations, are each one batch."""

import logging
from abc import ABC, abstractmethod
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import override

import msgspec
from eth_account.datastructures import SignedSetCodeAuthorization
from eth_typing import ChecksumAddress, HexStr

from .abi import selector
from .chain import (
    Chain,
    ChainError,
    code_of,
    data_of,
    has_code,
    quantity,
    revert_data,
    word_bytes,
)
from .common import BrunoError
from .deployer import DEPLOYER, runner_gas_within, transaction_gas
from .eip3009 import transfer_gas
from .erc6492 import Deployment
from .evm import WARM_ACCOUNT_ACCESS, EncodedCall
from .forks import GasSchedule
from .jsonrpc import REVERTED, Answer, Call, JsonRpcError
from .refusals import Refusal, message
from .runner import (
    BALANCE_UNREADABLE,
    GAS_EXHAUSTED,
    UNDERPAID,
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
from .transactions import Fees, authorization_json, unsigned_size
from .userops import FAILED_OP, EntryPoint, FeePayment, handle_ops_beneficiary

logger = logging.getLogger(__name__)

_DELEGATION_PREFIX = bytes.fromhex("ef0100")
# how a node says it hasn't a block it's asked at: EIP-1474's code, geth's message, and the
# start of anvil's (a block past its head, as after its snapshot's reverted)
_RESOURCE_NOT_FOUND = -32001
_HEADER_NOT_FOUND = "header not found"
_BLOCK_OUT_OF_RANGE = "BlockOutOfRangeError"


class SimulationFailedError(BrunoError):
    """The submission's transaction reverts: with `revert_data`, what it says read for its
    message (revert_reason)."""

    def __init__(self, revert_data: bytes) -> None:
        super().__init__(message(Refusal.SIMULATION_FAILED, reason=revert_reason(revert_data)))
        self.revert_data = revert_data


# Solidity's revert reason and panic
_ERROR = selector("Error(string)")
_PANIC = selector("Panic(uint256)")


def revert_reason(data: bytes) -> str:
    """What a simulation's revert data says, and what to do next: Solidity's revert reason or
    panic, the runner's own errors, the EntryPoint's FailedOp; anything else by its
    selector."""
    head = data[:4]
    if head == UNDERPAID:
        return message(Refusal.REVERT_UNDERPAID)
    if head == GAS_EXHAUSTED:
        return message(Refusal.REVERT_GAS_EXHAUSTED)
    if head == BALANCE_UNREADABLE:
        return message(Refusal.REVERT_BALANCE_UNREADABLE)
    if head == FAILED_OP and (text := _string_at(data, 1)) is not None:
        return message(Refusal.REVERT_FAILED_OP, text=text)
    if head == _ERROR and (text := _string_at(data, 0)) is not None:
        return message(Refusal.REVERT_ERROR, text=text)
    if head == _PANIC and len(data) == 36:
        return message(Refusal.REVERT_PANIC, code=f"{int.from_bytes(data[4:]):x}")
    if not data:
        return message(Refusal.REVERT_EMPTY)
    return message(Refusal.REVERT_OTHER, selector="0x" + head.hex())


def _string_at(data: bytes, index: int) -> str | None:
    """The ABI-encoded string whose offset is argument `index` of `data`'s arguments, as UTF-8;
    None if it doesn't decode."""
    args = data[4:]
    if len(args) < 32 * (index + 1):
        return None
    offset = int.from_bytes(args[32 * index : 32 * index + 32])
    if offset + 32 > len(args):
        return None
    length = int.from_bytes(args[offset : offset + 32])
    if offset + 32 + length > len(args):
        return None
    try:
        return args[offset + 32 : offset + 32 + length].decode()
    except UnicodeDecodeError:
        return None


class BlockUnseenError(BrunoError):
    """The node simulating a submission hasn't the block it was asked at yet: nothing is known
    of whether it reverts."""

    def __init__(self, block: int) -> None:
        super().__init__(message(Refusal.BLOCK_UNSEEN, block=block))
        self.block = block


@dataclass(frozen=True, slots=True)
class Step:
    """One transaction of a plan, as its concurrency account makes it: its `transaction`
    (target, calldata and the wei it sends), its result read by its `proof`; its gas limit
    under a block's schedule, and the most bytes it takes unsigned (what's paid beyond gas is
    by size)."""

    transaction: EncodedCall
    proof: Proof
    gas_limit: int
    size: int

    def cost(self, fees: Fees) -> int:
        """The most it costs its sender at `fees`: its gas, the wei it sends, and what's paid
        beyond gas."""
        return fees.cost(self.gas_limit, self.size, self.transaction.value)


@dataclass(frozen=True, slots=True)
class Plan:
    """A submission's transactions, in order (`steps`), simulated together, one after
    another; `estimated` if a gas limit is its transaction's estimate (a step runs code the
    client chooses)."""

    steps: tuple[Step, ...]
    estimated: bool = False

    @property
    def gas_limit(self) -> int:
        """Its transactions' gas limits, together."""
        return sum(step.gas_limit for step in self.steps)

    @property
    def size(self) -> int:
        """Its transactions' most bytes unsigned, together."""
        return sum(step.size for step in self.steps)

    def cost(self, fees: Fees) -> int:
        """The most its transactions cost at `fees`, together, each costed as its own."""
        return sum(step.cost(fees) for step in self.steps)


class Proof(ABC):
    """How the relay knows a submission pays it: what it pays in a block, the transaction that
    shows it, and how that transaction's simulation reads. Whoever takes a submission says
    which; nothing else does."""

    @abstractmethod
    def paying(self, submission: Submission, base_fee: int) -> int:
        """What `submission` pays the payee in a block of `base_fee`."""
        ...

    @abstractmethod
    def plan(
        self,
        chain_id: int,
        draft: _Draft,
        schedule: GasSchedule,
        tokens: Mapping[ChecksumAddress, Erc20 | None],
        payee_holds: Collection[ChecksumAddress],
    ) -> Plan:
        """`draft`'s transaction, and its gas limit from the gas schedule: if it's to be
        estimated, only a placeholder until it is."""
        ...

    @abstractmethod
    def revert_of(self, result: object) -> bytes | None:
        """A simulation of its transaction that didn't fail, its `result` (the call's return
        data): None if it did what it's sent for, else its revert data; ChainError if it isn't
        an answer."""
        ...


@dataclass(frozen=True, slots=True)
class MeasuredByRunner(Proof):
    """Paid as the runner measures it: a runner, deployed through the deployer, makes the
    submission's transfer, if any, and its call, and reverts unless the payee gained its
    amount, so a transaction that doesn't pay doesn't land."""

    @override
    def paying(self, submission: Submission, base_fee: int) -> int:
        return submission.amount

    @override
    def plan(
        self,
        chain_id: int,
        draft: _Draft,
        schedule: GasSchedule,
        tokens: Mapping[ChecksumAddress, Erc20 | None],
        payee_holds: Collection[ChecksumAddress],
    ) -> Plan:
        return _runner_planned(chain_id, draft, schedule, tokens, payee_holds)

    @override
    def revert_of(self, result: object) -> bytes | None:
        """The deployer answers the runner's address, a word; an address without code answers
        nothing, and ran no runner, so checked no payment: a revert."""
        if result == "0x":
            return b""
        word_bytes(result)
        return None


@dataclass(frozen=True, slots=True)
class ByEntryPoint(Proof):
    """Paid by the EntryPoint's own rules, for the relay's own handleOps of an op paying by its
    fees (`fee_payment`), the payee its beneficiary, sent as is: a handleOps that lands has paid
    the beneficiary the op's gas at its price (its call reverting or not), and one whose op
    fails its validation reverts, so doesn't land. A submission that isn't such a handleOps is
    a bug of ours: ProofError, never sent."""

    entry_point: EntryPoint
    fee_payment: FeePayment

    @override
    def paying(self, submission: Submission, base_fee: int) -> int:
        return self.fee_payment.at(base_fee)

    @override
    def plan(
        self,
        chain_id: int,
        draft: _Draft,
        schedule: GasSchedule,
        tokens: Mapping[ChecksumAddress, Erc20 | None],
        payee_holds: Collection[ChecksumAddress],
    ) -> Plan:
        submission = draft.submission
        call = submission.call
        if call.to != self.entry_point.address:
            raise ProofError(f"handleOps to {call.to}, not EntryPoint {self.entry_point.version}'s")
        if handle_ops_beneficiary(bytes.fromhex(call.data.removeprefix("0x"))) != submission.payee:
            raise ProofError("a handleOps whose beneficiary isn't the payee")
        return entry_point_plan(
            chain_id, call, submission.gas, schedule, len(draft.authorities), draft.empty, self
        )

    @override
    def revert_of(self, result: object) -> bytes | None:
        """handleOps answers nothing; an op it refuses is an error, a revert."""
        if result != "0x":
            raise ChainError(f"handleOps answered {result!r}")
        return None


class ProofError(BrunoError):
    """A submission its proof can't be of: a bug of ours, that submission's alone."""


async def make_plan(
    chain: Chain,
    submission: Submission,
    proof: Proof,
    schedule: GasSchedule,
    tokens: Mapping[ChecksumAddress, Erc20 | None],
    sender: ChecksumAddress,
    payee_holds: Collection[ChecksumAddress] = (),
) -> Plan:
    """The submission's transaction by its `proof`, and its gas limit under `schedule`, paying
    in its token as `tokens` reads it, sent by `sender`; a transfer's gas bounded knowing which
    tokens the payee holds some of (`payee_holds`)."""
    (planned,) = await make_plans(
        chain, [submission], [proof], schedule, tokens, sender, payee_holds
    )
    if isinstance(planned, Exception):
        raise planned
    return planned


# a submission's plan, or why it has none: the client's mistake (InvalidSubmissionError), its
# simulation failing, a node's failure (JsonRpcError, ChainError), or a bug of ours, logged
type Planned = Plan | Exception


async def make_plans(
    chain: Chain,
    submissions: Sequence[Submission],
    proofs: Sequence[Proof],
    schedule: GasSchedule,
    tokens: Mapping[ChecksumAddress, Erc20 | None],
    sender: ChecksumAddress,
    payee_holds: Collection[ChecksumAddress] = (),
) -> list[Planned]:
    """Each submission's transaction by its proof (`proofs`, one each), and its gas limit
    under `schedule`, sent by `sender`, their reads in one batch (its call's target's code; each
    authority's balance, nonce and code; a payer's code): each its plan, or why not.

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
    for submission, proof, of, first in zip(submissions, proofs, authorities, firsts, strict=True):
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
                len(plans),
                submission,
                proof,
                of,
                delegated,
                empty,
                payer_has_code=payer_code is True,
            )
            plan = proof.plan(chain.chain_id, draft, schedule, tokens, payee_holds)
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
    """A submission as read, its plan by its `proof` at `index` of make_plans'; `prepare` to
    call its wrapped signature's factory however its payer's code reads."""

    index: int
    submission: Submission
    proof: Proof
    authorities: tuple[ChecksumAddress, ...]
    delegated: set[ChecksumAddress]
    empty: int
    payer_has_code: bool
    prepare: bool = False


def _runner_planned(
    chain_id: int,
    draft: _Draft,
    schedule: GasSchedule,
    tokens: Mapping[ChecksumAddress, Erc20 | None],
    payee_holds: Collection[ChecksumAddress],
) -> Plan:
    """The draft's runner's transaction, and its gas limit from the gas schedule: for one with
    a step that runs code the client chooses, only a placeholder until it's estimated."""
    submission = draft.submission
    if submission.token not in tokens:
        raise InvalidSubmissionError(
            "payment",
            message(Refusal.TOKEN_SUSPENDED, token=submission.token),
        )
    token = tokens[submission.token]
    deployment = submission.to_deploy
    transfer = None
    contract_payer = False
    if submission.transfer is not None:
        if token is None or token.transfer_gas is None:
            raise InvalidSubmissionError(
                "payment",
                message(Refusal.NO_EIP3009, token=submission.token),
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
    transaction = EncodedCall(DEPLOYER, HexStr("0x" + runner.initcode.hex()))
    return Plan((Step(transaction, MeasuredByRunner(), gas_limit, size),), estimated=estimated)


def entry_point_plan(
    chain_id: int,
    call: EncodedCall,
    call_gas: int,
    schedule: GasSchedule,
    authorizations: int,
    empty: int,
    proof: Proof,
) -> Plan:
    """`call`, a handleOps, sent as is, by `proof`: its transaction's gas limit from the gas
    schedule, the call given `call_gas` (handleOps' own gas, a node's estimate from its
    beneficiary, which the EntryPoint pays last, less its intrinsic gas), with `authorizations`
    of which `empty` are from empty accounts; and its most bytes unsigned. Sent from a
    concurrency account, the beneficiary is cold when it's paid: its access as estimated was
    warm."""
    data = bytes.fromhex(call.data.removeprefix("0x"))
    intrinsic = schedule.intrinsic(data, authorizations=authorizations)
    beneficiary = schedule.cold_account_access - WARM_ACCOUNT_ACCESS
    execution = call_gas + beneficiary + schedule.authorizations(empty, authorizations)
    gas_limit = max(intrinsic.standard + execution, intrinsic.floor)
    size = unsigned_size(chain_id, call.to, data, authorizations=authorizations)
    return Plan((Step(call, proof, gas_limit, size),))


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
    again, prepared. A plan estimated is a runner's, its one transaction."""
    if not drafts:
        return
    at = chain.seen_tag
    calls: list[Call] = []
    for draft, plan in drafts:
        (step,) = plan.steps
        calls.append(
            Call("eth_estimateGas", [_transaction(draft.submission, step.transaction, sender), at])
        )
    answers = await chain.batch(calls)
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
        (step,) = plan.steps
        estimated = replace(step, gas_limit=quantity(answer.value()))
        plans[draft.index] = replace(plan, steps=(estimated,))
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
        return [(prepared, _runner_planned(chain_id, prepared, schedule, tokens, payee_holds))]
    plans[draft.index] = SimulationFailedError(revert_data(answer.error))
    return []


def _transaction(
    submission: Submission,
    transaction: EncodedCall,
    sender: ChecksumAddress,
    gas_limit: int | None = None,
) -> dict[str, object]:
    """The submission's `transaction`, from `sender`, as eth_call and eth_estimateGas take it:
    no fees, so no balance is needed; at `gas_limit`, unless it's being estimated (a limit
    would cap the estimate's search)."""
    return _call_json(transaction, sender, submission.authorizations, gas_limit)


def _call_json(
    transaction: EncodedCall,
    sender: ChecksumAddress,
    authorizations: Sequence[SignedSetCodeAuthorization],
    gas_limit: int | None = None,
) -> dict[str, object]:
    """`transaction`, from `sender`, with `authorizations`, as eth_call and eth_estimateGas
    take it (_transaction)."""
    tx: dict[str, object] = {"from": sender, "to": transaction.to, "data": transaction.data}
    if transaction.value:
        tx["value"] = hex(transaction.value)
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
    deploying = EncodedCall(DEPLOYER, HexStr("0x" + runner.initcode.hex()))
    tx = _call_json(deploying, sender, authorizations)
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
    """A submission's transactions, as planned, from `sender`."""

    submission: Submission
    plan: Plan
    sender: ChecksumAddress


async def simulate(
    chain: Chain, submission: Submission, plan: Plan, sender: ChecksumAddress
) -> None:
    """The submission's transactions simulated from `sender`: raise the revert data of the
    first that reverts. A node without the block is passed over for another, as many times as
    there are nodes: BlockUnseenError if none had it."""
    revert: bytes | BlockUnseenError | ChainError | None = BlockUnseenError(chain.seen)
    for _ in chain.nodes():
        (revert,) = await simulations(chain, [Simulation(submission, plan, sender)])
        if not isinstance(revert, BlockUnseenError):
            break
    if isinstance(revert, BlockUnseenError | ChainError):
        raise revert
    if revert is not None:
        raise SimulationFailedError(revert)


class _CallError(msgspec.Struct, frozen=True):
    data: str | None = None


class _SimulatedCall(msgspec.Struct, frozen=True, rename="camel"):
    status: str
    return_data: str
    error: _CallError | None = None


class _SimulatedBlock(msgspec.Struct, frozen=True):
    calls: list[_SimulatedCall]


async def simulations(
    chain: Chain, sims: Sequence[Simulation]
) -> list[bytes | BlockUnseenError | ChainError | None]:
    """Each submission's transactions simulated from its sender, one eth_simulateV1 each (its
    transactions one after another, each in the state the one before left), in one batch, at
    the highest block the chain has seen (a node behind it answers that it hasn't the block,
    never an older state): each its first revert's data, None if every transaction did what
    it's sent for (as its proof reads it), BlockUnseenError if the node hadn't the block, or
    ChainError if its answer isn't one. No fees or nonces checked: the transactions are
    simulated as planned, not as signed."""
    at = chain.seen_tag
    calls = [
        Call(
            "eth_simulateV1",
            [
                {
                    "blockStateCalls": [
                        {
                            "calls": [
                                _transaction(
                                    sim.submission, step.transaction, sim.sender, step.gas_limit
                                )
                                for step in sim.plan.steps
                            ]
                        }
                    ]
                },
                at,
            ],
        )
        for sim in sims
    ]
    reverts: list[bytes | BlockUnseenError | ChainError | None] = []
    for sim, answer in zip(sims, await chain.batch(calls), strict=True):
        try:
            reverts.append(_revert(chain, sim.plan, answer))
        except ChainError as e:  # a node's malformed answer: this simulation's alone
            reverts.append(e)
    return reverts


def _revert(chain: Chain, plan: Plan, answer: Answer) -> bytes | BlockUnseenError | None:
    """A simulation's answer: the first of `plan`'s transactions to fail, its revert data (a
    call failing, or doing other than what it's sent for, as its proof reads its result); None
    if all went through; or BlockUnseenError if its node hadn't the block. An error answering
    the whole simulation isn't a revert, but the node failing (ChainError)."""
    if _unseen(answer):
        return BlockUnseenError(chain.seen)
    if answer.error is not None:
        raise ChainError(f"eth_simulateV1: {answer.error}")
    try:
        (block,) = msgspec.convert(answer.result, list[_SimulatedBlock])
    except (msgspec.ValidationError, ValueError) as e:
        raise ChainError(f"eth_simulateV1: not a block's calls: {answer.result!r}") from e
    if len(block.calls) != len(plan.steps):
        raise ChainError(f"eth_simulateV1: {len(block.calls)} calls, {len(plan.steps)} sent")
    for step, call in zip(plan.steps, block.calls, strict=True):
        if quantity(call.status) != 1:
            error = call.error
            return b"" if error is None or error.data is None else data_of(error.data)
        revert = step.proof.revert_of(call.return_data)
        if revert is not None:
            return revert
    return None


def _unseen(answer: Answer) -> bool:
    """Whether a node answered a simulation or an estimate that it hasn't the block asked at: EIP-1474's
    resource not found (-32001), geth's "header not found", anvil's BlockOutOfRangeError, or no
    result at all (Sepolia's nodes, 2026-10-26: publicnode and Tenderly the first, ethpandaops
    the last). A revert is another error (code 3)."""
    if answer.error is None:
        return answer.result is None
    message = answer.error.message
    return (
        answer.error.code == _RESOURCE_NOT_FOUND
        or message == _HEADER_NOT_FOUND
        or message.startswith(_BLOCK_OUT_OF_RANGE)
    )
