# a JSON document, as decoded
type Json = dict[str, Json] | list[Json] | str | int | float | bool | None


class BrunoError(Exception):
    """A domain failure: every module's own errors descend from it."""


def whole(pattern: str) -> str:
    """A regular expression matching a whole string as `pattern`, and nothing after it. `$`
    alone also matches before a final newline in Python's (msgspec's and Litestar's patterns
    are searched), not in ECMA-262's, which JSON Schema and the Worker's are: this is both's."""
    return f"^(?:{pattern})(?![\\s\\S])"
