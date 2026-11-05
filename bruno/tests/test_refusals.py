"""What refusals say, alike on both relays (docs/development/design/refusals.md): one catalogue of
templates, each used by both, the Worker's the Python's as exported."""

import json
from pathlib import Path

from bruno.refusals import Refusal, exported

_ROOT = Path(__file__).parents[2]


def _sources(directory: str, suffix: str, but: str) -> str:
    return "\n".join(
        p.read_text()
        for p in sorted((_ROOT / directory).rglob(f"*{suffix}"))
        if "tests" not in p.parts and p.name != but
    )


def test_every_refusal_is_said_alike_on_both_relays() -> None:
    """Each template is used by the Python relay and by the Worker, whose catalogue is the
    Python's as exported: a refusal reworded on one relay alone, or written inline, shows here
    as a template one relay no longer uses."""
    python = _sources("bruno", ".py", "refusals.py")
    worker = _sources("worker/src", ".ts", "refusals.ts")
    shipped = json.loads((_ROOT / "worker/src/refusals.json").read_text())

    assert shipped == exported(), "worker/src/refusals.json is stale: make vectors"
    assert [r for r in Refusal if f"Refusal.{r.name}" not in python] == []
    assert [r for r in Refusal if f'"{r.value}"' not in worker] == []
