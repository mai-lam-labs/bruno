"""A deployed relay verified with real transactions (make NETWORK-verify): a payment that
lands, and one a unit short that the simulation refuses, through the relay's own URL; and, where
it takes x402, a call paid through its challenge that lands too.

The client is the network's own verify key (.secrets/NETWORK/verify.key, created the first
time), holding nothing but the token: its call is the token's transferWithAuthorization
(EIP-3009) of the amount to the payee, which anyone may submit, so it needs no ETH and no
EIP-7702 delegation. The payee funds it with exactly what's missing, and the payment returns
it: what's spent is gas. Keys are read from .secrets/, only addresses printed.

    python -m bruno.tests.verify NETWORK URL CONFIG

How the client is funded is set by the network, here and nowhere else. On Ethereum the money is
real and the pools price it truly: the payee funds the client from what it holds, and a run
the relay quotes over CAPS' whole tokens is refused before anything is sent. On a test network
(TESTNETS) its pools may price it as they will, so nothing is capped: what the payee lacks it
buys with the network's ETH through the token's own pool, the one the relay quotes by, so
what's bought matches what's asked. Any other network isn't verified.
"""

import asyncio
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from decimal import Decimal
from math import ceil
from pathlib import Path

import httpx
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address

from bruno.abi import BALANCE_OF, address_word, selector
from bruno.accounts import new_key, read_key
from bruno.chain import Chain, lasting_fees, market_tip
from bruno.common import BrunoError
from bruno.config import ChainConfig, Eip712Domain, Hop, Token, load
from bruno.eip3009 import TransferAuthorization, measured, measuring, transfer_gas
from bruno.forks import NETWORK_NAMES
from bruno.runner import UNDERPAID
from bruno.tests.live import GAS_HEADROOM, WalletShortError, balance_of, buy, show
from bruno.tests.transfers import NEVER, by_key, decoded, payment_header, signed
from bruno.transactions import sign

SECRETS = Path(__file__).parents[2] / ".secrets"
CALL_BYTES = 4 + 9 * 32
DEADLINE_BLOCKS = 10
# how much longer than maxTimeoutSeconds an x402 request is waited for: its answer's way back
HELD_SLACK_SECONDS = 30
# the payee's funding transfer: its max fee lasting this many blocks
FUNDING_BLOCKS = 5
# on a test network, what the payee buys when it lacks what the client is funded with: this
# many times what's missing, so the next runs needn't buy again
BOUGHT_MULTIPLIER = Decimal("1.5")
_TRANSFER = selector("transfer(address,uint256)")


class VerifyError(BrunoError):
    pass


@dataclass(frozen=True, slots=True)
class Capped:
    """Funding on a network whose money is real: the payee funds the client from what it holds,
    and the relay may ask at most `most` of the token (in its units) for the run."""

    most: int


@dataclass(frozen=True, slots=True)
class Swapped:
    """Funding on a test network: what the payee lacks bought with its ETH through `pool` (a
    Uniswap v3 pool of the token and `weth`) and Uniswap's SwapRouter02 at `router`."""

    pool: Hop
    weth: ChecksumAddress
    router: ChecksumAddress


# the networks whose money is real, the most a run may cost there in whole tokens
CAPS = {1: Decimal(5)}
# the test networks: Sepolia, and anvil's (make anvil-verify)
TESTNETS = frozenset({11_155_111, 31_337})


@dataclass(frozen=True, slots=True)
class Verified:
    transaction_hash: str
    block_number: int
    paid: int
    # the x402 payment's transaction, where the relay takes x402
    x402_transaction_hash: str | None = None


async def authorized(
    client: LocalAccount,
    domain: Eip712Domain,
    chain_id: int,
    token: ChecksumAddress,
    payee: ChecksumAddress,
    value: int,
) -> TransferAuthorization:
    """`client`'s authorization of `value` of `token` to `payee`, never expiring, signed by its
    key."""
    return await signed(
        by_key(client), domain, chain_id, token, client.address, payee, value, NEVER
    )


async def call_gas(
    chain: Chain, domain: Eip712Domain, token: ChecksumAddress, payee: ChecksumAddress
) -> int:
    """The gas the client's call (its transferWithAuthorization to the payee) is given: as the
    relay bounds an x402 transfer, a zero-value one measured, and the balance writes added at
    the fork's price, the payee's slot set if it holds none."""
    probe, data = measuring(domain, chain.chain_id, token, payee)
    schedule = await chain.schedule()
    execution = measured(int(await chain.rpc(probe.method, probe.params), 16), data, schedule)
    return transfer_gas(execution, schedule, await balance_of(chain, token, payee) > 0)


def submission(
    transfer: TransferAuthorization, token: ChecksumAddress, amount: int, deadline: int, gas: int
) -> dict[str, object]:
    return {
        "call": {"to": token, "data": "0x" + transfer.calldata().hex()},
        "gas": hex(gas),
        "payment": {"token": token, "payee": transfer.to, "amount": hex(amount)},
        "maxBlockNumber": hex(deadline),
    }


async def fund(
    chain: Chain,
    payee: LocalAccount,
    token: ChecksumAddress,
    to: str,
    amount: int,
    wait: float,
) -> None:
    """`to` given `amount` of `token` by the payee: a plain transfer, its gas measured."""
    data = _TRANSFER + address_word(to) + amount.to_bytes(32)
    query = {"from": payee.address, "to": token, "data": "0x" + data.hex()}
    gas = ceil(int(await chain.rpc("eth_estimateGas", [query]), 16) * GAS_HEADROOM)
    latest = await chain.latest()
    fees = lasting_fees(latest, market_tip(latest.tips), FUNDING_BLOCKS)
    nonce = await chain.nonce(payee.address)
    tx = sign(payee, chain.chain_id, nonce, fees, gas, token, data)
    receipt = await chain.send(tx, "the client's funding", wait)
    paid = int(receipt["gasUsed"], 16) * int(receipt["effectiveGasPrice"], 16)
    print(f">> funded {to} with {amount}: 0x{tx.hash.hex()}, {paid / 10**18} ETH")


async def verify(
    http: httpx.AsyncClient,
    path: str,
    chain: Chain,
    client: LocalAccount,
    payee: LocalAccount,
    token: ChecksumAddress,
    domain: Eip712Domain,
    next_block: Callable[[], Awaitable[object]],
    funding_wait: float,
    funding: Capped | Swapped,
) -> Verified:
    """Through the relay at `http` (its chain at `path`): the client funded to the quote, a
    payment a unit short refused with ERCNNNN_Underpaid(), then the quote paid and landed, the
    payee gaining it. `next_block` waits for, or makes, the next block. `funding` is the
    network's: Capped refuses a run quoted over its most before anything is sent, Swapped buys
    what the payee lacks."""
    terms = (await http.get(f"{path}/v1/config")).json()
    if to_checksum_address(terms["payee"]) != payee.address:
        raise VerifyError(f"the relay's payee is {terms['payee']}, not {payee.address}")
    gas = await call_gas(chain, domain, token, payee.address)
    params = {"gas": hex(gas), "callDataLength": hex(CALL_BYTES), "tokens": [token]}
    quoted = await http.post(f"{path}/v1/quote", json=params)
    if quoted.status_code != 200:
        raise VerifyError(f"quote: {quoted.status_code} {quoted.text}")
    amount = int(quoted.json()["quotes"][0]["amount"], 16)
    # x402, where it's taken: its challenge for a call that succeeds for anyone
    x402_call = {
        "call": {"to": token, "data": "0x" + (BALANCE_OF + address_word(client.address)).hex()},
        "gas": hex(gas),
    }
    challenged = await http.post(f"{path}/v1/x402", json=x402_call)
    accepts = None
    if challenged.status_code == 402:
        accepts = next(a for a in challenged.json()["accepts"] if a["asset"] == token)
    x402_amount = 0 if accepts is None else int(accepts["amount"])
    print(f">> client {client.address}; quoted {amount}, over x402 {x402_amount} of {token}")
    asked = amount + x402_amount
    if isinstance(funding, Capped) and asked > funding.most:
        raise VerifyError(f"the relay asks {asked}, over the {funding.most} allowed")
    missing = asked - await balance_of(chain, token, client.address)
    if missing > 0:
        stock = await balance_of(chain, token, payee.address)
        if stock < missing and isinstance(funding, Swapped):
            bought = ceil(missing * BOUGHT_MULTIPLIER) - stock
            await buy(chain, payee, token, funding.pool, funding.weth, funding.router, bought)
            stock += bought
        if stock < missing:
            raise VerifyError(f"the payee holds {stock} of {token}, short of {missing}")
        await fund(chain, payee, token, client.address, missing, funding_wait)

    short = await authorized(client, domain, chain.chain_id, token, payee.address, amount - 1)
    head = await chain.head()
    refused = await http.post(
        f"{path}/v1/submissions", json=submission(short, token, amount, head + DEADLINE_BLOCKS, gas)
    )
    revert = refused.json().get("error", {}).get("data", {})
    if refused.status_code != 422 or revert.get("revertData") != "0x" + UNDERPAID.hex():
        raise VerifyError(f"a unit short wasn't refused as underpaid: {refused.text}")
    print(">> a unit short: refused, ERCNNNN_Underpaid()")

    before = await balance_of(chain, token, payee.address)
    paying = await authorized(client, domain, chain.chain_id, token, payee.address, amount)
    head = await chain.head()
    taken = await http.post(
        f"{path}/v1/submissions",
        json=submission(paying, token, amount, head + DEADLINE_BLOCKS, gas),
    )
    if taken.status_code != 202:
        raise VerifyError(f"the payment wasn't taken: {taken.status_code} {taken.text}")
    print(f">> taken: {taken.json()['id']}")
    status = await landed(http, taken.headers["Location"], next_block)
    gained = await balance_of(chain, token, payee.address) - before
    if gained < amount:
        raise VerifyError(f"the payee gained {gained}, not {amount}")
    print(f">> landed: {status['transactionHash']} in block {int(status['blockNumber'], 16)}")
    verified = Verified(status["transactionHash"], int(status["blockNumber"], 16), gained)
    if accepts is None:
        print(">> x402: not taken here")
        return verified

    x402_transfer = await authorized(
        client, domain, chain.chain_id, token, payee.address, x402_amount
    )
    header = payment_header(accepts, x402_transfer)
    # held until its transaction is included: the client waits maxTimeoutSeconds, and more
    sending = asyncio.create_task(
        http.post(
            f"{path}/v1/x402",
            json=x402_call,
            headers={"PAYMENT-SIGNATURE": header},
            timeout=int(accepts["maxTimeoutSeconds"]) + HELD_SLACK_SECONDS,
        )
    )
    for _ in range(DEADLINE_BLOCKS + 3):
        if sending.done():
            break
        await next_block()
    paid = await sending
    if paid.status_code != 200:
        raise VerifyError(f"the x402 payment didn't settle: {paid.status_code} {paid.text}")
    transaction = decoded(paid.headers["PAYMENT-RESPONSE"])["transaction"]
    status = await landed(http, paid.headers["Location"], next_block)
    if status["transactionHash"] != transaction:
        raise VerifyError(f"settled as {transaction}, but {status['transactionHash']} landed")
    print(f">> x402 settled: {transaction} in block {int(status['blockNumber'], 16)}")
    return replace(verified, x402_transaction_hash=transaction)


def funding_of(chain_id: int, config: ChainConfig, token: Token, decimals: int) -> Capped | Swapped:
    """How a run on `chain_id`, served as `config` says, funds its client in `token` (of
    `decimals`): capped where the money is real (CAPS), bought through the token's one pool and
    the treasury's router on a test network (TESTNETS), and any other network refused."""
    if chain_id in CAPS:
        return Capped(int(CAPS[chain_id] * 10**decimals))
    if chain_id not in TESTNETS:
        raise VerifyError(f"chain {chain_id} isn't verified: neither capped (CAPS) nor a testnet")
    if config.treasury is None or len(token.price) != 1:
        raise VerifyError(f"{token.address} is bought through one pool and the treasury's router")
    return Swapped(token.price[0], config.weth, config.treasury.router)


async def landed(
    http: httpx.AsyncClient, location: str, next_block: Callable[[], Awaitable[object]]
) -> dict[str, str]:
    """A submission's status once it isn't pending: raise unless it's included."""
    for _ in range(DEADLINE_BLOCKS + 3):
        await next_block()
        status: dict[str, str] = (await http.get(location)).json()
        if status["status"] != "pending":
            break
        print(f">> pending, {int(status['attempts'], 16)} attempts")
    if status["status"] != "included":
        raise VerifyError(f"the payment didn't land: {status}")
    return status


async def _main(network: str, url: str, config: Path) -> None:
    chain_id = next((i for i, name in NETWORK_NAMES.items() if name == network), None)
    if chain_id is None:
        raise VerifyError(f"no network {network}")
    chain_config = load(config).chains[chain_id]
    paid_in, domain = next((t, t.eip3009) for t in chain_config.tokens if t.eip3009 is not None)
    token = paid_in.address
    minute = time.gmtime().tm_min
    if minute >= 57 or minute <= 2:
        raise VerifyError("the hourly cron runs the treasury: try again a few minutes after")
    async with httpx.AsyncClient(base_url=url, timeout=60) as http, httpx.AsyncClient() as nodes:
        chain = Chain(nodes, chain_id, chain_config.rpc)
        await chain.check_chain()
        decimals = int.from_bytes(await chain.call(token, selector("decimals()")))
        funding = funding_of(chain_id, chain_config, paid_in, decimals)
        # the client's key, created once the network is known to be verified
        key = SECRETS / network / "verify.key"
        client = read_key(key) if key.exists() else new_key(key)

        async def next_block() -> None:
            await asyncio.sleep(chain.slot)

        payee = read_key(SECRETS / "payee.key")
        try:
            await verify(
                http,
                f"/{network}",
                chain,
                client,
                payee,
                token,
                domain,
                next_block,
                20 * chain.slot,
                funding,
            )
        except WalletShortError as e:
            if e.address != payee.address:
                raise
            # the payee buying the token: its ETH is the deployment's, so it's funded by hand
            show(payee.address, chain_id)
            raise VerifyError(
                f"the {network} payee {payee.address} holds {e.held / 10**18} ETH and wants "
                f"{e.wanted / 10**18} to buy the token: fund it from a faucet (QR code above)"
            ) from None


if __name__ == "__main__":
    asyncio.run(_main(sys.argv[1], sys.argv[2], Path(sys.argv[3])))
