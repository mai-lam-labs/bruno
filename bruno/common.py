import re
from dataclasses import dataclass

import msgspec

# a JSON document, as decoded
type Json = dict[str, Json] | list[Json] | str | int | float | bool | None


class BrunoError(Exception):
    """A domain failure: every module's own errors descend from it."""


# a missing field, as msgspec's ValidationError names it: in its message, not its path
_MISSING = re.compile(r"Object missing required field `(?P<field>[^`]*)`")
# where it says it failed: its path is only in its message, as Litestar also reads it
_AT = " - at `$"


@dataclass(frozen=True, slots=True)
class Invalid:
    """A value that isn't its type, as msgspec says: the field at fault, a path within the
    value, and why."""

    field: str
    why: str

    @classmethod
    def at(cls, why: str, path: str, whole: str) -> Invalid:
        """`why` at `path` within the value; `whole` if it's the whole value."""
        if (missing := _MISSING.fullmatch(why)) is not None:
            path = f"{path}.{missing['field']}" if path else missing["field"]
        return cls(path or whole, why)

    @classmethod
    def of(cls, e: msgspec.ValidationError, whole: str) -> Invalid:
        """msgspec's ValidationError `e`; `whole` if it's the whole value."""
        why, at, path = str(e).rpartition(_AT)
        if not at:
            return cls.at(path, "", whole)
        return cls.at(why, path.removesuffix("`").removeprefix("."), whole)


def whole(pattern: str) -> str:
    """A regular expression matching a whole string as `pattern`, and nothing after it. `$`
    alone also matches before a final newline in Python's (msgspec's and Litestar's patterns
    are searched), not in ECMA-262's, which JSON Schema and the Worker's are: this is both's."""
    return f"^(?:{pattern})(?![\\s\\S])"
