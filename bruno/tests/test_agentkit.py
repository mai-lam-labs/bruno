"""AgentKit as an npm user runs it, against both relays (make npm-e2e): the packed packages
installed by npm, an agent of AgentKit's own registry and viem wallet provider run by Node in a
container on the host's network. Holding only USDC, it sends some, paying Bruno over x402; the
transfer and the payment land in one transaction."""

import json

import pytest
from eth_typing import ChecksumAddress

from bruno.tests import kernel, smartwallet
from bruno.tests.calibur import fresh_client
from bruno.tests.localchain import SMART_WALLET_FACTORY_1_1, USDC, LocalChain
from bruno.tests.npm_agent import run_agent
from bruno.tests.served import Served

pytestmark = pytest.mark.npm

# the agent's USDC, the amount it sends, and the most it pays Bruno, in whole USDC
HELD = 10 * 10**6
AMOUNT = "2.5"
SENT = 2_500_000
MAX_FEE = "5"
# the agent's whole run, its container's start included
AGENT_SECONDS = 120


def whole_usdc(base_units: int) -> str:
    """Base units of USDC in whole units, as the client writes them: no trailing zeros."""
    whole, fraction = divmod(base_units, 10**6)
    return f"{whole}.{fraction:06d}".rstrip("0").rstrip(".")


async def test_an_agent_holding_only_usdc_sends_some(chain: LocalChain, served: Served) -> None:
    """The agent's wallet, never used and holding no ETH, sends 2.5 USDC through
    send_usdc_gasless: the recipient gains it, Bruno's payee gains what the agent said it paid,
    and the agent holds the rest, still no ETH."""
    agent = fresh_client("an AgentKit agent")
    recipient = fresh_client("an AgentKit agent's recipient").address
    await chain.mint(USDC, agent.address, HELD)
    before = await chain.balance_of(USDC, served.payee)

    status, out, err = await run_agent(
        {
            "BRUNO_URL": str(served.http.base_url).rstrip("/"),
            "RPC_URL": chain.url,
            "CHAIN_ID": str(served.chain_id),
            "USDC": USDC.address,
            "KEY": "0x" + agent.key.hex(),
            "RECIPIENT": recipient,
            "AMOUNT": AMOUNT,
            "MAX_FEE": MAX_FEE,
        },
        AGENT_SECONDS,
        served.land,
    )

    assert status == 0, f"{out}\n{err}"
    said = json.loads(out.strip().splitlines()[-1])["said"]
    paid = await chain.balance_of(USDC, served.payee) - before
    assert await chain.balance_of(USDC, recipient) == SENT
    assert 0 < paid <= int(MAX_FEE) * 10**6
    assert await chain.balance_of(USDC, agent.address) == HELD - SENT - paid
    assert f"paying Bruno {whole_usdc(paid)} USDC" in said, said
    assert int(await chain.rpc("eth_getBalance", [agent.address, "latest"]), 16) == 0


@pytest.mark.parametrize("deployed", [False, True], ids=["undeployed", "deployed"])
@pytest.mark.parametrize(
    ("version", "factory"),
    [("1", smartwallet.FACTORY), ("1.1", SMART_WALLET_FACTORY_1_1), ("kernel", None)],
    ids=["v1", "v1.1", "kernel"],
)
async def test_a_smart_wallet_sends_some(
    chain: LocalChain,
    served: Served,
    deployed: bool,
    version: str,
    factory: ChecksumAddress | None,
) -> None:
    """The agent's wallet a smart account its key owns, holding only USDC: a Coinbase Smart
    Wallet of `version` (viem's toCoinbaseSmartAccount), or ZeroDev's Kernel v3.2 as AgentKit's
    ZeroDevWalletProvider makes it. It sends 2.5 USDC through send_usdc_gasless; the recipient
    gains it, and the wallet holds the rest. Undeployed, its ERC-6492 signatures deploy it in the
    same transaction that pays Bruno and makes the transfer; deployed, it signs as ERC-1271 has
    it."""
    case = f"{version}, deployed: {deployed}"
    owner = fresh_client(f"an AgentKit agent's smart wallet owner, {case}")
    if factory is None:
        wallet = await (kernel.create if deployed else kernel.address_of)(chain, owner)
    elif deployed:
        wallet = await smartwallet.create(chain, owner, factory=factory)
    else:
        wallet = await smartwallet.address_of(chain, owner, factory=factory)
    recipient = fresh_client(f"an AgentKit smart wallet's recipient, {case}").address
    await chain.mint(USDC, wallet, HELD)
    before = await chain.balance_of(USDC, served.payee)

    status, out, err = await run_agent(
        {
            "BRUNO_URL": str(served.http.base_url).rstrip("/"),
            "RPC_URL": chain.url,
            "CHAIN_ID": str(served.chain_id),
            "USDC": USDC.address,
            "KEY": "0x" + owner.key.hex(),
            "SMART": version,
            "RECIPIENT": recipient,
            "AMOUNT": AMOUNT,
            "MAX_FEE": MAX_FEE,
        },
        AGENT_SECONDS,
        served.land,
    )

    assert status == 0, f"{out}\n{err}"
    paid = await chain.balance_of(USDC, served.payee) - before
    assert await chain.balance_of(USDC, recipient) == SENT
    assert 0 < paid <= int(MAX_FEE) * 10**6
    assert await chain.balance_of(USDC, wallet) == HELD - SENT - paid
    assert await chain.code(wallet) != "0x"
