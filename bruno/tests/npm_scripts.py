"""A user docs' script as an npm user runs it, in a container: one of npm-userops/, installed
by npm with the packed client in NPM_USEROPS_APP (make npm-e2e, make sepolia-userops), run by
Node in NODE_IMAGE on the host's network, so it reaches the relay and the node the test gives
it."""

import asyncio
import os
from collections.abc import Awaitable, Callable

# the environment variable naming the installed project the scripts are run from
USEROPS = "NPM_USEROPS_APP"
# how often a waiting test lands a block on the local chain, while a script runs
LAND_SECONDS = 0.5


async def run_script(
    env: dict[str, str],
    seconds: float,
    land: Callable[[], Awaitable[object]] | None = None,
    *,
    script: str,
) -> tuple[int, str, str]:
    """`script` run with `env`, for at most `seconds`, its container's start included: its exit
    status, output and errors. `land`, if given, lands a block each LAND_SECONDS while it runs
    (a local chain, whose blocks come only when asked)."""
    names = [arg for name, value in env.items() for arg in ("-e", f"{name}={value}")]
    node = await asyncio.create_subprocess_exec(
        "docker",
        "run",
        "--rm",
        "--network=host",
        f"--user={os.getuid()}:{os.getgid()}",
        "-v",
        f"{os.environ[USEROPS]}:/app:ro",
        "-w",
        "/app",
        *names,
        os.environ["NODE_IMAGE"],
        "node",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    running = asyncio.create_task(node.communicate())
    try:
        async with asyncio.timeout(seconds):
            while not running.done():
                if land is not None:
                    await land()
                await asyncio.wait({running}, timeout=LAND_SECONDS)
    finally:
        if node.returncode is None:
            node.terminate()  # docker passes it on to the container, which --rm removes
    out, err = await running
    assert node.returncode is not None
    return node.returncode, out.decode(), err.decode()
