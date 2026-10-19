"""Run the relay: python -m bruno.server --config CONFIG [--port PORT] [--float-key PATH].

--float-key overrides every chain's float_key, so one configuration serves several nodes,
each with its own float."""

import argparse
import asyncio
import logging
from pathlib import Path

import httpx
import uvicorn

from ..accounts import read_key
from ..config import ConfigError, load
from ..forks import network_forks
from ..relay import Relay, open_relay
from .app import create_app

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8402


async def serve(config_path: Path, host: str, port: int, float_key: Path | None) -> None:
    config = load(config_path)
    async with httpx.AsyncClient() as client:
        relays: dict[int, Relay] = {}
        for chain_id, chain in config.chains.items():
            key = float_key if float_key is not None else chain.float_key
            if key is None:
                raise ConfigError(f"chain {chain_id}: no float key (float_key, or --float-key)")
            relays[chain_id] = await open_relay(
                client, chain_id, chain, read_key(Path(key)), network_forks(chain_id)
            )
        server = uvicorn.Server(uvicorn.Config(create_app(relays), host=host, port=port))
        async with asyncio.TaskGroup() as tasks:
            loops = [tasks.create_task(relay.run()) for relay in relays.values()]
            await server.serve()
            for loop in loops:
                loop.cancel()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m bruno.server", description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--float-key", type=Path, help="the float's key file, for every chain")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve(args.config, args.host, args.port, args.float_key))


if __name__ == "__main__":
    main()
