"""Live testnets, for the tests that spend real (testnet) funds: each network's wallet, its
nodes, and the contracts the relay needs there.

The wallet is the float: the payee, and the sender of every funding transaction. The live
tests' concurrency accounts and clients derive from its key (bruno.accounts). Nothing is
left as dust: a concurrency account's leftover gas money is its working balance, a client is
given exactly what it spends, and `make <network>-sweep` drains the derived accounts back
into the float to exactly 0.

Keys live in .secrets/<network>/, never committed and apart from dist/, which `make
distclean` removes: they hold funds. Each is created the first time it's needed and kept, so
nothing is ever left with a key that's gone. <NETWORK>_RPC_URLS (comma-separated) picks a
network's nodes.

The wallet holds the network's ETH, topped up from a faucet. `make <network>-wallet`
creates it if need be and says where to send it; `make <network>-deploy` deploys, from it,
what the network lacks: the runner's deployer, Calibur and x402's Permit2 proxy, each through
Arachnid's deployer at its address everywhere.

    python -m bruno.tests.live <hoodi | sepolia | unichain-sepolia | base-sepolia> [deploy | sweep]
"""

import asyncio
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress

from bruno.accounts import derive
from bruno.chain import Chain, ChainError
from bruno.deployer import ARACHNID, DEPLOYER, RUNTIME, arachnid_calldata, deployed_code
from bruno.maintenance import sweep as sweep_accounts
from bruno.transactions import sign, sign_drain

SECRETS = Path(__file__).parents[2] / ".secrets"
FIXTURES = Path(__file__).parent / "fixtures" / "evm"
# the slot time, and how long a sent transaction is waited for: an engineering bound, twenty
# slots
SLOT_SECONDS = 12
RECEIPT_TIMEOUT_SECONDS = 20 * SLOT_SECONDS
# the priority fee the live tests pay: a choice, well over what Hoodi's nodes suggest
# (0.06 gwei on 2026-10-19), so a builder has reason to include them
PRIORITY_FEE = 10**9
# the live tests' accounts, derived from the float's key: concurrency accounts 0 and 1, the
# client that pays a wei short (it never sends, so it's the same one every run), and the
# paying clients from PAYER_INDEX on, each run taking the first that has never sent
CONCURRENCY_ACCOUNTS = 2
SHORT_CLIENT_INDEX = 1000
PAYER_INDEX = 1001
# the key that signs requests to builders that want an X-Flashbots-Signature: an identity,
# holding nothing
BUILDER_IDENTITY_INDEX = 999


@dataclass(frozen=True, slots=True)
class Network:
    """A live network; `wallet_of` the network whose wallet it shares (its own by default):
    one key, funded on each."""

    name: str
    chain_id: int
    rpc_urls: tuple[str, ...]
    wallet_of: str = ""

    @property
    def keys(self) -> Path:
        return SECRETS / (self.wallet_of or self.name)

    @property
    def wallet_key(self) -> Path:
        return self.keys / "wallet.key"

    def chain(self, client: httpx.AsyncClient) -> Chain:
        return Chain(client, self.chain_id, self.rpc_urls)


def _rpc_urls(variable: str, default: str) -> tuple[str, ...]:
    return tuple(os.environ.get(variable, default).split(","))


# chain ids from github.com/eth-clients/{hoodi,sepolia}; the nodes public ones, each
# answering with its network's chain id on 2026-10-19
HOODI = Network(
    "hoodi",
    560_048,
    _rpc_urls(
        "HOODI_RPC_URLS",
        "https://ethereum-hoodi-rpc.publicnode.com,https://rpc.hoodi.ethpandaops.io",
    ),
)
SEPOLIA = Network(
    "sepolia",
    11_155_111,
    _rpc_urls(
        "SEPOLIA_RPC_URLS",
        "https://ethereum-sepolia-rpc.publicnode.com,https://rpc.sepolia.ethpandaops.io",
    ),
)
# Unichain Sepolia (OP stack): chain id and node from docs.unichain.org, answering 1301 on
# 2026-10-23; the float is Sepolia's
UNICHAIN_SEPOLIA = Network(
    "unichain-sepolia",
    1301,
    _rpc_urls("UNICHAIN_SEPOLIA_RPC_URLS", "https://sepolia.unichain.org"),
    wallet_of="sepolia",
)
# Base Sepolia (OP stack): chain id and node from docs.base.org, answering 84532 on 2026-10-23;
# the float is Sepolia's
BASE_SEPOLIA = Network(
    "base-sepolia",
    84_532,
    _rpc_urls("BASE_SEPOLIA_RPC_URLS", "https://sepolia.base.org"),
    wallet_of="sepolia",
)
NETWORKS = {n.name: n for n in (HOODI, SEPOLIA, UNICHAIN_SEPOLIA, BASE_SEPOLIA)}


def key(path: Path) -> LocalAccount:
    """A key kept on disk, never committed: created the first time it's needed."""
    if not path.exists():
        for directory in (path.parent.parent, path.parent):  # .secrets/, then the network's
            directory.mkdir(mode=0o700, exist_ok=True)
        path.write_text(os.urandom(32).hex() + "\n")
        path.chmod(0o600)
    return Account.from_key(bytes.fromhex(path.read_text().strip()))


async def fund(chain: Chain, float_: LocalAccount, to: ChecksumAddress, target: int) -> None:
    """Top `to` up from the float to `target`, if it holds less."""
    held = await chain.balance(to)
    short = target - held
    if short <= 0:
        return
    empty = held == 0 and await chain.nonce(to) == 0 and await chain.code(to) == "0x"
    gas = (await chain.schedule()).transfer(new_recipient=empty)
    nonce = await chain.nonce(float_.address)
    fees = await chain.fees(PRIORITY_FEE)
    tx = sign(float_, chain.chain_id, nonce, fees, gas, to, b"", short)
    await chain.send(tx, f"funding {to}", RECEIPT_TIMEOUT_SECONDS)


async def sweep(chain: Chain, float_: LocalAccount) -> None:
    """Drain the live tests' concurrency accounts and the short-paying client into the float,
    to exactly 0 (a paying client spends all it's given); on an OP-stack chain, to what each
    drain's fee beyond gas leaves."""
    gas_price = (await chain.fees(PRIORITY_FEE)).max_fee_per_gas
    schedule = await chain.schedule()
    data_fee = (await chain.latest(await chain.op_stack())).data_fee
    await sweep_accounts(
        chain,
        float_,
        range(CONCURRENCY_ACCOUNTS),
        gas_price,
        schedule,
        RECEIPT_TIMEOUT_SECONDS,
        data_fee,
    )
    short = derive(float_, SHORT_CLIENT_INDEX)
    balance = await chain.balance(short.address)
    nonce = await chain.nonce(short.address)
    gas = schedule.transfer(new_recipient=False)  # the float exists
    tx = sign_drain(short, chain.chain_id, nonce, balance, gas_price, float_.address, gas, data_fee)
    if tx is not None:
        await chain.send(tx, "draining the short-paying client", RECEIPT_TIMEOUT_SECONDS)
    elif balance:
        raise ChainError(f"the short-paying client holds {balance} wei, too little to send")


async def deploy_through_arachnid(
    chain: Chain, wallet: LocalAccount, calldata: bytes, address: ChecksumAddress, what: str
) -> None:
    """Send `calldata` to Arachnid's deployer from the wallet, unless `address` has code."""
    if await chain.code(address) not in ("0x", "0x0"):
        print(f">> {what} already at {address}")
        return
    q = {"from": wallet.address, "to": ARACHNID, "data": "0x" + calldata.hex()}
    gas = int(await chain.rpc("eth_estimateGas", [q]), 16)
    nonce = await chain.nonce(wallet.address)
    tx = sign(
        wallet, chain.chain_id, nonce, await chain.fees(PRIORITY_FEE), gas, ARACHNID, calldata
    )
    receipt = await chain.send(tx, f"{what}'s deployment", RECEIPT_TIMEOUT_SECONDS)
    paid = int(receipt["gasUsed"], 16) * int(receipt["effectiveGasPrice"], 16)
    print(f">> {what} deployed at {address}: 0x{tx.hash.hex()}, {paid / 10**18} ETH")


# what `deploy` deploys from the fixtures, after the runner's deployer: each a manifest name
# with its deployment's calldata (`<name>.deploy.hex`) and the runtime it must leave
_DEPLOYED = (("calibur", "Calibur"), ("x402_exact_permit2_proxy", "x402's Permit2 proxy"))


async def deploy(chain: Chain, wallet: LocalAccount) -> None:
    """The runner's deployer, Calibur and x402's Permit2 proxy, each checked against the
    runtime we test with."""
    await deploy_through_arachnid(chain, wallet, arachnid_calldata(), DEPLOYER, "the deployer")
    if await deployed_code(chain, DEPLOYER, RECEIPT_TIMEOUT_SECONDS) != "0x" + RUNTIME.hex():
        raise ChainError(f"{DEPLOYER} doesn't hold the deployer's runtime")
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    for name, what in _DEPLOYED:
        address = manifest["deployments"][name]["address"]
        calldata = bytes.fromhex((FIXTURES / f"{name}.deploy.hex").read_text().strip()[2:])
        await deploy_through_arachnid(chain, wallet, calldata, address, what)
        deployed = bytes.fromhex((await deployed_code(chain, address, RECEIPT_TIMEOUT_SECONDS))[2:])
        if hashlib.sha256(deployed).hexdigest() != manifest["contracts"][name]["sha256"]:
            raise ChainError(f"{what}'s code there isn't the fixture's")


async def _main(network: Network, command: str | None) -> None:
    wallet = key(network.wallet_key)
    async with httpx.AsyncClient() as client:
        chain = network.chain(client)
        await chain.check_chain()
        if command == "deploy":
            await deploy(chain, wallet)
        elif command == "sweep":
            await sweep(chain, wallet)
        balance = await chain.balance(wallet.address) / 10**18
        print(f">> {network.name} wallet {wallet.address}: {balance} ETH")


if __name__ == "__main__":
    asyncio.run(_main(NETWORKS[sys.argv[1]], sys.argv[2] if len(sys.argv) > 2 else None))
