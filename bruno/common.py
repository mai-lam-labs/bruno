# a JSON document, as decoded
type Json = dict[str, Json] | list[Json] | str | int | float | bool | None


class BrunoError(Exception):
    """A domain failure: every module's own errors descend from it."""
