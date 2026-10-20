"""Vendor contracts for the local chain: each named manifest entry's runtime code fetched from
a live chain at its latest block, written to fixtures/evm/NAME.hex, and its size and sha256
recorded in the manifest. An entry's address, note and storage are written by hand first; the
note says which chain and block its code came from (printed here).

    python -m bruno.tests.vendor RPC NAME...    (make vendor RPC=… VENDOR="NAME…")

The checked-in code is what the tests run: re-vendor only to change it."""

import asyncio
import hashlib
import json
import sys

import httpx

from bruno.net.jsonrpc import call
from bruno.tests.localchain import FIXTURES


async def vendor(rpc: str, names: list[str]) -> None:
    path = FIXTURES / "manifest.json"
    manifest = json.loads(path.read_text())
    async with httpx.AsyncClient() as client:
        block = int(await call(client, rpc, "eth_blockNumber", []), 16)
        for name in names:
            entry = manifest["contracts"][name]
            code: str = await call(client, rpc, "eth_getCode", [entry["address"], hex(block)])
            runtime = bytes.fromhex(code[2:])
            if not runtime:
                raise SystemExit(f"{name}: no code at {entry['address']} at block {block}")
            (FIXTURES / f"{name}.hex").write_text(code)
            entry["bytes"] = len(runtime)
            entry["sha256"] = hashlib.sha256(runtime).hexdigest()
            print(f"{name}: {len(runtime)} bytes from {entry['address']} at block {block}")
    path.write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    asyncio.run(vendor(sys.argv[1], sys.argv[2:]))
