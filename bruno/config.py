"""The relay's configuration: a JSON file, one entry a chain, keyed by chain id.

Secrets are never in it: the float's key is a path (or given on the command line, so one
configuration serves several floats), as is a bundler's identity key."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import msgspec

from .bundle import Mode
from .common import BrunoError

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
class Token:
    """An asset accepted in payment, its margin, and the pools from it to WETH: a token's
    address, or ERC-7528's for ETH, which has no pools."""

    address: Address
    price: tuple[Hop, ...] = ()
    margin_multiplier: Decimal = Decimal(1)

    def __post_init__(self) -> None:
        _at_least_one(self.margin_multiplier)


@dataclass(frozen=True, slots=True)
class Bundler:
    """A builder's bundle endpoint, the mode it's sent bundles in, and the key that signs
    them (X-Flashbots-Signature), for one that wants it."""

    url: str
    mode: Mode
    identity_key: str | None = None


@dataclass(frozen=True, slots=True)
class ChainConfig:
    rpc: tuple[str, ...]
    # receives the payments; the float only funds the concurrency accounts
    payee: Address
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
    # the priority fee each transaction offers, in wei
    priority_fee: Annotated[int, msgspec.Meta(ge=0)]
    max_gas: Annotated[int, msgspec.Meta(ge=1)]
    max_call_bytes: Annotated[int, msgspec.Meta(ge=0)]
    margin_multiplier: Decimal = Decimal(1)
    price_ttl_seconds: Annotated[int, msgspec.Meta(ge=0)] = 0
    # how many blocks a quote is meant to hold for: its amount covers the next base fee
    # risen by the most EIP-1559 allows in as many blocks
    quote_blocks: Annotated[int, msgspec.Meta(ge=1)] = 1
    # how often a batch is made from the queue
    batch_seconds: Annotated[float, msgspec.Meta(gt=0)] = 12.0
    # a top-up covers this many times its transaction's cost: headroom, so an account
    # covers its next transactions without a funding round
    funding_multiplier: Decimal = Decimal(1)
    # how long a funding transaction is waited for before it's given up on
    funding_timeout_seconds: Annotated[float, msgspec.Meta(gt=0)] = 120.0
    float_key: str | None = None

    def __post_init__(self) -> None:
        _at_least_one(self.margin_multiplier)
        _at_least_one(self.funding_multiplier)


@dataclass(frozen=True, slots=True)
class Config:
    chains: dict[int, ChainConfig]


def load(path: Path) -> Config:
    try:
        return msgspec.json.decode(path.read_bytes(), type=Config)
    except (OSError, msgspec.ValidationError, msgspec.DecodeError) as e:
        raise ConfigError(f"{path}: {e}") from e
