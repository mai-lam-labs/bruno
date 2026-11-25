"""The user docs' sponsorship script (npm-test/sponsor.ts) as an npm user runs it, against both
relays (make npm-e2e): the packed client and viem installed by npm, the script run by Node in a
container on the host's network. An account holding only USDC sends some as its own transaction,
the relay sending it the ETH for it and paid in USDC, the four transactions landing together."""

import json

import pytest

from bruno.tests.calibur import fresh_client
from bruno.tests.localchain import USDC, LocalChain
from bruno.tests.npm_agent import run_agent
from bruno.tests.served import Served

pytestmark = pytest.mark.npm

# the account's USDC, what it sends, and the most it pays the relay, in USDC's smallest unit
HELD = 100 * 10**6
SENT = 2_500_000
MAX = 90 * 10**6
# the script's whole run, its container's start included
SCRIPT_SECONDS = 120


async def test_an_account_holding_only_usdc_sends_its_own_transaction(
    chain: LocalChain, served: Served
) -> None:
    """The script's account, never used and holding no ETH, has a transfer of 2.5 USDC
    sponsored: viem's local account signs the transfer and the payment as the relay's quote
    filled them in, and they land between the relay's two. The recipient gains what was sent,
    from the account itself; the payee gains what the script said it paid, less the unit the
    forwarder keeps; the account's nonce is two on."""
    user = fresh_client("the sponsorship script's account")
    recipient = fresh_client("the sponsorship script's recipient").address
    await chain.mint(USDC, user.address, HELD)
    before = await chain.balance_of(USDC, served.payee)

    status, out, err = await run_agent(
        {
            "BRUNO_URL": str(served.http.base_url).rstrip("/"),
            "CHAIN": str(served.chain_id),
            "PRIVATE_KEY": "0x" + user.key.hex(),
            "TOKEN": USDC.address,
            "RECIPIENT": recipient,
            "AMOUNT": str(SENT),
            "MAX": str(MAX),
        },
        SCRIPT_SECONDS,
        served.land,
        "sponsor.ts",
    )

    assert status == 0, f"{out}\n{err}"
    said = json.loads(out.strip().splitlines()[-1])
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
