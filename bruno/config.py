"""The relay's configuration: a JSON file, one entry a chain, keyed by chain id.

Secrets are never in it: the float's key is a path (or given on the command line, so one
configuration serves several floats), as is a bundler's identity key. Nodes with API keys in
their URLs go in a local configuration merged over the default (`merged`), never committed:

    python -m bruno.config DEFAULT [LOCAL] > MERGED
"""

import sys
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import msgspec

from .bundle import Mode
from .chain import host
from .common import BrunoError, Json
from .evm import NATIVE_ASSET
from .net.jsonrpc import Node

type Address = Annotated[str, msgspec.Meta(pattern="^0x[0-9a-fA-F]{40}$")]


def _at_least_one(multiplier: Decimal) -> None:
    """A multiplier on cost, "1.1" being 10% over it: never under it."""
    if not multiplier >= 1:
        raise ValueError(f"a multiplier is at least 1, not {multiplier}")


class ConfigError(BrunoError):
    pass


class PoolKind(StrEnum):
    UNISWAP_V2 = "uniswap-v2"
    UNISWAP_V3 = "uniswap-v3"


@dataclass(frozen=True, slots=True)
class Hop:
    """A pool on a token's way to ETH. Which of its tokens is which, it says itself."""

    kind: PoolKind
    pool: Address


@dataclass(frozen=True, slots=True)
class Eip712Domain:
    """A token's EIP-712 domain as its EIP-3009 authorizations are signed for: its name() and
    version(); its chain id and address complete it."""

    name: str
    version: str


@dataclass(frozen=True, slots=True)
class Token:
    """An asset accepted in payment, its margin, and the pools from it to WETH: a token's
    address, or ERC-7528's for ETH, which has no pools. A token with `eip3009` is accepted
    over x402 too, paid by a signed transferWithAuthorization."""

    address: Address
    price: tuple[Hop, ...] = ()
    margin_multiplier: Decimal = Decimal(1)
    eip3009: Eip712Domain | None = None

    def __post_init__(self) -> None:
        _at_least_one(self.margin_multiplier)
        if self.eip3009 is not None and self.address.lower() == NATIVE_ASSET.lower():
            raise ValueError("ETH has no EIP-3009 transfer: eip3009 is a token's")


@dataclass(frozen=True, slots=True)
class MinFee:
    """The least our fee is on a transaction: `amount` whole tokens (by its decimals()) of
    `token`, one the chain accepts, priced through its pools."""

    token: Address
    amount: Decimal

    def __post_init__(self) -> None:
        if not self.amount > 0:
            raise ValueError(f"a minimum fee is over 0, not {self.amount}")


@dataclass(frozen=True, slots=True)
class Bundler:
    """A builder's bundle endpoint, the mode it's sent bundles in, and the key that signs
    them (X-Flashbots-Signature), for one that wants it."""

    url: str
    mode: Mode
    identity_key: str | None = None


@dataclass(frozen=True, slots=True)
class TreasuryConfig:
    """Converting the payee's revenue to ETH for the float, every `interval_seconds`: the
    float and its accounts topped up to `runway_days` of their measured spend, at least
    enough to fund every account once. A conversion goes ahead only if it delivers at least
    its amount / `max_cost_multiplier`, unless the float would otherwise run dry; a swap takes
    at least the pools' price / `slippage_multiplier`."""

    # Uniswap's SwapRouter02
    router: Address
    interval_seconds: Annotated[int, msgspec.Meta(ge=1)] = 21600
    runway_days: Decimal = Decimal(2)
    max_cost_multiplier: Decimal = Decimal("1.02")
    slippage_multiplier: Decimal = Decimal("1.005")

    def __post_init__(self) -> None:
        if not self.runway_days > 0:
            raise ValueError(f"runway_days is over 0, not {self.runway_days}")
        _at_least_one(self.max_cost_multiplier)
        _at_least_one(self.slippage_multiplier)


@dataclass(frozen=True, slots=True)
class ChainConfig:
    # its nodes: each a URL, or {"url", "max_batch"} for a node that caps a batch request
    rpc: tuple[str | Node, ...]
    # the chain's wrapped ether: where every token's price path ends
    weth: Address
    bundlers: tuple[Bundler, ...]
    tokens: tuple[Token, ...]
    # how many concurrency accounts: a batch's size
    accounts: Annotated[int, msgspec.Meta(ge=1)]
    # the queue's size: a full queue takes a submission only by evicting its least profitable
    max_pending: Annotated[int, msgspec.Meta(ge=1)]
    # the furthest ahead a submission's maxBlockNumber may be
    max_deadline_blocks: Annotated[int, msgspec.Meta(ge=1)]
    max_gas: Annotated[int, msgspec.Meta(ge=1)]
    max_call_bytes: Annotated[int, msgspec.Meta(ge=0)]
    # the furthest ahead an x402 payment's deadline may be: its request is held until it lands
    # or this passes, so it bounds how long a client waits (maxTimeoutSeconds)
    x402_deadline_blocks: Annotated[int, msgspec.Meta(ge=1)] = 10
    # the most EIP-7702 authorizations a submission may carry: each costs reads to plan
    max_authorizations: Annotated[int, msgspec.Meta(ge=0)] = 1
    # how long a finished submission's status is kept before it's forgotten
    retention_seconds: Annotated[float, msgspec.Meta(ge=0)] = 3600.0
    margin_multiplier: Decimal = Decimal(1)
    # our fee is the market's tip a gas, at least this many wei a gas, and at least min_fee
    min_fee_per_gas: Annotated[int, msgspec.Meta(ge=0)] = 0
    min_fee: MinFee | None = None
    price_ttl_seconds: Annotated[int, msgspec.Meta(ge=0)] = 60
    # how many blocks a quote is meant to hold for: its amount covers the next base fee
    # risen by the most EIP-1559 allows in as many blocks
    quote_blocks: Annotated[int, msgspec.Meta(ge=1)] = 1
    # the chain's block time: the next block is a slot after the latest
    slot_seconds: Annotated[int, msgspec.Meta(ge=1)] = 12
    # how often a batch is made from the queue
    batch_seconds: Annotated[float, msgspec.Meta(gt=0)] = 12.0
    # the least time between the relay's steps, whatever the block time: 5 s on any real
    # network (make preflight refuses less); only a local test chain goes lower
    min_step_seconds: Annotated[float, msgspec.Meta(gt=0)] = 5.0
    # a top-up covers this many times its transaction's cost: headroom, so an account
    # covers its next transactions without a funding round
    funding_multiplier: Decimal = Decimal(1)
    # how long a funding transaction is waited for before it's given up on
    funding_timeout_seconds: Annotated[float, msgspec.Meta(gt=0)] = 120.0
    # how old the chain's measurements (its fork, its tokens' gas, the deployer) may get before
    # they're taken again, when next needed
    measure_seconds: Annotated[float, msgspec.Meta(gt=0)] = 3600.0
    # receives the payments; the float only funds the concurrency accounts. Without one, the
    # configuration's payee (or --payee's)
    payee: Address | None = None
    float_key: str | None = None
    # converting the payee's revenue for the float, given the payee's key; none without it
    treasury: TreasuryConfig | None = None

    def __post_init__(self) -> None:
        _at_least_one(self.margin_multiplier)
        _at_least_one(self.funding_multiplier)
        for node in self.rpc:
            if isinstance(node, Node) and node.max_batch < 1:
                raise ValueError(
                    f"{host(node.url)}'s max_batch is at least 1, not {node.max_batch}"
                )
        if self.x402_deadline_blocks > self.max_deadline_blocks:
            raise ValueError(
                f"x402_deadline_blocks {self.x402_deadline_blocks} is over max_deadline_blocks "
                f"{self.max_deadline_blocks}"
            )


@dataclass(frozen=True, slots=True)
class Config:
    chains: dict[int, ChainConfig]
    # the payee of every chain that doesn't name its own; --payee replaces it
    payee: Address | None = None


def load(path: Path) -> Config:
    try:
        return msgspec.json.decode(path.read_bytes(), type=Config)
    except (OSError, msgspec.ValidationError, msgspec.DecodeError) as e:
        raise ConfigError(f"{path}: {e}") from e


def merged(base: Json, over: Json) -> Json:
    """`over` merged into `base`: objects key by key, at any depth; anything else (a list, a
    value) is `over`'s, whole."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for key, value in over.items():
            out[key] = merged(base[key], value) if key in base else value
        return out
    return over


def _main(paths: list[Path]) -> None:
    """Each configuration merged over the one before, checked, written to stdout."""
    try:
        documents: list[Json] = [msgspec.json.decode(p.read_bytes()) for p in paths]
    except (OSError, msgspec.ValidationError, msgspec.DecodeError) as e:
        raise ConfigError(f"{e}") from e
    result = documents[0]
    for document in documents[1:]:
        result = merged(result, document)
    encoded = msgspec.json.encode(result)
    try:
        msgspec.json.decode(encoded, type=Config)
    except msgspec.ValidationError as e:
        raise ConfigError(f"merged: {e}") from e
    sys.stdout.write(msgspec.json.format(encoded, indent=2).decode() + "\n")


if __name__ == "__main__":
    _main([Path(p) for p in sys.argv[1:]])
