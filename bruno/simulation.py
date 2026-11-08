"""A submission's simulation: its plan's transactions run as planned, one eth_simulateV1 each,
every transaction in the state the one before left, and what its answer says (the first
transaction to fail, its revert data; a node without the block asked at). Many submissions'
simulations are one batch."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import msgspec
from eth_typing import ChecksumAddress

from .chain import Chain, ChainError, data_of, quantity
from .common import BrunoError
from .jsonrpc import Answer, Call
from .plan import Plan, SimulationFailedError, transaction_json
from .refusals import Refusal, message
from .submission import Submission
from .transactions import Fees

# how a node says it hasn't a block it's asked at: EIP-1474's code, geth's message, and the
# start of anvil's (a block past its head, as after its snapshot's reverted)
_RESOURCE_NOT_FOUND = -32001
_HEADER_NOT_FOUND = "header not found"
_BLOCK_OUT_OF_RANGE = "BlockOutOfRangeError"


class BlockUnseenError(BrunoError):
    """The node simulating a submission hasn't the block it was asked at yet: nothing is known
    of whether it reverts."""

    def __init__(self, block: int) -> None:
        super().__init__(message(Refusal.BLOCK_UNSEEN, block=block))
        self.block = block


class Simulating(Protocol):
    """A submission's transactions to simulate, and how: what each kind asks eth_simulateV1."""

    @property
    def plan(self) -> Plan:
        """The transactions simulated."""
        ...

    def request(self, at: str) -> Call:
        """Its eth_simulateV1, at the block `at`."""
        ...


@dataclass(frozen=True, slots=True)
class Simulation:
    """A submission's transactions as planned, from `sender`: no fees or nonces, nothing
    validated, as when ranking a batch or taking a submission, before an account is chosen."""

    submission: Submission
    plan: Plan
    sender: ChecksumAddress

    def request(self, at: str) -> Call:
        calls = [
            transaction_json(self.submission, step.transaction, self.sender, step.gas_limit)
            for step in self.plan.steps
        ]
        return Call("eth_simulateV1", [{"blockStateCalls": [{"calls": calls}]}, at])


@dataclass(frozen=True, slots=True)
class SignedSimulation:
    """A submission's transactions as they'll be signed: from `sender`, its account, at its
    consecutive nonces from `nonce`, at `fees`, validated as a block takes them (each nonce,
    fee and balance)."""

    submission: Submission
    plan: Plan
    sender: ChecksumAddress
    nonce: int
    fees: Fees

    def request(self, at: str) -> Call:
        calls: list[dict[str, object]] = []
        for i, step in enumerate(self.plan.steps):
            call = transaction_json(self.submission, step.transaction, self.sender, step.gas_limit)
            call["nonce"] = hex(self.nonce + i)
            call["maxFeePerGas"] = hex(self.fees.max_fee_per_gas)
            call["maxPriorityFeePerGas"] = hex(self.fees.max_priority_fee_per_gas)
            calls.append(call)
        payload = {"blockStateCalls": [{"calls": calls}], "validation": True}
        return Call("eth_simulateV1", [payload, at])


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
    chain: Chain, sims: Sequence[Simulating]
) -> list[bytes | BlockUnseenError | ChainError | None]:
    """Each submission's transactions simulated, one eth_simulateV1 each (its transactions one
    after another, each in the state the one before left), as its kind asks (as planned, or as
    signed), in one batch, at the highest block the chain has seen (a node behind it answers
    that it hasn't the block, never an older state): each its first revert's data, None if
    every transaction did what it's sent for (as its proof reads it), BlockUnseenError if the
    node hadn't the block, or ChainError if its answer isn't one (a transaction as signed that
    a block wouldn't take among them: its nonce, fee or balance)."""
    at = chain.seen_tag
    calls = [sim.request(at) for sim in sims]
    reverts: list[bytes | BlockUnseenError | ChainError | None] = []
    for sim, answer in zip(sims, await chain.batch(calls), strict=True):
        try:
            reverts.append(_first_revert(chain, sim.plan, answer))
        except ChainError as e:  # a node's malformed answer: this simulation's alone
            reverts.append(e)
    return reverts


def _first_revert(chain: Chain, plan: Plan, answer: Answer) -> bytes | BlockUnseenError | None:
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
