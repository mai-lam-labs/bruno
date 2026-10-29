"""The user docs' examples (npm-test/userop-*.ts, each included whole in its page under
docs/using/user-operations/), viem's bundler client and ZeroDev's Kernel account client sending a
user operation through the relay, run as an npm user runs them (make npm-e2e), against both relays, the relay's chain
endpoint their bundler transport: paying by its fees, from an account holding ETH, with nothing
of Bruno's in them but that URL; or paying in a token, from one holding only that, the payment
added to their calls and their fees zero."""

import json

import pytest

from bruno.tests import kernel
from bruno.tests.calibur import fresh_client
from bruno.tests.deployments import MAX_GAS
from bruno.tests.localchain import (
    ENTRY_POINT_0_7,
    KERNEL_3_3,
    SIMPLE_7702_ACCOUNT,
    USDC,
    LocalChain,
)
from bruno.tests.npm_agent import run_agent
from bruno.tests.served import Served

pytestmark = pytest.mark.npm

SENT = 2_500_000
# the script's whole run, its container's start included
SCRIPT_SECONDS = 120
# what an account paying by its fees is given
ETH = 10**17


async def the_most_paid(served: Served) -> int:
    """What the relay charges in USDC for a call of three quarters of the most gas a transaction
    may use, a transaction near the largest: more than any op's price here (an undeployed
    Kernel's deployment is over a million gas on Amsterdam)."""
    quote = await served.http.get(
        f"/{served.chain_id}/v1/quote",
        params={"gas": hex(MAX_GAS * 3 // 4), "callBytes": "0x800", "tokens": USDC.address},
    )
    assert quote.status_code == 200, quote.text
    return int(quote.json()["quotes"][0]["amount"], 16)


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
    await chain.mint(USDC, account, SENT + await the_most_paid(served))
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


@pytest.mark.parametrize("client", ["viem", "zerodev"])
async def test_sends_a_user_operation_paying_by_its_fees(
    chain: LocalChain, served: Served, client: str
) -> None:
    """An undeployed Kernel holding ETH and USDC sends 2.5 USDC through the client set up as for
    any bundler (the relay its bundler, its gas price the relay's answer): the receipt says it
    succeeded, the Kernel is deployed, the recipient gains what was sent, and the payee gains in
    ETH what the op's gas cost, as much as the Kernel spent of its ETH (what's left of its
    prefund kept as its deposit in the EntryPoint)."""
    owner = fresh_client(f"{client}'s ETH-paying account owner")
    account = await kernel.address_of(chain, owner, intent=False)
    recipient = fresh_client(f"{client}'s ETH-paying recipient").address
    await chain.mint(USDC, account, SENT)
    await chain.set_eth_balance(account, ETH)
    before = await chain.eth_balance(served.payee)

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
        f"userop-{client}-eth.ts",
    )

    assert status == 0, f"{out}\n{err}"
    said = json.loads(out.strip().splitlines()[-1])
    assert said["success"] is True, said
    assert said["account"] == account
    assert await chain.code(account) != "0x"
    assert await chain.balance_of(USDC, recipient) == SENT
    paid = int(said["paid"])
    assert await chain.eth_balance(served.payee) - before == paid
    deposit = int.from_bytes(
        await chain.call(ENTRY_POINT_0_7, "balanceOf(address)", ["address"], [account])
    )
    assert await chain.eth_balance(account) + deposit == ETH - paid


@pytest.mark.parametrize(
    ("client", "delegate"), [("viem", SIMPLE_7702_ACCOUNT), ("zerodev", KERNEL_3_3)]
)
async def test_an_eoa_sends_as_an_eip7702_account(
    chain: LocalChain, served: Served, client: str, delegate: str
) -> None:
    """An EOA holding ETH and USDC sends 2.5 USDC through the client as an EIP-7702 account
    (viem's Simple7702 account on EntryPoint v0.8; ZeroDev's Kernel v3.3 on v0.7), the client
    set up as for any bundler: its first op brings its authorization, so the receipt says it
    succeeded, the EOA is delegated to the account's code, the recipient gains what was sent,
    and the payee gains in ETH what the op's gas cost."""
    owner = fresh_client(f"{client}'s EIP-7702 account owner")
    recipient = fresh_client(f"{client}'s EIP-7702 account's recipient").address
    await chain.mint(USDC, owner.address, SENT)
    await chain.set_eth_balance(owner.address, ETH)
    before = await chain.eth_balance(served.payee)

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
        f"userop-{client}-7702.ts",
    )

    assert status == 0, f"{out}\n{err}"
    said = json.loads(out.strip().splitlines()[-1])
    assert said["success"] is True, said
    assert said["account"] == owner.address
    assert await chain.code(owner.address) == "0xef0100" + delegate[2:].lower()
    assert await chain.balance_of(USDC, recipient) == SENT
    assert await chain.eth_balance(served.payee) - before == int(said["paid"])
