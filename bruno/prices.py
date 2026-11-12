"""What a token is worth in wei, read from pools on the chain: each token's path of pools to
WETH, multiplied out, each pool's price its spot price. A price is kept for the configured
time, then read again.

A pool's price of its token0 in token1, base unit for base unit:
- Uniswap v2: reserve1 / reserve0 (getReserves);
- Uniswap v3: (sqrtPriceX96 / 2**96)**2 (slot0)."""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction

from eth_typing import ChecksumAddress

from .abi import selector
from .chain import Chain, ChainError, address_of, data_of, word_bytes
from .common import BrunoError
from .config import Hop, PoolKind
from .evm import NATIVE_ASSET
from .jsonrpc import Call, eth_call
from .units import Millis, Seconds

_TOKEN0 = selector("token0()")
_TOKEN1 = selector("token1()")
_GET_RESERVES = selector("getReserves()")
_SLOT0 = selector("slot0()")


class PriceError(BrunoError):
    pass


@dataclass(frozen=True, slots=True)
class _Read:
    wei: Fraction
    at: Millis


class Prices:
    """Each token's price in wei a base unit, by its path, kept `ttl`. One read at a
    time for a token: concurrent requests for a price being read wait for that read."""

    def __init__(
        self,
        chain: Chain,
        weth: ChecksumAddress,
        paths: Mapping[ChecksumAddress, tuple[Hop, ...]],
        ttl: Seconds,
    ) -> None:
        self._chain = chain
        self._weth = weth
        self._paths = paths
        self._ttl = ttl
        self._read: dict[ChecksumAddress, _Read] = {}
        self._reading: dict[ChecksumAddress, asyncio.Task[Fraction]] = {}

    async def wei(self, token: ChecksumAddress) -> Fraction:
        """What one base unit of `token` is worth in wei: ETH's is 1."""
        if token == NATIVE_ASSET:
            return Fraction(1)
        kept = self._read.get(token)
        if kept is not None and Millis.monotonic() - kept.at < self._ttl.millis:
            return kept.wei
        reading = self._reading.get(token)
        if reading is None:
            reading = asyncio.ensure_future(self._fresh(token))
            self._reading[token] = reading
        return await asyncio.shield(reading)

    async def _fresh(self, token: ChecksumAddress) -> Fraction:
        try:
            wei = await self._along(token)
            self._read[token] = _Read(wei, Millis.monotonic())
            return wei
        finally:
            del self._reading[token]

    async def _along(self, token: ChecksumAddress) -> Fraction:
        """`token`'s price along its path: every pool's tokens and spot price read in one
        batch, then multiplied out."""
        if token not in self._paths:
            raise PriceError(f"no price path for {token}")
        hops = self._paths[token]
        calls: list[Call] = []
        for hop in hops:
            spot = _GET_RESERVES if hop.kind is PoolKind.UNISWAP_V2 else _SLOT0
            calls += [*token_calls(hop.pool), eth_call(hop.pool, spot)]
        answers = [a.value() for a in await self._chain.batch(calls, atomic=True)]
        pairs = [tokens_of(answers[3 * n], answers[3 * n + 1]) for n in range(len(hops))]
        outs = walk(token, self._weth, hops, pairs)
        price = Fraction(1)
        for n, (hop, (_, token1), out) in enumerate(zip(hops, pairs, outs, strict=True)):
            of_token0 = _spot(hop.kind, hop.pool, data_of(answers[3 * n + 2]))
            price = price * of_token0 if out == token1 else price / of_token0
        return price


def token_calls(pool: str) -> list[Call]:
    """The pool's token0() and token1(), whose answers tokens_of reads."""
    return [eth_call(pool, _TOKEN0), eth_call(pool, _TOKEN1)]


def tokens_of(token0: object, token1: object) -> tuple[ChecksumAddress, ChecksumAddress]:
    """A pool's token0 and token1, from token_calls' answers: ChainError if they aren't words."""
    return address_of(word_bytes(token0)), address_of(word_bytes(token1))


def walk(
    token: ChecksumAddress,
    weth: ChecksumAddress,
    hops: Sequence[Hop],
    pairs: Sequence[tuple[ChecksumAddress, ChecksumAddress]],
) -> list[ChecksumAddress]:
    """The token each of `token`'s hops reaches, its pools trading `pairs`: PriceError if a
    pool doesn't trade the token reached, or the path doesn't end at WETH."""
    outs: list[ChecksumAddress] = []
    at = token
    for hop, (token0, token1) in zip(hops, pairs, strict=True):
        if at not in (token0, token1):
            raise PriceError(f"{token}'s path: pool {hop.pool} doesn't trade {at}")
        at = token1 if at == token0 else token0
        outs.append(at)
    if at != weth:
        raise PriceError(f"{token}'s path ends at {at}, not WETH ({weth})")
    return outs


def _spot(kind: PoolKind, pool: ChecksumAddress, answer: bytes) -> Fraction:
    """The pool's price of token0 in token1, base unit for base unit, from its getReserves
    (v2) or slot0 (v3) answer."""
    if len(answer) < (64 if kind is PoolKind.UNISWAP_V2 else 32):
        raise ChainError(f"pool {pool}: an answer that doesn't decode: {len(answer)} bytes")
    match kind:
        case PoolKind.UNISWAP_V2:
            reserve0 = int.from_bytes(answer[0:32])
            reserve1 = int.from_bytes(answer[32:64])
            if reserve0 == 0 or reserve1 == 0:
                raise PriceError(f"pool {pool} is empty")
            return Fraction(reserve1, reserve0)
        case PoolKind.UNISWAP_V3:
            sqrt_price_x96 = int.from_bytes(answer[0:32])
            if sqrt_price_x96 == 0:
                raise PriceError(f"pool {pool} isn't initialised")
            return Fraction(sqrt_price_x96**2, 2**192)
