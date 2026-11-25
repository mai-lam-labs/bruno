"""The user docs' Python sponsorship script (docs/using/sponsorship/sponsor.py), run as a reader
runs it, against both relays: httpx and eth-account alone, over REST. An account holding only
USDC sends some as its own transaction, the relay sending it the ETH for it and paid in USDC,
the four transactions landing together."""

import asyncio
import json
import os
import sys
from pathlib import Path

from bruno.tests.calibur import fresh_client
from bruno.tests.localchain import USDC, LocalChain
from bruno.tests.served import Served

SCRIPT = Path(__file__).parents[2] / "docs" / "using" / "sponsorship" / "sponsor.py"
# the account's USDC, what it sends, and the most it pays the relay, in USDC's smallest unit
HELD = 100 * 10**6
SENT = 2_500_000
MAX = 90 * 10**6
SCRIPT_SECONDS = 60
# how often a block is landed on the local chain while the script waits for its status
LAND_SECONDS = 0.5


async def test_the_python_script_sends_an_accounts_own_transaction(
    chain: LocalChain, served: Served
) -> None:
    """The script's account, never used and holding no ETH, has a transfer of 2.5 USDC
    sponsored over REST: it asks for a quote, checks what it's given to sign, signs the
    transfer and the payment with eth-account without sending them, submits them, and waits
    for the status. They land between the relay's two. The recipient gains what was sent, from
    the account itself; the payee gains what the script said it paid, less the unit the
    forwarder keeps; the account's nonce is two on."""
    user = fresh_client("the python script's account")
    recipient = fresh_client("the python script's recipient").address
    await chain.mint(USDC, user.address, HELD)
    before = await chain.balance_of(USDC, served.payee)
    env = {
        **os.environ,
        "BRUNO_URL": str(served.http.base_url).rstrip("/"),
        "CHAIN": str(served.chain_id),
        "PRIVATE_KEY": "0x" + user.key.hex(),
        "TOKEN": USDC.address,
        "RECIPIENT": recipient,
        "AMOUNT": str(SENT),
        "MAX": str(MAX),
    }

    script = await asyncio.create_subprocess_exec(
        sys.executable,
        str(SCRIPT),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    running = asyncio.create_task(script.communicate())
    try:
        async with asyncio.timeout(SCRIPT_SECONDS):
            while not running.done():
                await served.land()
                await asyncio.wait({running}, timeout=LAND_SECONDS)
    finally:
        if script.returncode is None:
            script.terminate()
    out, err = await running

    assert script.returncode == 0, f"{out.decode()}\n{err.decode()}"
    said = json.loads(out.decode().strip().splitlines()[-1])
    paid = int(said["paid"])
    assert said["status"] == "included", said
    assert said["account"] == user.address
    assert len(said["transactions"]) == 4, said
    sent = await chain.rpc("eth_getTransactionByHash", [said["transactions"][1]])
    assert sent["from"].lower() == user.address.lower()
    assert await chain.balance_of(USDC, recipient) == SENT
    assert await chain.balance_of(USDC, served.payee) - before == paid - 1
    assert await chain.balance_of(USDC, user.address) == HELD - SENT - paid
    assert await chain.nonce(user.address) == 2
