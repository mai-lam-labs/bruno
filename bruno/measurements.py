"""What the relay knows of a chain by measuring it, never by assuming it: the nodes' chain id,
the fork in force, whether the deployer is there, whether it's an OP-stack chain (its fee
oracle's code) whose fees beyond gas our formulas give, and each token's gas, decimals and
symbol, and the payee's holdings of each x402 token. One batch (and, on an OP-stack chain, one
more at its block), taken again once it's an hour old.

Anything that fails takes out only what depends on it: a token that can't be measured is
suspended, the rest served; a chain whose nodes are another chain, whose fork no schedule
matches, or whose deployer is missing is unavailable, nothing sent on it."""

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address

from . import datafee
from .chain import Chain, ChainError, empty_call
from .config import ChainConfig, Eip712Domain
from .deployer import DEPLOYER, RUNTIME
from .eip3009 import measured, measuring
from .evm import NATIVE_ASSET
from .forks import GasSchedule, schedule_of
from .net.jsonrpc import Answer, Call, JsonRpcError
from .runner import Erc20
from .tokens import balance_of_call, decimals_call, erc20, symbol_call, symbol_of
from .tokens import measuring as measuring_balance

logger = logging.getLogger(__name__)

# where the tokens' answers start in measure's batch: after the chain id, the block, the empty
# call, the deployer's code and the fee oracle's
_TOKENS = 5


@dataclass(frozen=True, slots=True)
class Measurements:
    """A chain as measured at `block` (`at`, time.monotonic()): its fork's schedule, each
    token's measured gas (None for ETH, read with BALANCE; an x402 token's transfer gas with
    it), decimals and symbol, which x402 tokens the payee holds some of; each token suspended
    and why, and each taken without x402 and why; whether it's an OP-stack chain (a data fee
    read with each block); and why the chain is unavailable, if it is."""

    at: float
    block: int
    schedule: GasSchedule | None
    tokens: Mapping[ChecksumAddress, Erc20 | None]
    decimals: Mapping[ChecksumAddress, int]
    symbols: Mapping[ChecksumAddress, str]
    payee_holds: frozenset[ChecksumAddress] = frozenset()
    suspended: Mapping[ChecksumAddress, str] = field(default_factory=dict)
    without_x402: Mapping[ChecksumAddress, str] = field(default_factory=dict)
    op_stack: bool = False
    unavailable: str | None = None

    def problems(self) -> tuple[str, ...]:
        """What's wrong, each a line: none if all's well."""
        lines = [] if self.unavailable is None else [f"unavailable: {self.unavailable}"]
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
    erc20s = [
        to_checksum_address(t.address)
        for t in config.tokens
        if to_checksum_address(t.address) != NATIVE_ASSET
    ]
    domains: dict[ChecksumAddress, Eip712Domain] = {
        to_checksum_address(t.address): t.eip3009
        for t in config.tokens
        if t.eip3009 is not None and to_checksum_address(t.address) in erc20s
    }
    transfers = [measuring(d, chain.chain_id, t, payee) for t, d in domains.items()]
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
        ]
    )
    try:
        block = int(answers[1].value()["number"], 16)
        nodes = int(answers[0].value(), 16)
        empty_call_gas = int(answers[2].value(), 16)
        deployer = answers[3].value()
        op_stack = answers[4].value() not in ("0x", "0x0")
    except (TypeError, ValueError, KeyError) as e:  # an answer that isn't one: the nodes failed
        raise ChainError(f"a malformed answer: {e!r}") from e
    schedule = schedule_of(empty_call_gas)
    unavailable = None
    if nodes != chain.chain_id:
        unavailable = f"the nodes are chain {nodes}, not {chain.chain_id}"
    elif schedule is None:
        unavailable = f"a fork no schedule knows: a plain call costs {empty_call_gas}"
    elif deployer != "0x" + RUNTIME.hex():
        unavailable = f"the deployer isn't at {DEPLOYER}"
    elif op_stack and not await _data_fee_known(chain, block):
        unavailable = "an OP-stack fee formula Bruno doesn't know: its oracle answers otherwise"
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
                symbols[token] = symbol_of(bytes.fromhex(answers[_TOKENS + 2 * n + i].value()[2:]))
            except (JsonRpcError, ValueError, KeyError, TypeError) as e:
                logger.warning("chain %d: %s unmeasured: %s", chain.chain_id, token, e)
                suspended[token] = "its balanceOf, decimals() or symbol() didn't measure"
                tokens.pop(token, None)
        at = _TOKENS + 3 * n
        for i, (token, (_, data)) in enumerate(zip(domains, transfers, strict=True)):
            current = tokens.get(token)
            if current is None:
                continue
            try:
                gas = measured(_int(answers[at + i]), data, schedule)
                tokens[token] = replace(current, transfer_gas=gas)
                if _int(answers[at + len(domains) + i]) > 0:
                    payee_holds.add(token)
            except (JsonRpcError, ValueError, TypeError) as e:
                # taken without x402: only its transfer failed (a domain not the token's, say)
                logger.warning("chain %d: %s's transfer unmeasured: %s", chain.chain_id, token, e)
                without_x402[token] = "a transfer signed for its eip3009 domain didn't measure"
    return Measurements(
        at=time.monotonic(),
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


async def _data_fee_known(chain: Chain, block: int) -> bool:
    """Whether, at `block`, the chain's fee oracle answers what datafee's formulas give from the
    inputs read at the same block: one batch, its reads all of that block."""
    at = hex(block)
    answers = await chain.batch([*datafee.reading(at), *datafee.checking(at)])
    inputs = len(datafee.reading(at))
    try:
        return datafee.agrees(datafee.read(answers[:inputs]), answers[inputs:])
    except (JsonRpcError, ValueError, TypeError) as e:
        logger.warning("chain %d: the fee oracle didn't answer: %s", chain.chain_id, e)
        return False


def _int(answer: Answer) -> int:
    return int(answer.value(), 16)
