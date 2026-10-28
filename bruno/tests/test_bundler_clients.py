"""The user docs' examples (npm-test/userop-*.ts, included whole in docs/using/user-operations.md),
viem's bundler client and ZeroDev's Kernel account client sending a user operation through the
relay, run as an npm user runs them (make npm-e2e), against both relays: the relay's chain
endpoint their bundler transport, nothing of Bruno's in them but that URL, the payment they add
to their calls, and their fees zero."""

import json

import pytest

from bruno.tests import kernel
from bruno.tests.calibur import fresh_client
from bruno.tests.localchain import USDC, LocalChain
from bruno.tests.npm_agent import run_agent
from bruno.tests.served import Served

pytestmark = pytest.mark.npm

HELD = 10 * 10**6
SENT = 2_500_000
# the script's whole run, its container's start included
SCRIPT_SECONDS = 120


@pytest.mark.parametrize("client", ["viem", "zerodev"])
async def test_sends_a_user_operation(chain: LocalChain, served: Served, client: str) -> None:
    """An undeployed Kernel (a plain one, no intent executor) holding only USDC sends 2.5 of it
    through the client's sendUserOperation, its last call paying the relay, and waits with its
    waitForUserOperationReceipt: the receipt says it succeeded, the Kernel is deployed, the
    recipient gains what was sent and the payee what the script paid, and the Kernel holds no
    ETH."""
    owner = fresh_client(f"{client}'s bundler client's account owner")
    account = await kernel.address_of(chain, owner, intent=False)
    recipient = fresh_client(f"{client}'s bundler client's recipient").address
    await chain.mint(USDC, account, HELD)
    before = await chain.balance_of(USDC, served.payee)

    status, out, err = await run_agent(
        {
            "BRUNO_URL": f"{str(served.http.base_url).rstrip('/')}/{served.chain_id}",
            "RPC_URL": chain.url,
            "PRIVATE_KEY": "0x" + owner.key.hex(),
            "TOKEN": USDC.address,
            "RECIPIENT": recipient,
            "AMOUNT": str(SENT),
        },
        SCRIPT_SECONDS,
        served.land,
        f"userop-{client}.ts",
    )

    assert status == 0, f"{out}\n{err}"
    said = json.loads(out.strip().splitlines()[-1])
    assert said["success"] is True, said
    assert said["account"] == account
    assert await chain.code(account) != "0x"
    assert await chain.balance_of(USDC, recipient) == SENT
    assert await chain.balance_of(USDC, served.payee) - before == int(said["paid"])
    assert int(await chain.rpc("eth_getBalance", [account, "latest"]), 16) == 0
