"""Units of time as types, so a number of seconds can't be mistaken for milliseconds: a field
or constant says its unit by its type, not its name, and turning one unit into another is
spelled out where it happens (`Seconds(2).millis`). As worker/src/units.ts's.

They're whole numbers, so arithmetic on them gives a plain int, wrapped again where the result
is a duration or an instant (`Millis(a + b)`). A clock or a timeout that wants a number of
seconds takes `.seconds`, a float; a sub-second interval is written in Millis, and so is a
configured number of seconds that may be fractional (`Millis.of_seconds`)."""

import time
from typing import ClassVar, Self


class Duration(int):
    """A whole, non-negative number of some unit of time."""

    PER_SECOND: ClassVar[int]

    def __new__(cls, value: int) -> Self:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{cls.__name__}: expected an integer, got {value!r}")
        if value < 0:
            raise ValueError(f"{cls.__name__}: must be non-negative, got {value}")
        return super().__new__(cls, value)

    @property
    def seconds(self) -> float:
        """As a number of seconds, for a clock or a timeout."""
        return self / self.PER_SECOND

    @property
    def millis(self) -> Millis:
        return Millis(self * Millis.PER_SECOND // self.PER_SECOND)


class Millis(Duration):
    """Milliseconds: the relay's clock, and its intervals."""

    PER_SECOND: ClassVar[int] = 1000

    @classmethod
    def of_seconds(cls, seconds: float) -> Millis:
        """A number of seconds, perhaps fractional (a configured interval), to the millisecond."""
        return cls(round(seconds * cls.PER_SECOND))

    @classmethod
    def monotonic(cls) -> Millis:
        """The relay's clock: only the difference between two readings means anything, and it
        never jumps as the wall clock can. The Worker's is Date.now(), which outlives it."""
        return cls(time.monotonic_ns() // 1_000_000)

    @classmethod
    def unix(cls) -> Millis:
        """The wall clock, since the epoch: what a block's timestamp is compared with."""
        return cls(time.time_ns() // 1_000_000)


class Seconds(Duration):
    """Seconds."""

    PER_SECOND: ClassVar[int] = 1
