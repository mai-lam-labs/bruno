"""Live testnets, for the tests that spend real (testnet) funds: each network's wallet, its
nodes, and the contracts the relay needs there.

The wallet is the float: the payee, and the sender of every funding transaction. The live
tests' concurrency accounts and clients derive from its key (bruno.accounts). Nothing is
left as dust: a concurrency account's leftover gas money is its working balance, a client is
given exactly what it spends, and `make <network>-sweep` drains the derived accounts back
into the float to exactly 0.

Keys live in .secrets/<network>/, never committed and apart from dist/, which `make
distclean` removes: they hold funds. Each is created the first time it's needed and kept, so
nothing is ever left with a key that's gone. A network's nodes are the deployment's
(config.default.json, config.local.json merged over it), unless <NETWORK>_RPC_URLS
(comma-separated) picks others; a network neither configures has public ones.

The wallet holds the network's ETH, topped up from a faucet, and is the live tests' alone:
never a deployment's float or payee. Any token a test pays in, it buys with that ETH and keeps
between runs, selling back what it no longer needs (`hold`). Nothing it would spend goes out
unless it can pay (WalletShortError), and `make check-<network>` ends by sweeping and saying how
many more runs the wallet can afford. `make <network>-wallet` creates it if need be and shows
where to send it, with a QR code; `make <network>-deploy` deploys, from it,
what the network lacks: the runner's deployer, Calibur and x402's Permit2 proxy, each through
Arachnid's deployer at its address everywhere.

    python -m bruno.tests.live <hoodi | sepolia | unichain-sepolia | base-sepolia>
        [deploy | balance | sweep [WEI_BEFORE_THE_RUN]]
"""

import asyncio
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from fractions import Fraction
from math import ceil, floor
from pathlib import Path

import httpx
import msgspec
import segno
from eth_abi.abi import encode
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from bruno.accounts import derive
from bruno.chain import Chain, ChainError
from bruno.common import BrunoError
from bruno.config import Config, Hop, PoolKind, merged
from bruno.deployer import ARACHNID, DEPLOYER, RUNTIME, arachnid_calldata, deployed_code
from bruno.maintenance import sweep as sweep_accounts
from bruno.net.jsonrpc import Node
from bruno.prices import Prices
from bruno.router import Step, approve_calldata, swap_calldata
from bruno.tests.verify import GAS_HEADROOM, balance_of
from bruno.transactions import sign, sign_drain

ROOT = Path(__file__).parents[2]
SECRETS = ROOT / ".secrets"
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
# a token the tests pay in, kept by the float between runs: topped up to HELD_RUNS runs' worth
# when it holds less than one, the surplus over HELD_RUNS swapped back to ETH once it holds
# more than SURPLUS_RUNS, so what the runs take from the pool goes back to it; each swap at
# most SLIPPAGE under the pool's spot price, beyond its fee tier (our amounts are small against
# a pool's depth)
HELD_RUNS = 2
SURPLUS_RUNS = 4
SLIPPAGE = Fraction(101, 100)
# when the run's end warns that the wallet needs a faucet: fewer runs left than this, at what
# this run spent
LOW_RUNS = 5
_EXACT_INPUT = keccak(text="exactInput((bytes,address,uint256,uint256))")[:4]
_FEE = keccak(text="fee()")[:4]
_TOKEN0 = keccak(text="token0()")[:4]
_TOKEN1 = keccak(text="token1()")[:4]
_ALLOWANCE = keccak(text="allowance(address,address)")[:4]


class WalletShortError(BrunoError):
    """The wallet can't pay for what a run needs: it wants ETH from a faucet."""


@dataclass(frozen=True, slots=True)
class Network:
    """A live network; `wallet_of` the network whose wallet it shares (its own by default):
    one key, funded on each."""

    name: str
    chain_id: int
    nodes: tuple[str | Node, ...]
    wallet_of: str = ""

    @property
    def keys(self) -> Path:
        return SECRETS / (self.wallet_of or self.name)

    @property
    def wallet_key(self) -> Path:
        return self.keys / "wallet.key"

    def chain(self, client: httpx.AsyncClient) -> Chain:
        return Chain(client, self.chain_id, self.nodes)


def _configured() -> dict[int, tuple[str | Node, ...]]:
    """Each chain's nodes as the deployment has them: config.default.json, with
    config.local.json (gitignored: nodes with API keys) merged over it if there is one."""
    paths = [ROOT / "config.default.json", ROOT / "config.local.json"]
    documents = [msgspec.json.decode(p.read_bytes()) for p in paths if p.exists()]
    result = documents[0]
    for document in documents[1:]:
        result = merged(result, document)
    config = msgspec.json.decode(msgspec.json.encode(result), type=Config)
    return {chain_id: chain.rpc for chain_id, chain in config.chains.items()}


_CONFIGURED = _configured()


def _nodes(variable: str, chain_id: int, default: str) -> tuple[str | Node, ...]:
    """A network's nodes: `variable`'s (comma-separated URLs), else the configuration's, else
    `default`'s, public ones."""
    if variable in os.environ:
        return tuple(os.environ[variable].split(","))
    return _CONFIGURED.get(chain_id) or tuple(default.split(","))


# chain ids from github.com/eth-clients/{hoodi,sepolia}; the default nodes public ones, each
# answering with its network's chain id on 2026-10-19
HOODI = Network(
    "hoodi",
    560_048,
    _nodes(
        "HOODI_RPC_URLS",
        560_048,
        "https://ethereum-hoodi-rpc.publicnode.com,https://rpc.hoodi.ethpandaops.io",
    ),
)
SEPOLIA = Network(
    "sepolia",
    11_155_111,
    _nodes(
        "SEPOLIA_RPC_URLS",
        11_155_111,
        "https://ethereum-sepolia-rpc.publicnode.com,https://rpc.sepolia.ethpandaops.io",
    ),
)
# Unichain Sepolia (OP stack): chain id and node from docs.unichain.org, answering 1301 on
# 2026-10-23; the float is Sepolia's
UNICHAIN_SEPOLIA = Network(
    "unichain-sepolia",
    1301,
    _nodes("UNICHAIN_SEPOLIA_RPC_URLS", 1301, "https://sepolia.unichain.org"),
    wallet_of="sepolia",
)
# Base Sepolia (OP stack): chain id and node from docs.base.org, answering 84532 on 2026-10-23;
# the float is Sepolia's
BASE_SEPOLIA = Network(
    "base-sepolia",
    84_532,
    _nodes("BASE_SEPOLIA_RPC_URLS", 84_532, "https://sepolia.base.org"),
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


def funding_wanted(address: str, chain_id: int, held: int, wanted: int) -> str:
    """What to tell the user when the wallet at `address` holds `held` wei and wants `wanted`:
    the faucet, and the make target with its QR code."""
    name = next(n.wallet_of or n.name for n in NETWORKS.values() if n.chain_id == chain_id)
    return (
        f"the {name} wallet {address} holds {held / 10**18} ETH and wants {wanted / 10**18}: "
        f"fund it from a faucet (`make {name}-wallet` shows its QR code)"
    )


async def afford(chain: Chain, float_: LocalAccount, cost: int) -> None:
    """Raise WalletShortError before the float sends what costs it `cost` wei and it holds less."""
    held = await chain.balance(float_.address)
    if held < cost:
        raise WalletShortError(funding_wanted(float_.address, chain.chain_id, held, cost))


async def fund(chain: Chain, float_: LocalAccount, to: ChecksumAddress, target: int) -> None:
    """Top `to` up from the float to `target`, if it holds less."""
    held = await chain.balance(to)
    short = target - held
    if short <= 0:
        return
    empty = held == 0 and await chain.nonce(to) == 0 and await chain.code(to) == "0x"
    gas = (await chain.schedule()).transfer(new_recipient=empty)
    fees = await chain.fees(PRIORITY_FEE)
    await afford(chain, float_, short + gas * fees.max_fee_per_gas)
    nonce = await chain.nonce(float_.address)
    tx = sign(float_, chain.chain_id, nonce, fees, gas, to, b"", short)
    await chain.send(tx, f"funding {to}", RECEIPT_TIMEOUT_SECONDS)


async def _send_measured(
    chain: Chain, float_: LocalAccount, to: ChecksumAddress, data: bytes, value: int, what: str
) -> None:
    """The float's call of `data` to `to` with `value`, its gas measured, once it can pay."""
    query = {"from": float_.address, "to": to, "data": "0x" + data.hex(), "value": hex(value)}
    gas = ceil(int(await chain.rpc("eth_estimateGas", [query]), 16) * GAS_HEADROOM)
    fees = await chain.fees(PRIORITY_FEE)
    await afford(chain, float_, value + gas * fees.max_fee_per_gas)
    nonce = await chain.nonce(float_.address)
    tx = sign(float_, chain.chain_id, nonce, fees, gas, to, data, value)
    await chain.send(tx, what, RECEIPT_TIMEOUT_SECONDS)
    print(f"  {what}: 0x{tx.hash.hex()}")


async def hold(
    chain: Chain,
    float_: LocalAccount,
    token: ChecksumAddress,
    pool: Hop,
    weth: ChecksumAddress,
    router: ChecksumAddress,
    run: int,
) -> None:
    """The float holding `run` of `token`, a run's worth, before a test pays it out (and gets
    it back): ETH swapped for HELD_RUNS runs' worth through `pool` (a Uniswap v3 pool of
    `token` and WETH) and Uniswap's SwapRouter02 at `router` when it holds less; the surplus
    over HELD_RUNS swapped back when it holds more than SURPLUS_RUNS."""
    if pool.kind is not PoolKind.UNISWAP_V3:
        raise ChainError(f"{token}: only a Uniswap v3 pool to WETH is swapped through")
    held = await balance_of(chain, token, float_.address)
    if run <= held <= SURPLUS_RUNS * run:
        return
    to_weth = Step(PoolKind.UNISWAP_V3, token, weth, await _fee(chain, pool.pool, token, weth))
    wei = await Prices(chain, weth, {token: (pool,)}, 0).wei(token)
    if held < run:
        wanted = HELD_RUNS * run - held
        paid = ceil(wanted * wei * SLIPPAGE / (1 - Fraction(to_weth.fee, 10**6)))
        path = bytes.fromhex(weth[2:]) + to_weth.fee.to_bytes(3) + bytes.fromhex(token[2:])
        data = _EXACT_INPUT + encode(
            ["(bytes,address,uint256,uint256)"], [(path, float_.address, paid, wanted)]
        )
        await _send_measured(
            chain, float_, router, data, paid, f"bought {wanted} of {token} for {paid} wei"
        )
        return
    surplus = held - HELD_RUNS * run
    least = floor(surplus * wei * (1 - Fraction(to_weth.fee, 10**6)) / SLIPPAGE)
    allowance = await chain.call(
        token,
        _ALLOWANCE
        + bytes(12)
        + bytes.fromhex(float_.address[2:])
        + bytes(12)
        + bytes.fromhex(router[2:]),
    )
    if int.from_bytes(allowance) < surplus:
        approve = approve_calldata(router, surplus)
        await _send_measured(chain, float_, token, approve, 0, f"the router may spend {token}")
    data = swap_calldata([to_weth], surplus, least, float_.address)
    await _send_measured(
        chain, float_, router, data, 0, f"sold {surplus} of {token} for {least} wei at least"
    )


async def _fee(chain: Chain, pool: str, token: ChecksumAddress, weth: ChecksumAddress) -> int:
    """The fee tier of the v3 `pool`, which must trade `token` for `weth`."""
    traded = {to_checksum_address((await chain.call(pool, c))[12:]) for c in (_TOKEN0, _TOKEN1)}
    if traded != {token, weth}:
        raise ChainError(f"pool {pool} doesn't trade {token} for WETH")
    return int.from_bytes(await chain.call(pool, _FEE))


async def sweep(chain: Chain, float_: LocalAccount) -> None:
    """Drain the live tests' concurrency accounts, the short-paying client and any paying
    client a failed run left holding ETH into the float, to exactly 0 (a paying client spends
    all it's given when its run succeeds); on an OP-stack chain, to what each drain's fee beyond
    gas leaves."""
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
    gas = schedule.transfer(new_recipient=False)  # the float exists
    # the short-paying client, and the paying clients a failed run funded and left
    clients = [(derive(float_, SHORT_CLIENT_INDEX), "the short-paying client")]
    i = PAYER_INDEX
    while not await fresh(chain, (payer := derive(float_, i)).address):
        clients.append((payer, f"paying client {i}"))
        i += 1
    for client, name in clients:
        balance = await chain.balance(client.address)
        if not balance:
            continue
        nonce = await chain.nonce(client.address)
        tx = sign_drain(
            client, chain.chain_id, nonce, balance, gas_price, float_.address, gas, data_fee
        )
        if tx is None:
            raise ChainError(f"{name} holds {balance} wei, too little to send")
        await chain.send(tx, f"draining {name}", RECEIPT_TIMEOUT_SECONDS)


async def fresh(chain: Chain, client: str) -> bool:
    """Whether a paying client is unused: it has never sent or been delegated, and holds no
    ETH."""
    return (
        await chain.nonce(client) == 0
        and await chain.code(client) == "0x"
        and await chain.balance(client) == 0
    )


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


def show(address: str, chain_id: int) -> None:
    """A QR code of `address` on its chain (EIP-681), to fund it from a phone's wallet."""
    segno.make(f"ethereum:{address}@{chain_id}").terminal(compact=True)


async def _main(network: Network, command: str | None, since: int | None) -> None:
    wallet = key(network.wallet_key)
    async with httpx.AsyncClient() as client:
        chain = network.chain(client)
        await chain.check_chain()
        if command == "balance":  # in wei, for the run's report after it (make check-NETWORK)
            print(await chain.balance(wallet.address))
            return
        if command == "deploy":
            await deploy(chain, wallet)
        elif command == "sweep":
            await sweep(chain, wallet)
        held = await chain.balance(wallet.address)
        print(f">> {network.name} wallet {wallet.address}: {held / 10**18} ETH")
        if command is None:
            show(wallet.address, network.chain_id)
        elif since is not None and (spent := since - held) > 0:
            runs = held // spent
            print(f">> the run spent {spent / 10**18} ETH: {runs} more like it")
            if runs < LOW_RUNS:
                print(
                    ">> " + funding_wanted(wallet.address, network.chain_id, held, LOW_RUNS * spent)
                )
                show(wallet.address, network.chain_id)


if __name__ == "__main__":
    asyncio.run(
        _main(
            NETWORKS[sys.argv[1]],
            sys.argv[2] if len(sys.argv) > 2 else None,
            int(sys.argv[3]) if len(sys.argv) > 3 else None,
        )
    )
