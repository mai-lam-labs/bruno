"""A deployed relay verified with real transactions (make NETWORK-verify): a payment that
lands, and one a unit short that the simulation refuses, through the relay's own URL; and, where
it takes x402, a call paid through its challenge that lands too.

The client is an account derived from the float's key, holding nothing but the token: its call
is the token's transferWithAuthorization (EIP-3009) of the amount to the payee, which anyone may
submit, so it needs no ETH and no EIP-7702 delegation. The payee funds it with exactly what's
missing, and the payment returns it: what's spent is gas. Keys are read from .secrets/, only
addresses printed.

    python -m bruno.tests.verify NETWORK URL CONFIG MOST

MOST is the most the client may be funded, in whole tokens: a relay quoting more is refused.
"""

import asyncio
import base64
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from decimal import Decimal
from math import ceil
from pathlib import Path

import httpx
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress, Hash32
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from bruno.accounts import derive, read_key
from bruno.chain import Chain, lasting_fees, market_tip
from bruno.common import BrunoError
from bruno.config import Eip712Domain, load
from bruno.eip3009 import TransferAuthorization, digest, measured, measuring, transfer_gas
from bruno.forks import NETWORK_NAMES
from bruno.runner import UNDERPAID
from bruno.transactions import sign

SECRETS = Path(__file__).parents[2] / ".secrets"
# the client: an account derived from the float's key, apart from the live tests' (1000 on)
CLIENT_INDEX = 2000
CALL_BYTES = 4 + 9 * 32
DEADLINE_BLOCKS = 10
# how much longer than maxTimeoutSeconds an x402 request is waited for: its answer's way back
HELD_SLACK_SECONDS = 30
# the payee's funding transfer: its max fee lasting this many blocks; its gas over its estimate
FUNDING_BLOCKS = 5
GAS_HEADROOM = 5 / 4
_TRANSFER = keccak(text="transfer(address,uint256)")[:4]
_BALANCE_OF = keccak(text="balanceOf(address)")[:4]
_NEVER = 2**256 - 1


class VerifyError(BrunoError):
    pass


@dataclass(frozen=True, slots=True)
class Verified:
    transaction_hash: str
    block_number: int
    paid: int
    # the x402 payment's transaction, where the relay takes x402
    x402_transaction_hash: str | None = None


def word(address: str) -> bytes:
    return bytes(12) + bytes.fromhex(address[2:])


async def balance_of(chain: Chain, token: ChecksumAddress, holder: str) -> int:
    return int.from_bytes(await chain.call(token, _BALANCE_OF + word(holder)))


def authorized(
    client: LocalAccount,
    domain: Eip712Domain,
    chain_id: int,
    token: ChecksumAddress,
    payee: ChecksumAddress,
    value: int,
) -> TransferAuthorization:
    nonce = os.urandom(32)
    signed = client.unsafe_sign_hash(
        Hash32(digest(domain, chain_id, token, client.address, payee, value, 0, _NEVER, nonce))
    )
    return TransferAuthorization(
        client.address, payee, value, 0, _NEVER, nonce, bytes(signed.signature)
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
    chain: Chain, payee: LocalAccount, token: ChecksumAddress, to: str, amount: int, wait: float
) -> None:
    """`to` given `amount` of `token` by the payee: a plain transfer, its gas measured."""
    data = _TRANSFER + word(to) + amount.to_bytes(32)
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
    float_: LocalAccount,
    payee: LocalAccount,
    token: ChecksumAddress,
    domain: Eip712Domain,
    next_block: Callable[[], Awaitable[None]],
    funding_wait: float,
    most: int,
) -> Verified:
    """Through the relay at `http` (its chain at `path`): the client funded to the quote, a
    payment a unit short refused with ERCNNNN_Underpaid(), then the quote paid and landed, the
    payee gaining it. `next_block` waits for, or makes, the next block."""
    client = derive(float_, CLIENT_INDEX)
    terms = (await http.get(f"{path}/v1/config")).json()
    if to_checksum_address(terms["payee"]) != payee.address:
        raise VerifyError(f"the relay's payee is {terms['payee']}, not {payee.address}")
    gas = await call_gas(chain, domain, token, payee.address)
    params = {"gas": hex(gas), "callBytes": hex(CALL_BYTES), "tokens": [token]}
    quoted = await http.get(f"{path}/v1/quote", params=params)
    if quoted.status_code != 200:
        raise VerifyError(f"quote: {quoted.status_code} {quoted.text}")
    amount = int(quoted.json()["quotes"][0]["amount"], 16)
    # x402, where it's taken: its challenge for a call that succeeds for anyone
    x402_call = {
        "call": {"to": token, "data": "0x" + (_BALANCE_OF + word(client.address)).hex()},
        "gas": hex(gas),
    }
    challenged = await http.post(f"{path}/v1/x402", json=x402_call)
    accepts = None
    if challenged.status_code == 402:
        accepts = next(a for a in challenged.json()["accepts"] if a["asset"] == token)
    x402_amount = 0 if accepts is None else int(accepts["amount"])
    print(f">> client {client.address}; quoted {amount}, over x402 {x402_amount} of {token}")
    if amount + x402_amount > most:
        raise VerifyError(f"the relay asks {amount + x402_amount}, over the {most} allowed")
    held = await balance_of(chain, token, client.address)
    if held < amount + x402_amount:
        await fund(chain, payee, token, client.address, amount + x402_amount - held, funding_wait)

    short = authorized(client, domain, chain.chain_id, token, payee.address, amount - 1)
    head = await chain.head()
    refused = await http.post(
        f"{path}/v1/submissions", json=submission(short, token, amount, head + DEADLINE_BLOCKS, gas)
    )
    revert = refused.json().get("error", {}).get("data", {})
    if refused.status_code != 422 or revert.get("revertData") != "0x" + UNDERPAID.hex():
        raise VerifyError(f"a unit short wasn't refused as underpaid: {refused.text}")
    print(">> a unit short: refused, ERCNNNN_Underpaid()")

    before = await balance_of(chain, token, payee.address)
    paying = authorized(client, domain, chain.chain_id, token, payee.address, amount)
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

    signed = authorized(client, domain, chain.chain_id, token, payee.address, x402_amount)
    header = x402_payment(accepts, signed)
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
    transaction = decode_payment_response(paid.headers["PAYMENT-RESPONSE"])["transaction"]
    status = await landed(http, paid.headers["Location"], next_block)
    if status["transactionHash"] != transaction:
        raise VerifyError(f"settled as {transaction}, but {status['transactionHash']} landed")
    print(f">> x402 settled: {transaction} in block {int(status['blockNumber'], 16)}")
    return replace(verified, x402_transaction_hash=transaction)


def decode_payment_response(header: str) -> dict[str, str]:
    """PAYMENT-RESPONSE's settlement response: base64 of its JSON."""
    response: dict[str, str] = json.loads(base64.b64decode(header))
    return response


async def landed(
    http: httpx.AsyncClient, location: str, next_block: Callable[[], Awaitable[None]]
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


def x402_payment(accepted: dict[str, object], transfer: TransferAuthorization) -> str:
    """A PAYMENT-SIGNATURE: `transfer`, for `accepted`, base64 of its JSON."""
    signature = transfer.signature
    payload = {
        "x402Version": 2,
        "accepted": accepted,
        "payload": {
            "signature": "0x" + signature.hex(),
            "authorization": {
                "from": transfer.payer,
                "to": transfer.to,
                "value": str(transfer.value),
                "validAfter": str(transfer.valid_after),
                "validBefore": str(transfer.valid_before),
                "nonce": "0x" + transfer.nonce.hex(),
            },
        },
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


async def _main(network: str, url: str, config: Path, most_tokens: Decimal) -> None:
    chain_id = next((i for i, name in NETWORK_NAMES.items() if name == network), None)
    if chain_id is None:
        raise VerifyError(f"no network {network}")
    chain_config = load(config).chains[chain_id]
    token, domain = next(
        (to_checksum_address(t.address), t.eip3009)
        for t in chain_config.tokens
        if t.eip3009 is not None
    )
    minute = time.gmtime().tm_min
    if minute >= 57 or minute <= 2:
        raise VerifyError("the hourly cron runs the treasury: try again a few minutes after")
    async with httpx.AsyncClient(base_url=url, timeout=60) as http, httpx.AsyncClient() as nodes:
        chain = Chain(nodes, chain_id, chain_config.rpc)
        await chain.check_chain()
        decimals = int.from_bytes(await chain.call(token, keccak(text="decimals()")[:4]))

        async def next_block() -> None:
            await asyncio.sleep(chain.slot_seconds)

        await verify(
            http,
            f"/{network}",
            chain,
            read_key(SECRETS / "float.key"),
            read_key(SECRETS / "payee.key"),
            token,
            domain,
            next_block,
            20 * chain.slot_seconds,
            int(most_tokens * 10**decimals),
        )


if __name__ == "__main__":
    asyncio.run(_main(sys.argv[1], sys.argv[2], Path(sys.argv[3]), Decimal(sys.argv[4])))
