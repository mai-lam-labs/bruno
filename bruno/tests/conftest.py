import os
import socket
import time
from collections.abc import AsyncIterator, Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

import pytest

from bruno.forks import SCHEDULES, GasSchedule
from bruno.tests.localchain import LocalChain

# the relay under test, against both relays: its fixtures, `backend` and `served`
pytest_plugins = ["bruno.tests.served"]

# the chain `make anvil-start` runs: the Makefile exports its port and fork
ANVIL_URL = f"http://127.0.0.1:{os.environ.get('ANVIL_PORT', '8546')}"
ANVIL_HARDFORK = os.environ.get("ANVIL_HARDFORK", "prague")
# the Worker under workerd, started by `make test-fork`: the Makefile exports its port
WORKERD_PORT = int(os.environ.get("WORKERD_PORT", "8547"))

# each run's timings: every test's setup, call and teardown, when it started and how long it
# took, and the session's, a line each as it happens, in dist/test-timings/
_TIMINGS_DIR = Path(__file__).resolve().parents[2] / "dist" / "test-timings"
_timings: list[TextIO] = []


def _timing(phase: str, started: float, seconds: float, what: str) -> None:
    """A line of this run's timings: `phase`, its start (UTC), its seconds, and `what`."""
    for out in _timings:
        start = datetime.fromtimestamp(started, UTC).isoformat(timespec="milliseconds")
        out.write(f"{phase}\t{start}\t{seconds:.3f}\t{what}\n")
        out.flush()


def pytest_sessionstart(session: pytest.Session) -> None:
    del session
    _TIMINGS_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    _timings.append((_TIMINGS_DIR / f"{now}-{ANVIL_HARDFORK}.tsv").open("w"))
    _timings[0].write("phase\tstart\tseconds\ttest\n")
    _timing("session", time.time(), 0.0, "started")


def pytest_sessionfinish(session: pytest.Session) -> None:
    del session
    _timing("session", time.time(), 0.0, "finished")
    for out in _timings:
        out.close()
    _timings.clear()


def _timed(phase: str, item: pytest.Item) -> Generator[None, object, object]:
    started, clock = time.time(), time.perf_counter()
    try:
        return (yield)
    finally:
        _timing(phase, started, time.perf_counter() - clock, item.nodeid)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_setup(item: pytest.Item) -> Generator[None, object, object]:
    return (yield from _timed("setup", item))


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, object, object]:
    return (yield from _timed("call", item))


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item: pytest.Item) -> Generator[None, object, object]:
    return (yield from _timed("teardown", item))


@pytest.fixture
def schedule() -> GasSchedule:
    """The local chain's gas schedule: its fork's."""
    return SCHEDULES[ANVIL_HARDFORK]


@pytest.fixture
async def chain() -> AsyncIterator[LocalChain]:
    """The local chain, set up by `make anvil-start`; whatever a test does to it is undone. Each
    test starts on the wall clock: a block mined at it, after the snapshot, so the chain's time
    is the same whatever ran before (a revert takes the chain back to the setup's head, behind
    the wall clock, `ANVIL_GENESIS_SECONDS_AGO`)."""
    local = LocalChain(ANVIL_URL)
    snapshot = await local.snapshot()
    await local.mine_at(int(time.time()))
    yield local
    await local.revert(snapshot)


@pytest.fixture(scope="session")
def workerd() -> str:
    """The bundled Worker under workerd, started by `make test-fork` beside anvil: its base
    URL."""
    try:
        socket.create_connection(("127.0.0.1", WORKERD_PORT), timeout=1).close()
    except OSError as e:
        raise ConnectionError(f"workerd isn't listening on {WORKERD_PORT}: run through make") from e
    return f"http://127.0.0.1:{WORKERD_PORT}"
