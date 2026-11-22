"""What the relay knows of a chain by measuring it, never by assuming it: the nodes' chain id,
the fork in force, whether the deployer is there, whether it's an OP-stack chain (its fee
oracle's code) whose fees beyond gas our formulas give, and each token's gas, decimals and
symbol, and the payee's holdings of each x402 token. One batch (and, on an OP-stack chain, one
more at its block), taken again once it's an hour old.

Anything that fails takes out only what depends on it: a token that can't be measured is
suspended, the rest served; a chain whose nodes are another chain, whose fork no schedule
matches, or whose deployer is missing is unavailable, nothing sent on it."""

import asyncio
import logging
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

import httpx
from eth_typing import ChecksumAddress

from . import datafee, forks
from .chain import (
    Block,
    Chain,
    ChainError,
    Latest,
    code_of,
    data_of,
    empty_call,
    has_code,
    quantity,
    simulating_call,
    word_of,
)
from .common import BrunoError
from .config import ChainConfig, Eip712Domain
from .deployer import DEPLOYER, RUNTIME
from .eip3009 import measured as measured_transfer
from .eip3009 import measuring as measuring_transfer
from .evm import NATIVE_ASSET
from .forks import GasSchedule
from .jsonrpc import Answer, Call, JsonRpcError, result_of
from .maintenance import AccountsStatus
from .refusals import Category, Refusal, Refused, Wording, message, refused
from .runner import Erc20
from .tokens import balance_of_call, decimals_call, erc20, symbol_call, symbol_of
from .tokens import measuring as measuring_balance
from .units import Millis, Seconds

logger = logging.getLogger(__name__)

# how soon measurements that couldn't be taken again are tried again, the last kept meanwhile
MEASURE_RETRY = Seconds(60)
# how many of the last bugs of ours in a submission's work the root lists
INTERNAL_ERRORS_KEPT = 5
# why a chain is unavailable before its first measurement
_UNMEASURED = refused(Refusal.NOT_MEASURED)


def next_step(category: Category) -> str:
    """What a client can do about a refusal in `category`."""
    match category:
        case Category.RETRY:
            return message(Wording.NEXT_RETRY)
        case Category.REQUEST:
            return message(Wording.NEXT_REQUEST)
        case Category.OPERATOR:
            return message(Wording.NEXT_OPERATOR)


@dataclass(frozen=True, slots=True)
class Unavailable:
    """Why a chain can't be served: the refusal, and what its template says of it."""

    refused: Refused


class UnavailableError(BrunoError):
    """The chain can't be served now: its nodes, its fork or its deployer aren't what the relay
    needs (measurements), or it hasn't been measured yet; why (`reason`), and its details for
    people."""

    def __init__(self, refused: Refused) -> None:
        super().__init__(f"{refused}. {next_step(refused.refusal.category)}")
        self.refused = refused

    @classmethod
    def of(cls, unavailable: Unavailable) -> UnavailableError:
        return cls(unavailable.refused)


# where the tokens' answers start in measure's batch: after the chain id, the block, the empty
# call, the deployer's code and the fee oracle's; the batch's last answer is eth_simulateV1's
_TOKENS = 5


@dataclass(frozen=True, slots=True)
class Measurements:
    """A chain as measured at `block`: its fork's schedule, each
    token's measured gas (None for ETH, read with BALANCE; an x402 token's transfer gas with
    it), decimals and symbol, which x402 tokens the payee holds some of; each token suspended
    and why, and each taken without x402 and why; whether it's an OP-stack chain (a data fee
    read with each block); and why the chain is unavailable, if it is."""

    block: int
    schedule: GasSchedule | None
    tokens: Mapping[ChecksumAddress, Erc20 | None]
    decimals: Mapping[ChecksumAddress, int]
    symbols: Mapping[ChecksumAddress, str]
    payee_holds: frozenset[ChecksumAddress] = frozenset()
    suspended: Mapping[ChecksumAddress, str] = field(default_factory=dict)
    without_x402: Mapping[ChecksumAddress, str] = field(default_factory=dict)
    op_stack: bool = False
    unavailable: Unavailable | None = None

    def problems(self) -> tuple[str, ...]:
        """What's wrong, each a line: none if all's well."""
        lines = [] if self.unavailable is None else [f"unavailable: {self.unavailable.refused}"]
        lines += [f"{t} suspended: {why}" for t, why in self.suspended.items()]
        lines += [f"{t} not over x402: {why}" for t, why in self.without_x402.items()]
        return tuple(lines)

    def holding(self, holdings: Mapping[ChecksumAddress, int]) -> Measurements:
        """These measurements, the payee's holdings of each measured token as `holdings` says."""
        holds = set(self.payee_holds)
        for token, amount in holdings.items():
            if amount > 0:
                holds.add(token)
            else:
                holds.discard(token)
        return replace(self, payee_holds=frozenset(holds))


async def measure(chain: Chain, config: ChainConfig, payee: ChecksumAddress) -> Measurements:
    """The chain measured, in one batch: raise only if the nodes don't answer it at all."""
    erc20s = [t.address for t in config.tokens if t.address != NATIVE_ASSET]
    domains: dict[ChecksumAddress, Eip712Domain] = {
        t.address: t.eip3009 for t in config.tokens if t.eip3009 is not None and t.address in erc20s
    }
    transfers = [measuring_transfer(d, chain.chain_id, t, payee) for t, d in domains.items()]
    answers = await chain.batch(
        [
            Call("eth_chainId", []),
            Call("eth_getBlockByNumber", ["latest", False]),
            empty_call(),
            Call("eth_getCode", [DEPLOYER, "latest"]),
            Call("eth_getCode", [datafee.GAS_PRICE_ORACLE, "latest"]),
            *(measuring_balance(t, payee) for t in erc20s),
            *(decimals_call(t) for t in erc20s),
            *(symbol_call(t) for t in erc20s),
            *(call for call, _ in transfers),
            *(balance_of_call(t, payee) for t in domains),
            simulating_call(),
        ]
    )
    # an answer that isn't one is the nodes failing: each read raises ChainError
    nodes = quantity(result_of(answers[0]))
    block = Block.of(result_of(answers[1])).number
    empty_call_gas = quantity(result_of(answers[2]))
    deployer = code_of(result_of(answers[3]))
    op_stack = has_code(result_of(answers[4]))
    schedule = forks.schedule_of(empty_call_gas)
    unavailable = None
    if nodes != chain.chain_id:
        unavailable = Unavailable(refused(Refusal.WRONG_CHAIN, nodes=nodes, chain=chain.chain_id))
    elif schedule is None:
        unavailable = Unavailable(refused(Refusal.UNKNOWN_FORK, gas=empty_call_gas))
    elif deployer != RUNTIME:
        unavailable = Unavailable(refused(Refusal.NO_DEPLOYER, deployer=DEPLOYER))
    elif not _simulates(chain, answers[-1]):
        unavailable = Unavailable(refused(Refusal.NO_SIMULATION))
    elif op_stack and not await _data_fee_known(chain, block):
        unavailable = Unavailable(refused(Refusal.UNKNOWN_FEE_FORMULA))
    tokens: dict[ChecksumAddress, Erc20 | None] = {NATIVE_ASSET: None}
    decimals = {NATIVE_ASSET: 18}
    symbols = {NATIVE_ASSET: "ETH"}
    suspended: dict[ChecksumAddress, str] = {}
    without_x402: dict[ChecksumAddress, str] = {}
    payee_holds: set[ChecksumAddress] = set()
    if schedule is not None:
        n = len(erc20s)
        for i, token in enumerate(erc20s):
            try:
                tokens[token] = erc20(token, payee, _int(answers[_TOKENS + i]), schedule)
                decimals[token] = _int(answers[_TOKENS + n + i])
                symbols[token] = symbol_of(data_of(result_of(answers[_TOKENS + 2 * n + i])))
            except (JsonRpcError, ChainError, ValueError) as e:
                logger.warning("chain %d: %s unmeasured: %s", chain.chain_id, token, e)
                suspended[token] = "its balanceOf, decimals() or symbol() didn't measure"
                tokens.pop(token, None)
        at = _TOKENS + 3 * n
        for i, (token, (_, data)) in enumerate(zip(domains, transfers, strict=True)):
            current = tokens.get(token)
            if current is None:
                continue
            try:
                gas = measured_transfer(_int(answers[at + i]), data, schedule)
                tokens[token] = replace(current, transfer_gas=gas)
                if _int(answers[at + len(domains) + i]) > 0:
                    payee_holds.add(token)
            except (JsonRpcError, ValueError) as e:
                # taken without x402: only its transfer failed (a domain not the token's, say)
                logger.warning("chain %d: %s's transfer unmeasured: %s", chain.chain_id, token, e)
                without_x402[token] = "a transfer signed for its eip3009 domain didn't measure"
    return Measurements(
        block=block,
        schedule=schedule,
        tokens=tokens,
        decimals=decimals,
        symbols=symbols,
        payee_holds=frozenset(payee_holds),
        suspended=suspended,
        without_x402=without_x402,
        op_stack=op_stack,
        unavailable=unavailable,
    )


def _simulates(chain: Chain, answer: Answer) -> bool:
    """Whether the nodes answered measure's eth_simulateV1: a block's results, a list. A node's
    refusal of it is only logged."""
    try:
        return isinstance(result_of(answer), list)
    except JsonRpcError as e:
        logger.warning("chain %d: eth_simulateV1 unanswered: %s", chain.chain_id, e)
        return False


async def _data_fee_known(chain: Chain, block: int) -> bool:
    """Whether, at `block`, the chain's fee oracle answers what datafee's formulas give from the
    inputs read at the same block: one batch, its reads all of that block."""
    at = hex(block)
    answers = await chain.batch([*datafee.reading(at), *datafee.checking(at)])
    inputs = len(datafee.reading(at))
    try:
        values = [word_of(result_of(a)) for a in answers]
        return datafee.agrees(datafee.read(values[:inputs]), values[inputs:])
    except (JsonRpcError, ChainError) as e:
        logger.warning("chain %d: the fee oracle didn't answer: %s", chain.chain_id, e)
        return False


def _int(answer: Answer) -> int:
    return quantity(result_of(answer))


def schedule_of(measured: Measurements) -> GasSchedule:
    """The measured schedule of an available chain."""
    if measured.schedule is None:
        if measured.unavailable is not None:
            raise UnavailableError.of(measured.unavailable)
        raise UnavailableError(refused(Refusal.NO_SCHEDULE))
    return measured.schedule


class Measuring:
    """What the relay knows of its chain as last read: the measurements, taken again once
    `measure_seconds` old, one measuring at a time; the float and its accounts as last read,
    by a batch or the treasury; and the last bugs of ours a submission's work met. What's wrong
    with the chain, the root shows."""

    def __init__(
        self,
        chain: Chain,
        config: ChainConfig,
        payee: ChecksumAddress,
        domains: Mapping[ChecksumAddress, Eip712Domain],
    ) -> None:
        """Nothing is read here: the chain is measured when it's first needed."""
        self._chain = chain
        self._config = config
        self._payee = payee
        # each token taken over x402, and its EIP-712 domain, as configured
        self._domains = domains
        self._measured: Measurements | None = None
        self._measuring = asyncio.Lock()
        # when the chain is measured again: the measurements' age, or a minute after the nodes
        # didn't answer
        self._due = Millis(0)
        # the last bugs of ours a submission's work met, each its alone: listed with problems
        self._internal_errors: deque[str] = deque(maxlen=INTERNAL_ERRORS_KEPT)
        # the float and its accounts as last read, by a batch or the treasury: what the root
        # shows
        self.accounts_status: AccountsStatus | None = None

    async def measurements(self) -> Measurements:
        """The chain as measured: measured again once `measure_seconds` old. If the nodes don't
        answer, the last are kept and tried again a minute later; with none, unavailable, and
        not tried again for a minute. Why stays in the log: a node's URL isn't a client's."""
        if self._measured is not None and Millis.monotonic() < self._due:
            return self._measured
        async with self._measuring:
            if Millis.monotonic() < self._due:
                if self._measured is None:
                    raise UnavailableError(_UNMEASURED)
                return self._measured
            try:
                measured = await measure(self._chain, self._config, self._payee)
            except (ChainError, JsonRpcError, httpx.HTTPError) as e:
                logger.warning("chain %d: measuring: %s", self._chain.chain_id, e)
                self._due = Millis(Millis.monotonic() + MEASURE_RETRY.millis)
                if self._measured is None:
                    raise UnavailableError(_UNMEASURED) from e
                return self._measured
            except Exception as e:  # a bug of ours: reported, nothing measured, not asked again yet
                logger.exception("chain %d: measuring: internal error", self._chain.chain_id)
                self.internal_error("measuring", e)
                self._measured = None
                self._due = Millis(Millis.monotonic() + MEASURE_RETRY.millis)
                raise UnavailableError(_UNMEASURED) from e
            for problem in measured.problems():
                logger.error("chain %d: %s", self._chain.chain_id, problem)
            self._measured = measured
            measure_age = Millis.of_seconds(self._config.measure_seconds)
            self._due = Millis(Millis.monotonic() + measure_age)
            return measured

    async def available(self) -> Measurements:
        """The measurements, if the chain can be served; else UnavailableError, why."""
        measured = await self.measurements()
        if measured.unavailable is not None:
            raise UnavailableError.of(measured.unavailable)
        return measured

    async def latest(self) -> Latest:
        """The latest block, with what's paid beyond gas on an OP-stack chain: measured first
        (from the last measurements, read again only once they're old), so it's known which."""
        try:
            measured = await self.measurements()
        except UnavailableError:  # nothing priced or sent until it's measured
            return await self._chain.latest()
        return await self._chain.latest(measured.op_stack)

    def internal_error(self, what: str, e: Exception) -> None:
        """A bug of ours in one submission's work, logged where it was caught: listed with the
        chain's problems, so it's seen."""
        self._internal_errors.append(f"internal error {what}: {type(e).__name__}")

    def record_accounts(self, status: AccountsStatus) -> None:
        """`status` kept as the accounts' latest, unless one read at a later block is."""
        if self.accounts_status is None or status.block >= self.accounts_status.block:
            self.accounts_status = status

    @property
    def problems(self) -> tuple[str, ...]:
        """What's wrong with the chain, as last measured, and the last bugs of ours a
        submission's work met: what the root shows."""
        measured = () if self._measured is None else self._measured.problems()
        return (*measured, *self._internal_errors)

    def record_payee(self, holdings: Mapping[ChecksumAddress, int]) -> None:
        """What the payee holds of each x402 token, as the treasury last read it."""
        if self._measured is not None:
            self._measured = self._measured.holding(
                {t: a for t, a in holdings.items() if t in self._domains}
            )
