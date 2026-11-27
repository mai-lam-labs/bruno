"""The user docs' sponsorship scripts (npm-userops/sponsor-*.ts) as an npm user runs them,
against both relays (make npm-e2e): the packed client and each script's own library installed
by npm, the script run by Node in a container on the host's network. An account holding only
USDC sends some as its own transaction, signed by viem's local account or by an ethers Wallet,
the relay sending it the ETH for it and paid in USDC, the four transactions landing together;
and a plain key is delegated to Calibur and has its batch run, in the relay's own two."""

import json

import pytest

from bruno.tests.calibur import fresh_client
from bruno.tests.localchain import CALIBUR, USDC, LocalChain
from bruno.tests.npm_scripts import run_script
from bruno.tests.served import Served

pytestmark = pytest.mark.npm

# the account's USDC, what it sends, and the most it pays the relay, in USDC's smallest unit
HELD = 100 * 10**6
SENT = 2_500_000
MAX = 90 * 10**6
# the script's whole run, its container's start included
SCRIPT_SECONDS = 120


@pytest.mark.parametrize("script", ["sponsor-viem.ts", "sponsor-ethers.ts"])
async def test_an_account_holding_only_usdc_sends_its_own_transaction(
    chain: LocalChain, served: Served, script: str
) -> None:
    """The script's account, never used and holding no ETH, has a transfer of 2.5 USDC
    sponsored: its wallet (viem's local account, or an ethers Wallet behind a signer of a few
    lines) signs the transfer and the payment as the relay's quote filled them in, and they
    land between the relay's two. The recipient gains what was sent, from the account itself;
    the payee gains what the script said it paid, less the unit the forwarder keeps; the
    account's nonce is two on."""
    user = fresh_client(f"{script}'s account")
    recipient = fresh_client(f"{script}'s recipient").address
    await chain.mint(USDC, user.address, HELD)
    before = await chain.balance_of(USDC, served.payee)

    status, out, err = await run_script(
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
        script=script,
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


async def test_a_plain_key_is_delegated_and_its_code_pays(
    chain: LocalChain, served: Served
) -> None:
    """The script's account, a plain key never used and holding no ETH, signs an EIP-7702
    authorization to Calibur and Calibur's batch of a transfer and the payment. The relay's
    first transaction delegates it and runs the batch, and its second collects the payment:
    two transactions, none of the account's own. The account is delegated and still holds no
    ETH, its nonce moved only by its authorization; the recipient gains what was sent and the
    payee what the script said it paid, less the unit the forwarder keeps."""
    user = fresh_client("the delegating script's account")
    recipient = fresh_client("the delegating script's recipient").address
    await chain.mint(USDC, user.address, HELD)
    before = await chain.balance_of(USDC, served.payee)

    status, out, err = await run_script(
        {
            "BRUNO_URL": str(served.http.base_url).rstrip("/"),
            "CHAIN": str(served.chain_id),
            "RPC_URL": chain.url,
            "PRIVATE_KEY": "0x" + user.key.hex(),
            "TOKEN": USDC.address,
            "RECIPIENT": recipient,
            "AMOUNT": str(SENT),
            "MAX": str(MAX),
        },
        SCRIPT_SECONDS,
        served.land,
        script="sponsor-7702.ts",
    )

    assert status == 0, f"{out}\n{err}"
    said = json.loads(out.strip().splitlines()[-1])
    paid = int(said["paid"])
    assert said["status"] == "included", said
    assert len(said["transactions"]) == 2, said
    assert (await chain.code(user.address)).lower() == "0xef0100" + CALIBUR[2:].lower()
    assert await chain.eth_balance(user.address) == 0
    assert await chain.nonce(user.address) == 1
    assert await chain.balance_of(USDC, recipient) == SENT
    assert await chain.balance_of(USDC, served.payee) - before == paid - 1
    assert await chain.balance_of(USDC, user.address) == HELD - SENT - paid
