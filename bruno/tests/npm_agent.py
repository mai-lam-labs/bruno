"""A script of npm-test/ as an npm user runs it (the AgentKit agent, viem's bundler client), in
a container: the packed packages installed by npm in NPM_E2E_APP (make npm-e2e, make
sepolia-agentkit), run by Node in NODE_IMAGE on the host's network, so it reaches the relay and
the node the test gives it."""

import asyncio
import os
from collections.abc import Awaitable, Callable

# how often a waiting test lands a block on the local chain, while the agent runs
LAND_SECONDS = 0.5


async def run_agent(
    env: dict[str, str],
    seconds: float,
    land: Callable[[], Awaitable[object]] | None = None,
    script: str = "agent.ts",
) -> tuple[int, str, str]:
    """`script` (the agent, by default) run with `env`, for at most `seconds`, its container's
    start included: its exit status, output and errors. `land`, if given, lands a block each
    LAND_SECONDS while it runs (a local chain, whose blocks come only when asked)."""
    names = [arg for name, value in env.items() for arg in ("-e", f"{name}={value}")]
    agent = await asyncio.create_subprocess_exec(
        "docker",
        "run",
        "--rm",
        "--network=host",
        f"--user={os.getuid()}:{os.getgid()}",
        "-v",
        f"{os.environ['NPM_E2E_APP']}:/app:ro",
        "-w",
        "/app",
        *names,
        os.environ["NODE_IMAGE"],
        "node",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    running = asyncio.create_task(agent.communicate())
    try:
        async with asyncio.timeout(seconds):
            while not running.done():
                if land is not None:
                    await land()
                await asyncio.wait({running}, timeout=LAND_SECONDS)
    finally:
        if agent.returncode is None:
            agent.terminate()  # docker passes it on to the container, which --rm removes
    out, err = await running
    assert agent.returncode is not None
    return agent.returncode, out.decode(), err.decode()
