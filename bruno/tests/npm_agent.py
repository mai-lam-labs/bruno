"""A script as an npm user runs it, in a container: the AgentKit agent (npm-test/, the packed
packages installed by npm in NPM_E2E_APP: make npm-e2e, make sepolia-agentkit), or a user docs'
ERC-4337 script (npm-userops/, installed in NPM_USEROPS_APP: make npm-e2e, make
sepolia-userops), run by Node in NODE_IMAGE on the host's network, so it reaches the relay and
the node the test gives it."""

import asyncio
import os
from collections.abc import Awaitable, Callable

# the installed projects a script is run from: the AgentKit agent's, and the user docs' ERC-4337
# scripts'
AGENT = "NPM_E2E_APP"
USEROPS = "NPM_USEROPS_APP"
# how often a waiting test lands a block on the local chain, while the agent runs
LAND_SECONDS = 0.5


async def run_agent(
    env: dict[str, str],
    seconds: float,
    land: Callable[[], Awaitable[object]] | None = None,
    script: str = "agent.ts",
    app: str = AGENT,
) -> tuple[int, str, str]:
    """`script` (the agent, by default) run with `env`, for at most `seconds`, its container's
    start included: its exit status, output and errors. `land`, if given, lands a block each
    LAND_SECONDS while it runs (a local chain, whose blocks come only when asked). `app`, the
    environment variable naming the installed project it's run from."""
    names = [arg for name, value in env.items() for arg in ("-e", f"{name}={value}")]
    agent = await asyncio.create_subprocess_exec(
        "docker",
        "run",
        "--rm",
        "--network=host",
        f"--user={os.getuid()}:{os.getgid()}",
        "-v",
        f"{os.environ[app]}:/app:ro",
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
