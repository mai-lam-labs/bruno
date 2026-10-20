import os
import socket
from collections.abc import AsyncIterator

import pytest

from bruno.forks import SCHEDULES, GasSchedule
from bruno.tests.localchain import LocalChain

# the chain `make anvil-start` runs: the Makefile exports its port and fork
ANVIL_URL = f"http://127.0.0.1:{os.environ.get('ANVIL_PORT', '8546')}"
ANVIL_HARDFORK = os.environ.get("ANVIL_HARDFORK", "prague")
# the Worker under workerd, started by `make test-fork`: the Makefile exports its port
WORKERD_PORT = int(os.environ.get("WORKERD_PORT", "8547"))


@pytest.fixture
def schedule() -> GasSchedule:
    """The local chain's gas schedule: its fork's."""
    return SCHEDULES[ANVIL_HARDFORK]


@pytest.fixture
async def chain() -> AsyncIterator[LocalChain]:
    """The local chain, set up by `make anvil-start`; whatever a test does to it is undone."""
    local = LocalChain(ANVIL_URL)
    snapshot = await local.snapshot()
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
