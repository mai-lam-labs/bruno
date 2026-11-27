"""A submission's transactions, by its proof of payment (the runner measuring it, the
EntryPoint's own rules, or its transfer sent with its calls), and their gas limits under a
block's schedule, from what the chain says of its calls' targets and its authorities. The one path: a quote, the bundler's estimates
and a submission are each drafted (Draft), their proof chosen in one place (proof_of), and
planned alike (plan_drafts). Many submissions' reads are one batch; their simulation is
simulation.py's."""

import logging
from abc import ABC, abstractmethod
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Generic, TypeVar, overload, override

from eth_account.datastructures import SignedSetCodeAuthorization
from eth_typing import ChecksumAddress

from .accountreads import (
    CallAccounts,
    account_reads,
    accounts_of,
    authority_reads,
    empty_among,
)
from .chain import (
    Chain,
    ChainError,
    data_of,
    has_code,
    quantity,
    revert_data,
    word_of,
)
from .common import BrunoError
from .deployer import (
    ANSWER_BYTES,
    STAND_IN_SALT,
    runner_call,
    runner_gas_within,
    transaction_gas,
)
from .eip3009 import transfer_gas, transfer_parties
from .erc6492 import Deployment
from .evm import WARM_ACCOUNT_ACCESS, EncodedCall
from .forks import GasSchedule
from .forwarder import eth_gas, forwarding, token_gas
from .jsonrpc import REVERTED, Answer, Call, JsonRpcError, result_of
from .refusals import Refusal, refused
from .reverts import SimulationFailedError
from .runner import (
    Erc20,
    Payment,
    PlannedCall,
    Runner,
    RunnerError,
    Transfer,
    call_need,
    calls_need,
    client_gas_within,
    estimating_runner,
    in_call_runner,
)
from .sponsorship import Sponsorship
from .submission import InvalidSubmissionError, Submission, SubmittedCall
from .submission import authorities as authorities_of
from .targets import boundable, direct
from .tokens import balance_of_call
from .transactions import Decoded, Fees, authorization_json, unsigned_size
from .userops import (
    EntryPoint,
    FeePayment,
    entry_point_at,
    handle_ops_beneficiary,
)

logger = logging.getLogger(__name__)

# an amount whose word has no zero byte: the dearest calldata an amount can be
_WIDEST_AMOUNT = 2**256 - 1


@dataclass(frozen=True, slots=True)
class CostBasis:
    """What a transaction's cost is worked out from: its gas limit, its most bytes unsigned, and
    the wei it sends."""

    gas_limit: int
    size: int
    value: int

    def cost(self, fees: Fees) -> int:
        """The most it costs its sender at `fees`: its gas, the wei it sends, and what's paid
        beyond gas."""
        return fees.cost(self.gas_limit, self.size, self.value)


@dataclass(frozen=True, slots=True)
class Step:
    """One transaction of a plan, as its concurrency account makes it: its `transaction`
    (target, calldata and the wei it sends), its result read by its `proof`; its gas limit
    under a block's schedule, and the most bytes it takes unsigned (what's paid beyond gas is
    by size); the `authorizations` it carries, the submission's or none; `estimated` if its gas
    limit is its estimate (it runs code the client chooses), only a placeholder until then."""

    transaction: EncodedCall
    proof: Proof
    gas_limit: int
    size: int
    authorizations: tuple[SignedSetCodeAuthorization, ...]
    estimated: bool = False

    @property
    def basis(self) -> CostBasis:
        """What its cost is worked out from."""
        return CostBasis(self.gas_limit, self.size, self.transaction.value)


@dataclass(frozen=True, slots=True)
class SignedStep:
    """One transaction of a plan that another account signed (a sponsored account's own), sent
    as it is, in its place among the relay's. It costs the relay nothing itself: its sender
    pays for it, out of what a step before it sent."""

    transaction: Decoded

    @property
    def gas_limit(self) -> int:
        """The gas limit it was signed with."""
        return self.transaction.signed.gas


@dataclass(frozen=True, slots=True)
class Plan:
    """A submission's transactions, in order (`steps`): the relay's own (`own`), from one
    concurrency account at its consecutive nonces, and any another account signed, in their
    places; simulated together, one after another. One transaction goes in the block's bundle,
    a builder free to leave it out; several are a bundle of their own, which lands whole or not
    at all."""

    steps: tuple[Step | SignedStep, ...]

    @property
    def own(self) -> tuple[Step, ...]:
        """The transactions the relay signs and pays for, in order."""
        return tuple(step for step in self.steps if isinstance(step, Step))

    @property
    def estimated(self) -> bool:
        """Whether a step's gas limit is its estimate."""
        return any(step.estimated for step in self.own)

    @property
    def gas_limit(self) -> int:
        """Its transactions' gas limits, together, whoever signs them."""
        return sum(step.gas_limit for step in self.steps)

    @property
    def basis(self) -> tuple[CostBasis, ...]:
        """What each of the relay's own transactions' cost is worked out from, in order."""
        return tuple(step.basis for step in self.own)

    def cost(self, fees: Fees) -> int:
        """The most the relay's own transactions cost at `fees`, together, each costed as its
        own."""
        return sum(basis.cost(fees) for basis in self.basis)


class Proof(ABC):
    """How the relay knows what it has taken pays it: what it pays in a block, and how its
    transactions' simulation reads."""

    @abstractmethod
    def paying(self, amount: int, base_fee: int) -> int:
        """What's paid to the payee in a block of `base_fee`, the `amount` having been
        offered."""
        ...

    @abstractmethod
    def revert_of(self, result: object) -> bytes | None:
        """A simulation of one of its transactions that didn't fail, its `result` (the call's
        return data): None if it did what it's sent for, else its revert data; ChainError if it
        isn't an answer."""
        ...


class DraftedProof(Proof):
    """A submission's proof, and what the submission is sent as: its transactions (a runner;
    the EntryPoint's handleOps as it is; or its call and its transfer, each sent as it is).
    proof_of chooses it; nothing else does."""

    @abstractmethod
    def plan(self, chain_id: int, draft: Draft, schedule: GasSchedule) -> Plan:
        """`draft`'s transactions, and their gas limits from the gas schedule: one to be
        estimated, only a placeholder until it is."""
        ...


class QuotedProof(DraftedProof):
    """A proof a quote can be given: how its call's gas follows from an estimate, when the
    quote isn't given it, and what its call takes of its transaction's gas. An op paying by its
    fees isn't quoted: its estimate prices it."""

    @abstractmethod
    def estimating(self, draft: Draft, index: int, schedule: GasSchedule) -> EncodedCall:
        """The transaction whose estimate gives `draft`'s `index`th call its gas (call_gas)."""
        ...

    @abstractmethod
    def call_gas_of(self, estimate: int, draft: Draft, index: int, schedule: GasSchedule) -> int:
        """The most gas `draft`'s `index`th call can be given in a transaction of `estimate`."""
        ...

    @abstractmethod
    def call_part(self, client_gas: int) -> int:
        """The gas its transaction spends for its call to be given `client_gas`, the call's data
        apart (a quote's breakdown)."""
        ...


@dataclass(frozen=True, slots=True)
class MeasuredByRunner(QuotedProof):
    """Paid as the runner measures it: a runner, deployed through the deployer, makes the
    submission's deployment and transfer, if any, and its call, and reverts unless the payee
    gained its amount, so a transaction that doesn't pay doesn't land."""

    @override
    def paying(self, amount: int, base_fee: int) -> int:
        return amount

    @override
    def plan(self, chain_id: int, draft: Draft, schedule: GasSchedule) -> Plan:
        return _runner_planned(chain_id, draft, schedule, self)

    @override
    def revert_of(self, result: object) -> bytes | None:
        """The deployer answers the runner's address, 20 bytes; an address without code answers
        nothing, and ran no runner, so checked no payment: a revert."""
        if result == "0x":
            return b""
        if len(data_of(result)) != ANSWER_BYTES:
            raise ChainError(f"the deployer answered {result!r}")
        return None

    @override
    def estimating(self, draft: Draft, index: int, schedule: GasSchedule) -> EncodedCall:
        """The draft's runner, every call in it: the one estimated states no gas, so the runner
        gives it what's left."""
        initcode = _estimating_runner(draft, schedule).initcode
        return runner_call(STAND_IN_SALT, initcode, _sent(draft))

    @override
    def call_gas_of(self, estimate: int, draft: Draft, index: int, schedule: GasSchedule) -> int:
        """The runner's own steps each at their bound and its other calls each at their gas, as
        the runner sent has them, so what they don't use is the call's in both."""
        runner = _estimating_runner(draft, schedule)
        signed = len(draft.submission.authorizations)
        runner_gas = runner_gas_within(
            runner.initcode,
            estimate,
            schedule,
            signed,
            draft.accounts.empty,
            sending=_sent(draft) > 0,
        )
        others = calls_need(draft.calls)
        return client_gas_within(
            runner_gas - runner.overhead_gas - (draft.deploy_gas or 0) - others
        )

    @override
    def call_part(self, client_gas: int) -> int:
        return call_need(client_gas)


@dataclass(frozen=True, slots=True)
class ByEntryPoint(DraftedProof):
    """Paid by the EntryPoint's own rules, for the relay's own handleOps of an op paying by its
    fees (`fee_payment`), the payee its beneficiary, sent as is: a handleOps that lands has paid
    the beneficiary the op's gas at its price (its call reverting or not), and one whose op
    fails its validation reverts, so doesn't land. A submission that isn't such a handleOps is
    a bug of ours: ProofError, never sent."""

    entry_point: EntryPoint
    fee_payment: FeePayment

    @override
    def paying(self, amount: int, base_fee: int) -> int:
        return self.fee_payment.paid_at(base_fee)

    @override
    def plan(self, chain_id: int, draft: Draft, schedule: GasSchedule) -> Plan:
        call = _handle_ops(draft).call
        if call.to != self.entry_point.address:
            raise ProofError(f"handleOps to {call.to}, not EntryPoint {self.entry_point.version}'s")
        if handle_ops_beneficiary(call.data) != draft.submission.payee:
            raise ProofError("a handleOps whose beneficiary isn't the payee")
        return _entry_point_plan(chain_id, draft, schedule, self)

    @override
    def revert_of(self, result: object) -> bytes | None:
        """handleOps answers nothing; an op it refuses is an error, a revert."""
        if result != "0x":
            raise ChainError(f"handleOps answered {result!r}")
        return None


@dataclass(frozen=True, slots=True)
class PaidByTransfer(QuotedProof):
    """Paid by its signed transfer, its calls and the transfer each sent as it is, by the
    concurrency account, to targets it may call directly (targets.direct): the calls first, in
    order, then the transfer, so a call may earn what pays for them. A transaction each, so a
    bundle of their own: none lands without the others. Each call is its transaction, so the
    EVM gives it its gas, and the relay's overhead is the transactions' own. The transfer moves
    exactly the amount to the payee, as its payer signed."""

    @override
    def paying(self, amount: int, base_fee: int) -> int:
        return amount

    @override
    def plan(self, chain_id: int, draft: Draft, schedule: GasSchedule) -> Plan:
        return _transfer_planned(chain_id, draft, schedule, self)

    @override
    def revert_of(self, result: object) -> bytes | None:
        """Each is a call to a target that is what it's listed as: whatever it answers, it was
        made."""
        data_of(result)
        return None

    @override
    def estimating(self, draft: Draft, index: int, schedule: GasSchedule) -> EncodedCall:
        """The call alone, as the chain stands: the calls before it aren't made first."""
        return draft.calls[index].call

    @override
    def call_gas_of(self, estimate: int, draft: Draft, index: int, schedule: GasSchedule) -> int:
        """What's left of `estimate` after the transaction's own gas and its authorizations',
        each at its bound, as the transaction sent has them."""
        signed = len(draft.submission.authorizations)
        call = draft.calls[index].call
        intrinsic = schedule.intrinsic(call.data, authorizations=signed).standard
        authorizations = schedule.authorizations_gas(draft.accounts.empty, signed)
        return max(0, estimate - intrinsic - authorizations)

    @override
    def call_part(self, client_gas: int) -> int:
        return client_gas


@dataclass(frozen=True, slots=True)
class CheckedByForwarder(Proof):
    """A sponsorship's payment is checked by the forwarder, called by the relay's last
    transaction: it lands only if the payee gained the amount, and its bundle with it. Nothing
    proof_of chooses: a sponsored account makes its own calls, so there's no call of the
    relay's to isolate."""

    @override
    def paying(self, amount: int, base_fee: int) -> int:
        return amount

    @override
    def revert_of(self, result: object) -> bytes | None:
        """Each of the relay's transactions did what it's sent for if it didn't fail: the
        forwarder reverts unless the payee gained the amount."""
        data_of(result)
        return None


class ProofError(BrunoError):
    """A submission its proof can't be of: a bug of ours, that submission's alone."""


@overload
def proof_of(
    chain_id: int,
    calls: Sequence[EncodedCall],
    *,
    transfer: EncodedCall | None = None,
    deployment: Deployment | None = None,
    fee_payment: None = None,
) -> QuotedProof: ...
@overload
def proof_of(
    chain_id: int,
    calls: Sequence[EncodedCall],
    *,
    transfer: EncodedCall | None = None,
    deployment: Deployment | None = None,
    fee_payment: FeePayment | None,
) -> DraftedProof: ...
def proof_of(
    chain_id: int,
    calls: Sequence[EncodedCall],
    *,
    transfer: EncodedCall | None = None,
    deployment: Deployment | None = None,
    fee_payment: FeePayment | None = None,
) -> DraftedProof:
    """How `calls` are sent on the chain `chain_id`, and their payment known, decided here
    alone, for a quote and a submission alike, by what they call and how they pay. An op paying
    by its fees (`fee_payment`) goes to its EntryPoint as it is, its handleOps the one call,
    which pays or reverts. Calls each to a target the concurrency account may call directly
    (targets.direct), paid by a signed transfer (`transfer`, its token called with its
    calldata) the account may send directly too, are sent as they are with their transfer, if
    they deploy no account (`deployment`). Anything else needs a runner: for isolation, a
    target not being listed, or to check what the payee gains."""
    if fee_payment is not None:
        if len(calls) != 1:
            raise ProofError(f"an op's handleOps is one call, and there are {len(calls)}")
        return ByEntryPoint(entry_point_at(calls[0].to), fee_payment)
    if (
        transfer is not None
        and deployment is None
        and all(direct(chain_id, call) for call in calls)
        and direct(chain_id, transfer)
    ):
        return PaidByTransfer()
    return MeasuredByRunner()


# a draft's proof: a draft of a quoted proof is a draft of any (it's frozen, its proof only read)
_P_co = TypeVar("_P_co", bound="DraftedProof", covariant=True, default="DraftedProof")


def bound_reads(chain_id: int, call: EncodedCall, block: str) -> list[Call]:
    """What's read at `block` to bound `call`'s gas, if it's to a function the relay can bound
    (targets.boundable, a token's transfer a key signs): its signer's code, and what its
    recipient holds of the token. Nothing for any other call."""
    parties = transfer_parties(call.data) if boundable(chain_id, call) else None
    if parties is None:
        return []
    signer, recipient = parties
    return [Call("eth_getCode", [signer, block]), balance_of_call(call.to, recipient, block)]


def call_bound(
    chain_id: int,
    call: EncodedCall,
    reads: Sequence[Answer],
    tokens: Mapping[ChecksumAddress, Erc20 | None],
    schedule: GasSchedule,
    authorities: Collection[ChecksumAddress],
) -> int | None:
    """The most gas `call` can take, from its `reads` (bound_reads), if the relay bounds it:
    None otherwise. Bounded is a fact of this call, not of its function: a transfer the token
    measured, signed by a key, so from an account with no code that none of the submission's
    `authorities` delegates (the token checks any other signature by the account's own code).
    Its bound is the measured transfer's with its balances' writes, its recipient's set or only
    changed by what it holds; one whose holding wasn't read is taken to hold nothing, the
    dearer."""
    if not reads:
        return None
    parties = transfer_parties(call.data) if boundable(chain_id, call) else None
    erc20 = tokens.get(call.to)
    if parties is None or erc20 is None or erc20.transfer_gas is None:
        return None
    signer, _ = parties
    code, held = reads
    if signer in authorities or has_code(result_of(code)):
        return None
    holds = held.error is None and word_of(result_of(held)) > 0
    return transfer_gas(erc20.transfer_gas, schedule, holds)


async def bounds_of(
    chain: Chain,
    calls: Sequence[EncodedCall],
    tokens: Mapping[ChecksumAddress, Erc20 | None],
    schedule: GasSchedule,
    authorities: Collection[ChecksumAddress],
) -> list[int | None]:
    """Each of `calls`' bound (call_bound), their reads made here in one batch, at the highest
    block seen: for a quote, which has no batch of its own to put them in. Nothing is read
    where none may be bounded."""
    reads = [bound_reads(chain.chain_id, call, chain.seen_tag) for call in calls]
    asked = [read for its in reads for read in its]
    answers = iter(await chain.batch(asked) if asked else ())
    return [
        call_bound(
            chain.chain_id, call, [next(answers) for _ in its], tokens, schedule, authorities
        )
        for call, its in zip(calls, reads, strict=True)
    ]


def planned_call(index: int, given: SubmittedCall, bound: int | None) -> PlannedCall:
    """A submission's call, the `index`th, as it's planned: given its `bound`, if the relay
    bounds it (whatever gas it states), else the gas it states. InvalidSubmissionError for one
    the relay doesn't bound that states none."""
    if bound is not None:
        return PlannedCall(given.call, call_need(bound), bounded=True)
    if given.gas is None:
        raise InvalidSubmissionError(f"calls[{index}].gas", refused(Refusal.CALL_NO_GAS))
    return PlannedCall(given.call, given.gas)


@dataclass(frozen=True, slots=True)
class Draft(Generic[_P_co]):
    """What a plan is made from: its `submission` (a quote's stand-in, paying the most it can,
    its transfer only as `transfer` here), its `proof`, and what's read of its call's accounts
    (`accounts`, its `authorities` among them) and its token (`erc20`, None for ETH); its
    `calls` as planned, each with the gas it's given; the
    payment's transfer, if it's paid by one, its gas bounded or None (estimated); the account
    deployed first, if any, its factory call's gas known (`deploy_gas`, a quote's, estimated
    apart) or None (estimated with the rest); `prepare` to call a wrapped signature's factory
    however its payer's code reads (`payer_has_code`)."""

    submission: Submission
    proof: _P_co
    authorities: tuple[ChecksumAddress, ...]
    accounts: CallAccounts
    erc20: Erc20 | None
    calls: tuple[PlannedCall, ...]
    transfer: Transfer | None = None
    deployment: Deployment | None = None
    deploy_gas: int | None = None
    payer_has_code: bool = False
    prepare: bool = False
    salt: bytes = STAND_IN_SALT


async def make_plan(
    chain: Chain,
    taken: Submission | Sponsorship,
    proof: Proof,
    schedule: GasSchedule,
    tokens: Mapping[ChecksumAddress, Erc20 | None],
    sender: ChecksumAddress,
    payee_holds: Collection[ChecksumAddress] = (),
    *,
    payee_new: bool = False,
    salt: bytes = STAND_IN_SALT,
) -> Plan:
    """What was `taken`, its transactions by its `proof`, and their gas limits under
    `schedule`, paying in its token as `tokens` reads it, sent by `sender`; a transfer's gas
    bounded knowing which tokens the payee holds some of (`payee_holds`), and whether ETH
    forwarded to it would create its account (`payee_new`)."""
    (planned,) = await make_plans(
        chain,
        [taken],
        [proof],
        schedule,
        tokens,
        sender,
        payee_holds,
        payee_new=payee_new,
        salts=[salt],
    )
    return plan_of(planned)


# a submission's plan, or why it has none: the client's mistake (InvalidSubmissionError), its
# simulation failing, a node's failure (JsonRpcError, ChainError), or a bug of ours, logged
type Planned = Plan | Exception


def plan_of(planned: Planned) -> Plan:
    """The plan that was made: why not, raised."""
    if isinstance(planned, Exception):
        raise planned
    return planned


async def make_plans(
    chain: Chain,
    taken: Sequence[Submission | Sponsorship],
    proofs: Sequence[Proof],
    schedule: GasSchedule,
    tokens: Mapping[ChecksumAddress, Erc20 | None],
    sender: ChecksumAddress,
    payee_holds: Collection[ChecksumAddress] = (),
    *,
    payee_new: bool = False,
    salts: Sequence[bytes] | None = None,
) -> list[Planned]:
    """Each one taken, its transactions by its proof (`proofs`, one each), and their gas limits
    under `schedule`, sent by `sender`, their reads in one batch: a submission's call's
    accounts and its payer's code, then it drafted (drafted) and planned (plan_drafts); a
    sponsorship's account, then it planned as it is (sandwich). Each its plan, or why not.
    `salts`: each one's runner's salt, if it's sent through one (deployer.runner_salt); a
    stand-in's without them, for a plan that's only priced or simulated."""
    authorities: list[tuple[ChecksumAddress, ...] | InvalidSubmissionError] = []
    for one in taken:
        try:
            authorities.append(authorities_of(one.authorizations))
        except InvalidSubmissionError as e:
            authorities.append(e)
    # at the highest block seen, as the estimates after them are
    at = chain.seen_tag
    calls: list[Call] = []
    firsts: list[int] = []
    for one, of in zip(taken, authorities, strict=True):
        firsts.append(len(calls))
        if isinstance(of, InvalidSubmissionError):
            continue
        if isinstance(one, Sponsorship):
            calls += authority_reads(one.account, at)
            continue
        calls += account_reads([each.call.to for each in one.calls], of, at)
        if one.transfer is not None:
            calls.append(Call("eth_getCode", [one.transfer.payer, at]))
        for each in one.calls:
            calls += bound_reads(chain.chain_id, each.call, at)
    answers = await chain.batch(calls)
    planned: dict[int, Planned] = {}
    drafts: list[Draft | Exception] = []
    drafted_at: list[int] = []
    for index, (one, proof, of, first) in enumerate(
        zip(taken, proofs, authorities, firsts, strict=True)
    ):
        if isinstance(of, InvalidSubmissionError):
            planned[index] = of
            continue
        try:
            if isinstance(one, Sponsorship):
                planned[index] = _sandwiched(
                    chain.chain_id,
                    one,
                    proof,
                    answers[first : first + 3],
                    schedule,
                    _token_of(one.token, tokens),
                    payee_holds=one.token in payee_holds,
                    payee_new=payee_new,
                )
                continue
            draft: Draft | Exception = replace(
                _draft_of(
                    chain.chain_id,
                    one,
                    proof,
                    of,
                    answers[first:],
                    tokens,
                    schedule,
                    one.token in payee_holds,
                ),
                salt=STAND_IN_SALT if salts is None else salts[index],
            )
        except (InvalidSubmissionError, JsonRpcError, ChainError) as e:
            draft = e
        except Exception as e:  # a bug of ours: this one's alone, the rest planned
            logger.exception("drafting 0x%s: internal error", one.id.hex())
            draft = e
        drafts.append(draft)
        drafted_at.append(index)
    plans = await plan_drafts(chain, drafts, schedule, sender)
    planned |= dict(zip(drafted_at, plans, strict=True))
    return [planned[index] for index in range(len(taken))]


def _sandwiched(
    chain_id: int,
    sponsorship: Sponsorship,
    proof: Proof,
    reads: Sequence[Answer],
    schedule: GasSchedule,
    erc20: Erc20 | None,
    *,
    payee_holds: bool,
    payee_new: bool,
) -> Plan:
    """A sponsorship's plan from its account's `reads` (its balance, nonce and code):
    InvalidSubmissionError for an account with code given no gas for it, the relay's first
    transaction running that code whatever else it carries."""
    if not isinstance(proof, CheckedByForwarder):
        raise ProofError(f"a sponsorship's payment is checked by the forwarder, not {proof!r}")
    if sponsorship.gas == 0 and has_code(result_of(reads[2])):
        raise InvalidSubmissionError("gas", refused(Refusal.SPONSORED_NO_GAS))
    return sandwich(
        chain_id,
        sponsorship,
        schedule,
        erc20,
        account_empty=empty_among(reads) == 1,
        payee_holds=payee_holds,
        payee_new=payee_new,
    )


def _draft_of(
    chain_id: int,
    submission: Submission,
    proof: Proof,
    authorities: tuple[ChecksumAddress, ...],
    reads: Sequence[Answer],
    tokens: Mapping[ChecksumAddress, Erc20 | None],
    schedule: GasSchedule,
    payee_holds: bool,
) -> Draft:
    """A submission's draft from its `reads`, which start a batch's rest: its calls' accounts,
    then its payer's code, if it's paid by a transfer, then each call's own, if the relay may
    bound it (bound_reads)."""
    if not isinstance(proof, DraftedProof):
        raise ProofError(f"a submission's proof plans it, and {proof!r} plans none")
    targets = [given.call.to for given in submission.calls]
    at = len(targets) + 3 * len(authorities)
    accounts = accounts_of(targets, authorities, reads[:at])
    payer_code = None
    if submission.transfer is not None:
        payer_code = has_code(result_of(reads[at]))
        at += 1
    calls: list[PlannedCall] = []
    for index, given in enumerate(submission.calls):
        its = len(bound_reads(chain_id, given.call, ""))
        bound = call_bound(
            chain_id, given.call, reads[at : at + its], tokens, schedule, authorities
        )
        calls.append(planned_call(index, given, bound))
        at += its
    return drafted(
        submission,
        proof,
        authorities,
        accounts,
        _token_of(submission.token, tokens),
        schedule,
        tuple(calls),
        payer_has_code=payer_code is True,
        payee_holds=payee_holds,
    )


def drafted(
    submission: Submission,
    proof: DraftedProof,
    authorities: tuple[ChecksumAddress, ...],
    accounts: CallAccounts,
    erc20: Erc20 | None,
    schedule: GasSchedule,
    calls: tuple[PlannedCall, ...],
    *,
    payer_has_code: bool,
    payee_holds: bool,
) -> Draft:
    """A signed submission's draft, of its `calls` as planned: its transfer, if any, its gas
    bounded for a key's signature (from its token's measurement, knowing whether the payee
    holds some of it, `payee_holds`), estimated for a contract's (a payer with code or one of
    the submission's authorities, or a signature that isn't a key's), whose check is its own; its deployment, if any, estimated. A wrapped signature from a payer with
    code is drafted without its deployment, to be prepared only if it reverts without it
    (ERC-6492)."""
    deployment = submission.to_deploy
    transfer = None
    signed = submission.transfer
    if signed is not None:
        if erc20 is None or erc20.transfer_gas is None:
            raise InvalidSubmissionError(
                "payment",
                refused(Refusal.NO_EIP3009, token=submission.token),
            )
        contract_payer = (
            not signed.ecdsa
            or payer_has_code
            or signed.deployment is not None
            or signed.payer in authorities
        )
        gas = None if contract_payer else transfer_gas(erc20.transfer_gas, schedule, payee_holds)
        transfer = Transfer(signed.calldata(), gas)
        if signed.deployment is not None and payer_has_code:
            deployment = submission.deployment
    return Draft(
        submission,
        proof,
        authorities,
        accounts,
        erc20,
        calls,
        transfer,
        deployment,
        payer_has_code=payer_has_code,
    )


async def plan_drafts(
    chain: Chain,
    drafts: Sequence[Draft | Exception],
    schedule: GasSchedule,
    sender: ChecksumAddress,
) -> list[Planned]:
    """Each draft planned by its proof, under `schedule`, sent by `sender`, a draft that's an
    error left as it is: each its plan, or why not. A plan whose gas a step leaves to an
    estimate (code the client chooses) has it from one eth_estimateGas of its transaction as it
    will be sent, the estimates in one batch: an estimate that reverts is the submission's
    simulation failing. Quotes, the bundler's estimates and submissions are all planned here."""
    plans: list[Planned] = []
    estimating: list[tuple[int, Draft, Plan]] = []
    for draft in drafts:
        if isinstance(draft, Exception):
            plans.append(draft)
            continue
        try:
            plan = draft.proof.plan(chain.chain_id, draft, schedule)
        except (InvalidSubmissionError, JsonRpcError, ChainError) as e:
            plans.append(e)
            continue
        except Exception as e:  # a bug of ours: this submission's alone, the rest planned
            logger.exception("planning 0x%s: internal error", draft.submission.id.hex())
            plans.append(e)
            continue
        if plan.estimated:
            estimating.append((len(plans), draft, plan))
        plans.append(plan)
    await _estimate(chain, estimating, plans, schedule, sender)
    return plans


def _token_of(
    token: ChecksumAddress, tokens: Mapping[ChecksumAddress, Erc20 | None]
) -> Erc20 | None:
    """A payment's `token` as `tokens` measures it (None for ETH); refused if it's
    suspended."""
    if token not in tokens:
        raise InvalidSubmissionError("payment", refused(Refusal.TOKEN_SUSPENDED, token=token))
    return tokens[token]


def check_gas(
    schedule: GasSchedule, erc20: Erc20 | None, *, payee_holds: bool, payee_new: bool
) -> int:
    """The most gas the forwarder's call takes, the transaction's own aside. For ETH, the
    forwarder's own, to a payee whose code uses none (`payee_new`: the ETH creates its
    account). For a token, the forwarder's own and what it leaves the token: three balanceOf
    reads and its transfer, measured at zero value and the balances' writes added
    (`payee_holds`: the payee's is changed, not set), with a 63rd more, since each is a call
    below the transaction, given all but a 64th of what's left (EIP-150); a 63rd of them all
    leaves each call its own, whichever comes last."""
    if erc20 is None:
        return eth_gas(schedule, new_payee=payee_new)
    if erc20.forward_gas is None:
        raise InvalidSubmissionError("payment", refused(Refusal.NOT_FORWARDED, token=erc20.address))
    calls = 3 * erc20.balance_gas + transfer_gas(erc20.forward_gas, schedule, payee_holds)
    return token_gas(schedule) + call_need(calls)


def _dearest(data: bytes) -> bytes:
    """Calldata of `data`'s length at its dearest: no byte zero."""
    return b"\xff" * len(data)


def funding_overhead(
    schedule: GasSchedule, data: bytes, authorizations: int, sending: bool, account_empty: bool
) -> int:
    """What a sponsorship's first transaction takes beside the gas its call is given: its
    intrinsic gas, carrying `data` and `authorizations`, `sending` ETH or not, and what
    delegating the account costs, or creating it by the ETH sent, if it's empty. The data is
    priced as if it had no zero byte: an account's call may hold what it pays, so what it's
    charged mustn't change with that."""
    intrinsic = schedule.intrinsic(_dearest(data), value=sending, authorizations=authorizations)
    if authorizations:
        creating = schedule.authorizations_gas(1 if account_empty else 0, authorizations)
    else:
        creating = schedule.tx_new_account if sending and account_empty else 0
    return intrinsic.standard + creating


def sandwich(
    chain_id: int,
    sponsorship: Sponsorship,
    schedule: GasSchedule,
    erc20: Erc20 | None,
    *,
    account_empty: bool,
    payee_holds: bool,
    payee_new: bool,
) -> Plan:
    """A sponsorship's transactions. The relay's first, to the account: the ETH its
    transactions need (Sponsorship.funding), its authorizations, and its call, given the
    sponsorship's gas with the transaction's own, and what creating or delegating the account
    costs (`account_empty`: it has no balance, nonce or code yet). The account's own, as
    signed. The relay's last: the forwarder's call for the amount (check_gas), its calldata
    priced as if the amount's word had no zero byte, so what a sponsorship is charged doesn't
    change with what it pays. With no transaction of the account's the first sends no ETH: it's
    only the call."""
    proof = CheckedByForwarder()
    authorizations = len(sponsorship.authorizations)
    data = b"" if sponsorship.call is None else sponsorship.call.data
    funding = sponsorship.funding
    funded = EncodedCall(sponsorship.account, data, funding)
    own = funding_overhead(schedule, data, authorizations, funding > 0, account_empty)
    funding_gas = schedule.intrinsic(
        _dearest(data), value=funding > 0, authorizations=authorizations
    )
    first = Step(
        funded,
        proof,
        max(own + sponsorship.gas, funding_gas.floor),
        unsigned_size(chain_id, funded.to, data, funding, authorizations),
        sponsorship.authorizations,
    )
    check = forwarding(sponsorship.payee, sponsorship.token, sponsorship.amount)
    widest = forwarding(sponsorship.payee, sponsorship.token, _WIDEST_AMOUNT)
    checking = schedule.intrinsic(widest.data)
    executing = check_gas(schedule, erc20, payee_holds=payee_holds, payee_new=payee_new)
    last = Step(
        check,
        proof,
        max(checking.standard + executing, checking.floor),
        unsigned_size(chain_id, check.to, check.data),
        (),
    )
    return Plan((first, *(SignedStep(t) for t in sponsorship.transactions), last))


def _runner_planned(chain_id: int, draft: Draft, schedule: GasSchedule, proof: Proof) -> Plan:
    """The draft's runner's transaction, and its gas limit from the gas schedule, its calls each
    given their gas: for one with a step whose gas isn't known (code the client chooses), only
    a placeholder until it's estimated."""
    submission = draft.submission
    payment = Payment(draft.erc20, submission.payee, submission.amount, draft.transfer)
    try:
        runner = in_call_runner(
            draft.calls,
            payment,
            schedule,
            draft.accounts.delegated,
            deployment=draft.deployment,
            prepare=draft.prepare,
        )
    except RunnerError as e:
        raise InvalidSubmissionError("calls", refused(Refusal.RUNNER_UNBUILT, why=str(e))) from e
    authorizations = len(draft.authorities)
    gas_limit = transaction_gas(
        runner.initcode,
        runner.frame_gas(draft.calls) + (draft.deploy_gas or 0),
        schedule,
        authorizations,
        draft.accounts.empty,
        sending=_sent(draft) > 0,
    )
    transaction = runner_call(draft.salt, runner.initcode, _sent(draft))
    size = unsigned_size(
        chain_id, transaction.to, transaction.data, transaction.value, authorizations
    )
    estimated = (draft.transfer is not None and draft.transfer.gas is None) or (
        draft.deployment is not None and draft.deploy_gas is None
    )
    step = Step(transaction, proof, gas_limit, size, submission.authorizations, estimated)
    return Plan((step,))


def _sent(draft: Draft) -> int:
    """The ETH the draft's calls send, together: what its runner's transaction carries."""
    return sum(planned.call.value for planned in draft.calls)


def _estimating_runner(draft: Draft, schedule: GasSchedule) -> Runner:
    """The runner whose estimate gives the draft's call that states no gas its gas: its
    deployment, if any, then the calls, without its payment's transfer and check
    (estimating_runner)."""
    submission = draft.submission
    payment = Payment(draft.erc20, submission.payee, submission.amount)
    try:
        return estimating_runner(
            draft.calls,
            payment,
            schedule,
            draft.accounts.delegated,
            deployment=draft.deployment,
        )
    except RunnerError as e:
        raise InvalidSubmissionError("calls", refused(Refusal.RUNNER_UNBUILT, why=str(e))) from e


def _handle_ops(draft: Draft) -> PlannedCall:
    """An op's draft's one call, its handleOps: ProofError if it's drafted with any other
    number."""
    if len(draft.calls) != 1:
        raise ProofError(f"an op's handleOps is one call, and there are {len(draft.calls)}")
    return draft.calls[0]


def _entry_point_plan(chain_id: int, draft: Draft, schedule: GasSchedule, proof: Proof) -> Plan:
    """The draft's call, a handleOps, sent as is: its transaction's gas limit from the gas
    schedule, the call given the submission's gas (handleOps' own gas, a node's estimate from its
    beneficiary, which the EntryPoint pays last, less its intrinsic gas), with its
    authorizations, those of empty accounts among them; and its most bytes unsigned. Sent from a
    concurrency account, the beneficiary is cold when it's paid: its access as estimated was
    warm."""
    planned = _handle_ops(draft)
    call = planned.call
    authorizations = len(draft.authorities)
    intrinsic = schedule.intrinsic(call.data, authorizations=authorizations)
    beneficiary = schedule.cold_account_access - WARM_ACCOUNT_ACCESS
    execution = (
        planned.gas
        + beneficiary
        + schedule.authorizations_gas(draft.accounts.empty, authorizations)
    )
    gas_limit = max(intrinsic.standard + execution, intrinsic.floor)
    size = unsigned_size(chain_id, call.to, call.data, authorizations=authorizations)
    return Plan((Step(call, proof, gas_limit, size, draft.submission.authorizations),))


def _transfer_planned(chain_id: int, draft: Draft, schedule: GasSchedule, proof: Proof) -> Plan:
    """The draft's calls, in order, then its transfer, each a transaction as it is. A call's gas
    limit is its gas with the transaction's own; the first carries the authorizations, and
    their gas. The transfer's is its bound with the transaction's own, and a 63rd of the bound
    more: the bound is a zero-value transfer's gas, measured, with the balances' writes added,
    and what a token spends it spends below its own frame (a proxy's implementation), given all
    but a 64th of what's left (EIP-150). A runner's transfer needs none: the runner reads the
    payee's balance first, which leaves the transfer that much warm. One whose gas isn't known
    (a contract payer's) is estimated, alone, as the chain stands before the calls."""
    submission = draft.submission
    if draft.transfer is None:
        raise ProofError("paid by a transfer, and drafted without one")
    made: list[Step] = []
    for index, planned in enumerate(draft.calls):
        call = planned.call
        # the authorizations go with the first call: every later one finds them applied
        carried = submission.authorizations if index == 0 else ()
        authorizations = len(draft.authorities) if index == 0 else 0
        empty = draft.accounts.empty if index == 0 else 0
        calling = schedule.intrinsic(call.data, authorizations=authorizations)
        execution = planned.gas + schedule.authorizations_gas(empty, authorizations)
        made.append(
            Step(
                call,
                proof,
                max(calling.standard + execution, calling.floor),
                unsigned_size(chain_id, call.to, call.data, authorizations=authorizations),
                carried,
            )
        )
    transfer = EncodedCall(submission.token, draft.transfer.calldata)
    transferring = schedule.intrinsic(transfer.data)
    paid = Step(
        transfer,
        proof,
        max(transferring.standard + call_need(draft.transfer.gas or 0), transferring.floor),
        unsigned_size(chain_id, transfer.to, transfer.data),
        (),
        estimated=draft.transfer.gas is None,
    )
    return Plan((*made, paid))


async def _estimate(
    chain: Chain,
    drafts: Sequence[tuple[int, Draft, Plan]],
    plans: list[Planned],
    schedule: GasSchedule,
    sender: ChecksumAddress,
) -> None:
    """Each draft's plan, put in `plans` at its index, each step to be estimated given its gas
    limit by one eth_estimateGas of its transaction, in one batch: one that reverts is the
    submission's simulation failing, unless a wrapped signature's payer may need preparing,
    which is then estimated again, prepared."""
    if not drafts:
        return
    at = chain.seen_tag
    calls = [
        _estimate_call(transaction_json(step, sender), sender, step.transaction.value, at)
        for _, _, plan in drafts
        for step in plan.own
        if step.estimated
    ]
    answers = iter(await chain.batch(calls))
    again: list[tuple[int, Draft, Plan]] = []
    for index, draft, plan in drafts:
        its = [next(answers) for step in plan.own if step.estimated]
        try:
            again += _estimated(chain.chain_id, index, draft, plan, its, plans, schedule)
        except ChainError as e:  # a node's malformed answer: this submission's alone
            plans[index] = e
        except Exception as e:  # a bug of ours: this submission's alone
            logger.exception("estimating 0x%s: internal error", draft.submission.id.hex())
            plans[index] = e
    await _estimate(chain, again, plans, schedule, sender)


def _estimated(
    chain_id: int,
    index: int,
    draft: Draft,
    plan: Plan,
    answers: Sequence[Answer],
    plans: list[Planned],
    schedule: GasSchedule,
) -> list[tuple[int, Draft, Plan]]:
    """`draft`'s estimates `answers`, one for each of its plan's steps to be estimated, read
    into its plan, at `index` of `plans`: their gas limits, or why it has none; or, its payer's
    wrapped signature to be prepared first, it drafted again (returned)."""
    failed = next((a.error for a in answers if a.error is not None), None)
    if failed is None:
        estimates = iter(quantity(result_of(a)) for a in answers)
        steps = tuple(
            replace(step, gas_limit=next(estimates))
            if isinstance(step, Step) and step.estimated
            else step
            for step in plan.steps
        )
        plans[index] = replace(plan, steps=steps)
        return []
    if failed.code != REVERTED:
        plans[index] = failed
        return []
    signed = draft.submission.transfer
    if (
        not draft.prepare
        and signed is not None
        and signed.deployment is not None
        and draft.payer_has_code
    ):
        prepared = replace(draft, prepare=True, deployment=draft.submission.to_deploy)
        return [(index, prepared, prepared.proof.plan(chain_id, prepared, schedule))]
    plans[index] = SimulationFailedError(revert_data(failed))
    return []


def transaction_json(
    step: Step, sender: ChecksumAddress, gas_limit: int | None = None
) -> dict[str, object]:
    """`step`'s transaction, from `sender`, with the authorizations it carries, as eth_call and
    eth_estimateGas take it: no fees, so no balance is needed; at `gas_limit`, unless it's being
    estimated (a limit would cap the estimate's search)."""
    return _call_json(step.transaction, sender, step.authorizations, gas_limit)


def signed_json(transaction: Decoded) -> dict[str, object]:
    """A transaction another account signed, as eth_simulateV1 takes a call: from its sender,
    at its own nonce, gas and fees, with its access list and authorizations."""
    signed = transaction.signed
    tx: dict[str, object] = {
        "from": signed.sender,
        "data": "0x" + transaction.data.hex(),
        "value": hex(signed.value),
        "gas": hex(signed.gas),
        "nonce": hex(signed.nonce),
        "maxFeePerGas": hex(signed.max_fee_per_gas),
        "maxPriorityFeePerGas": hex(transaction.max_priority_fee_per_gas),
    }
    if transaction.to is not None:
        tx["to"] = transaction.to
    if transaction.access_list:
        tx["accessList"] = [
            {"address": a.address, "storageKeys": ["0x" + key.hex() for key in a.storage_keys]}
            for a in transaction.access_list
        ]
    if transaction.authorizations:
        tx["authorizationList"] = [authorization_json(a) for a in transaction.authorizations]
    return tx


def _estimate_call(
    transaction: Mapping[str, object], sender: ChecksumAddress, value: int, block: str
) -> Call:
    """eth_estimateGas of `transaction` at `block`. One sending ETH is estimated with its
    `sender` holding that much (a state override), as its simulation is: the account it's
    planned as sent from holds nothing of its own to send."""
    if not value:
        return Call("eth_estimateGas", [transaction, block])
    return Call("eth_estimateGas", [transaction, block, {sender: {"balance": hex(value)}}])


def _call_json(
    transaction: EncodedCall,
    sender: ChecksumAddress,
    authorizations: Sequence[SignedSetCodeAuthorization],
    gas_limit: int | None = None,
) -> dict[str, object]:
    """`transaction`, from `sender`, with `authorizations`, as eth_call and eth_estimateGas
    take it (transaction_json)."""
    data = "0x" + transaction.data.hex()
    tx: dict[str, object] = {"from": sender, "to": transaction.to, "data": data}
    if transaction.value:
        tx["value"] = hex(transaction.value)
    if gas_limit is not None:
        tx["gas"] = hex(gas_limit)
    if authorizations:
        tx["authorizationList"] = [authorization_json(a) for a in authorizations]
    return tx


async def call_gas(
    chain: Chain,
    draft: Draft[QuotedProof],
    index: int,
    schedule: GasSchedule,
    sender: ChecksumAddress,
) -> int:
    """The gas `draft`'s `index`th call needs as it will run, by its proof: one eth_estimateGas of the
    transaction its proof estimates it by (a runner's, without its payment's transfer and
    check, after its deployment), in `sender`'s transaction with the submission's
    authorizations, the most gas its call can be given in a transaction of that estimate.
    SimulationFailedError if the call reverts."""
    transaction = draft.proof.estimating(draft, index, schedule)
    tx = _call_json(transaction, sender, draft.submission.authorizations)
    (answer,) = await chain.batch([_estimate_call(tx, sender, transaction.value, "latest")])
    if answer.error is not None:
        if answer.error.code == REVERTED:
            raise SimulationFailedError(revert_data(answer.error))
        raise answer.error
    return draft.proof.call_gas_of(quantity(result_of(answer)), draft, index, schedule)
