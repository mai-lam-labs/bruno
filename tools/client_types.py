"""The relay's codes as the TypeScript client types them (clients/typescript/src/refusals.ts,
written by make vectors): every refusal an error names as its `reason`, who can act on one, and
why a submission was rejected, each a union of its names, so the client's types are the
relay's own.

    python tools/client_types.py > clients/typescript/src/refusals.ts
"""

from enum import StrEnum

from bruno.refusals import Category, Refusal
from bruno.tracked import Rejection


def union(name: str, plural: str, doc: str, values: type[StrEnum]) -> str:
    """`values` as a constant list of their names, and `name` the type of one of them."""
    names = ", ".join(f'"{value}"' for value in values)
    return (
        f"/** {doc} */\nexport const {plural} = [{names}] as const;\n"
        f"export type {name} = (typeof {plural})[number];\n"
    )


print(
    "// Written by make vectors from bruno/refusals.py and bruno/tracked.py: not edited by hand.\n"
)
print(union("Refusal", "REFUSALS", "Each refusal, by name: an error's `reason`.", Refusal))
print(union("Category", "CATEGORIES", "Who can act on a refusal.", Category))
print(
    union(
        "Rejection", "REJECTIONS", "Why a submission was rejected: its status' `reason`.", Rejection
    ),
    end="",
)
