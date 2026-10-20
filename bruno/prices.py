"""What a token is worth in wei, read from pools on the chain: each token's path of pools to
WETH, multiplied out, each pool's price its spot price. A price is kept for the configured
time, then read again.

A pool's price of its token0 in token1, base unit for base unit:
- Uniswap v2: reserve1 / reserve0 (getReserves);
- Uniswap v3: (sqrtPriceX96 / 2**96)**2 (slot0)."""

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction

from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from .chain import Chain
from .common import BrunoError
from .config import Hop, PoolKind
from .evm import NATIVE_ASSET

_TOKEN0 = keccak(text="token0()")[:4]
_TOKEN1 = keccak(text="token1()")[:4]
_GET_RESERVES = keccak(text="getReserves()")[:4]
_SLOT0 = keccak(text="slot0()")[:4]


class PriceError(BrunoError):
    pass


@dataclass(frozen=True, slots=True)
class _Read:
    wei: Fraction
    at: float  # time.monotonic()


class Prices:
    """Each token's price in wei a base unit, by its path, kept `ttl_seconds`. One read at a
    time for a token: concurrent requests for a price being read wait for that read."""

    def __init__(
        self,
        chain: Chain,
        weth: ChecksumAddress,
        paths: Mapping[ChecksumAddress, tuple[Hop, ...]],
        ttl_seconds: float,
    ) -> None:
        self._chain = chain
        self._weth = weth
        self._paths = paths
        self._ttl = ttl_seconds
        self._read: dict[ChecksumAddress, _Read] = {}
        self._reading: dict[ChecksumAddress, asyncio.Task[Fraction]] = {}

    async def wei(self, token: ChecksumAddress) -> Fraction:
        """What one base unit of `token` is worth in wei: ETH's is 1."""
        if token == NATIVE_ASSET:
            return Fraction(1)
        kept = self._read.get(token)
        if kept is not None and time.monotonic() - kept.at < self._ttl:
            return kept.wei
        reading = self._reading.get(token)
        if reading is None:
            reading = asyncio.ensure_future(self._fresh(token))
            self._reading[token] = reading
        return await asyncio.shield(reading)

    async def _fresh(self, token: ChecksumAddress) -> Fraction:
        try:
            wei = await self._along(token)
            self._read[token] = _Read(wei, time.monotonic())
            return wei
        finally:
            del self._reading[token]

    async def _along(self, token: ChecksumAddress) -> Fraction:
        if token not in self._paths:
            raise PriceError(f"no price path for {token}")
        price = Fraction(1)
        at = token
        for hop in self._paths[token]:
            pool = to_checksum_address(hop.pool)
            token0 = await self._address(pool, _TOKEN0)
            token1 = await self._address(pool, _TOKEN1)
            of_token0 = await self._spot(hop.kind, pool)
            if at == token0:
                price, at = price * of_token0, token1
            elif at == token1:
                price, at = price / of_token0, token0
            else:
                raise PriceError(f"{token}'s path: pool {pool} doesn't trade {at}")
        if at != self._weth:
            raise PriceError(f"{token}'s path ends at {at}, not WETH ({self._weth})")
        return price

    async def _address(self, pool: ChecksumAddress, selector: bytes) -> ChecksumAddress:
        return to_checksum_address((await self._chain.call(pool, selector))[12:32])

    async def _spot(self, kind: PoolKind, pool: ChecksumAddress) -> Fraction:
        """The pool's price of token0 in token1, base unit for base unit."""
        match kind:
            case PoolKind.UNISWAP_V2:
                answer = await self._chain.call(pool, _GET_RESERVES)
                reserve0 = int.from_bytes(answer[0:32])
                reserve1 = int.from_bytes(answer[32:64])
                if reserve0 == 0 or reserve1 == 0:
                    raise PriceError(f"pool {pool} is empty")
                return Fraction(reserve1, reserve0)
            case PoolKind.UNISWAP_V3:
                sqrt_price_x96 = int.from_bytes((await self._chain.call(pool, _SLOT0))[0:32])
                if sqrt_price_x96 == 0:
                    raise PriceError(f"pool {pool} isn't initialised")
                return Fraction(sqrt_price_x96**2, 2**192)
