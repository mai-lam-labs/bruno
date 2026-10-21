"""The deployer deployed where a network lacks it (make NETWORK-deployer-deploy), on anvil:
through Arachnid's, from a funded key, its code checked; nothing sent where it's there."""

import httpx
import pytest
from eth_account import Account
from eth_utils.crypto import keccak

from bruno.chain import Chain
from bruno.deployer import DEPLOYER, RUNTIME, DeployerError, deploy
from bruno.forks import GasSchedule
from bruno.tests.localchain import LocalChain

WALLET = Account.from_key(keccak(text="the deployer test's wallet"))


async def without_deployer(chain: LocalChain) -> None:
    """The deployer's address as on a network that lacks it: no code, and no nonce (a contract's
    is 1, and CREATE2 can't deploy over an account with one)."""
    await chain.rpc("anvil_setCode", [DEPLOYER, "0x"])
    await chain.rpc("anvil_setNonce", [DEPLOYER, "0x0"])


async def test_deploys_where_missing_and_not_again(
    chain: LocalChain, schedule: GasSchedule
) -> None:
    await without_deployer(chain)
    await chain.set_eth_balance(WALLET.address, 10**18)
    async with httpx.AsyncClient() as client:
        live = Chain(client, await chain.chain_id(), (chain.url,))
        sent = await deploy(live, WALLET)
        again = await deploy(live, WALLET)

    assert sent is not None
    assert again is None
    assert await chain.code(DEPLOYER) == "0x" + RUNTIME.hex()


async def test_an_unfunded_wallet_sends_nothing(chain: LocalChain, schedule: GasSchedule) -> None:
    await without_deployer(chain)
    async with httpx.AsyncClient() as client:
        live = Chain(client, await chain.chain_id(), (chain.url,))
        with pytest.raises(DeployerError, match="under the"):
            await deploy(live, Account.create())

    assert await chain.code(DEPLOYER) == "0x"
