import os
from collections.abc import AsyncIterator

import pytest

from bruno.forks import SCHEDULES, GasSchedule
from bruno.tests.localchain import LocalChain

# the chain `make anvil-start` runs: the Makefile exports its port and fork
ANVIL_URL = f"http://127.0.0.1:{os.environ.get('ANVIL_PORT', '8546')}"
ANVIL_HARDFORK = os.environ.get("ANVIL_HARDFORK", "prague")


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
