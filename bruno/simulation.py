"""A submission's simulation: its plan's transactions run as planned, one eth_simulateV1 each,
every transaction in the state the one before left, and what its answer says (the first
transaction to fail, its revert data; a node without the block asked at). Many submissions'
simulations are one batch."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

import msgspec
from eth_typing import ChecksumAddress

from .chain import Chain, ChainError, data_of, quantity, unseen
from .common import BrunoError
from .jsonrpc import Answer, Call
from .plan import Plan, Step, signed_json, transaction_json
from .refusals import Refusal, refused
from .reverts import SimulationFailedError
from .sponsorship import Sponsorship
from .submission import InvalidSubmissionError, Submission
from .transactions import Fees


class BlockUnseenError(BrunoError):
    """The node simulating a submission hasn't the block it was asked at yet: nothing is known
    of whether it reverts."""

    def __init__(self, block: int) -> None:
        self.refused = refused(Refusal.BLOCK_UNSEEN, block=block)
        super().__init__(str(self.refused))
        self.block = block


# eth_simulateV1's error for a transaction whose sender can't pay its gas and value
# (execution-apis' -38014, as anvil answers it): it answers the whole simulation
_INSUFFICIENT_FUNDS = -38014


class UnfundedError(ChainError):
    """A node answered a simulation that one of its transactions' senders can't pay its gas
    and value, in the state the transactions before it left."""


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
    """A submission's transactions as planned, the relay's from `sender`: no fees or nonces,
    nothing validated, as when ranking a batch or taking a submission, before an account is
    chosen. Another account's are as it signed them."""

    submission: Submission | Sponsorship
    plan: Plan
    sender: ChecksumAddress

    def request(self, at: str) -> Call:
        calls = [
            transaction_json(step, self.sender, step.gas_limit)
            if isinstance(step, Step)
            else signed_json(step.transaction)
            for step in self.plan.steps
        ]
        block: dict[str, object] = {"calls": calls}
        sending = sum(step.transaction.value for step in self.plan.own)
        if sending:
            # the sender is only stood in for the account that will send them: it holds what
            # they send, whatever it holds on the chain
            block["stateOverrides"] = {self.sender: {"balance": hex(sending)}}
        return Call("eth_simulateV1", [{"blockStateCalls": [block]}, at])


@dataclass(frozen=True, slots=True)
class SignedSimulation:
    """A submission's transactions as they'll be signed: the relay's from `sender`, its
    account, at its consecutive nonces from `nonce`, at `fees`, and another account's as it
    signed them; validated as a block takes them (each nonce, fee and balance)."""

    submission: Submission | Sponsorship
    plan: Plan
    sender: ChecksumAddress
    nonce: int
    fees: Fees

    def request(self, at: str) -> Call:
        calls: list[dict[str, object]] = []
        nonce = self.nonce
        for step in self.plan.steps:
            if not isinstance(step, Step):
                calls.append(signed_json(step.transaction))
                continue
            call = transaction_json(step, self.sender, step.gas_limit)
            call["nonce"] = hex(nonce)
            call["maxFeePerGas"] = hex(self.fees.max_fee_per_gas)
            call["maxPriorityFeePerGas"] = hex(self.fees.max_priority_fee_per_gas)
            calls.append(call)
            nonce += 1
        payload = {"blockStateCalls": [{"calls": calls}], "validation": True}
        return Call("eth_simulateV1", [payload, at])


async def simulate(
    chain: Chain, submission: Submission | Sponsorship, plan: Plan, sender: ChecksumAddress
) -> None:
    """The submission's transactions simulated from `sender`: raise the revert data of the
    first that reverts. A node without the block is passed over for another (Chain.batch):
    BlockUnseenError if none had it. A sponsorship one of whose transactions can't be paid
    for is the client's to hear of, not a node failing: the relay's own are given what they
    send, so it's the account's. If the relay's first transaction fails run alone (the
    account's code failing as it's sent its ETH, or given too little gas), that's its revert;
    otherwise the account's transactions cost more than it holds and is sent
    (InvalidSubmissionError)."""
    (revert,) = await simulations(chain, [Simulation(submission, plan, sender)])
    if isinstance(revert, UnfundedError) and isinstance(submission, Sponsorship):
        first = plan.own[0]
        funding = transaction_json(first, sender, first.gas_limit)
        await gas_used(chain, [funding], {sender: first.transaction.value})
        raise InvalidSubmissionError("transactions", refused(Refusal.SPONSORED_UNFUNDED))
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
    gas_used: str = "0x0"


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


async def gas_used(
    chain: Chain, calls: Sequence[dict[str, object]], holding: Mapping[ChecksumAddress, int]
) -> list[int]:
    """The gas each of `calls` uses, run one after another at the highest block seen, each in
    the state the one before left, nothing validated; the accounts in `holding` given those
    balances first. SimulationFailedError, with its revert data, for the first that fails;
    BlockUnseenError if no node had the block."""
    block: dict[str, object] = {"calls": list(calls)}
    if holding:
        block["stateOverrides"] = {a: {"balance": hex(wei)} for a, wei in holding.items()}
    request = Call("eth_simulateV1", [{"blockStateCalls": [block]}, chain.seen_tag])
    (answer,) = await chain.batch([request])
    if unseen(answer):
        raise BlockUnseenError(chain.seen)
    if answer.error is not None:
        raise ChainError(f"eth_simulateV1: {answer.error}")
    made = _simulated_calls(answer.result)
    if len(made) != len(calls):
        raise ChainError(f"eth_simulateV1: {len(made)} calls, {len(calls)} sent")
    for call in made:
        if quantity(call.status) != 1:
            error = call.error
            raise SimulationFailedError(
                b"" if error is None or error.data is None else data_of(error.data)
            )
    return [quantity(call.gas_used) for call in made]


async def fails(chain: Chain, transaction: dict[str, object], block: int) -> bool:
    """Whether `transaction`, a call as eth_simulateV1 takes it, fails simulated alone at
    `block`: reverting, or running out of its gas. BlockUnseenError if no node had the block;
    ChainError if a node's answer isn't a simulation's."""
    request = Call("eth_simulateV1", [{"blockStateCalls": [{"calls": [transaction]}]}, hex(block)])
    (answer,) = await chain.batch([request])
    if unseen(answer):
        raise BlockUnseenError(block)
    if answer.error is not None:
        raise ChainError(f"eth_simulateV1: {answer.error}")
    calls = _simulated_calls(answer.result)
    if len(calls) != 1:
        raise ChainError(f"eth_simulateV1: {len(calls)} calls, 1 sent")
    return quantity(calls[0].status) != 1


def _first_revert(chain: Chain, plan: Plan, answer: Answer) -> bytes | BlockUnseenError | None:
    """A simulation's answer: the first of `plan`'s transactions to fail, its revert data (a
    call failing, or doing other than what it's sent for, as its proof reads its result); None
    if all went through; or BlockUnseenError if its node hadn't the block. An error answering
    the whole simulation isn't a revert, but the node failing (ChainError)."""
    if unseen(answer):
        return BlockUnseenError(chain.seen)
    if answer.error is not None:
        failed = UnfundedError if answer.error.code == _INSUFFICIENT_FUNDS else ChainError
        raise failed(f"eth_simulateV1: {answer.error}")
    calls = _simulated_calls(answer.result)
    if len(calls) != len(plan.steps):
        raise ChainError(f"eth_simulateV1: {len(calls)} calls, {len(plan.steps)} sent")
    for step, call in zip(plan.steps, calls, strict=True):
        if quantity(call.status) != 1:
            error = call.error
            return b"" if error is None or error.data is None else data_of(error.data)
        if isinstance(step, Step):  # another account's did what it's for if it didn't fail
            revert = step.proof.revert_of(call.return_data)
            if revert is not None:
                return revert
    return None


def _simulated_calls(result: object) -> list[_SimulatedCall]:
    """The calls of eth_simulateV1's one simulated block: ChainError if `result` isn't one."""
    try:
        (block,) = msgspec.convert(result, list[_SimulatedBlock])
    except (msgspec.ValidationError, ValueError) as e:
        raise ChainError(f"eth_simulateV1: not a block's calls: {result!r}") from e
    return block.calls
