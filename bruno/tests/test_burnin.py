"""A burn-in: the relay on the fast chain for many blocks, left to its own clock, under a
seeded random load, against both relays (make burn-in; not part of make check).

Each slot, clients submit calls paying in USDC, USDT or WETH: most honest, some underpaying,
some spending their tokens after submitting, some with deadlines a block or two away, some
submitting twice. The base fee surges now and then, as fast as EIP-1559 lets it. Every few blocks the treasury runs:
the payee starts with a little ETH and the float small, so the relay lives on its own
revenue. Each block is checked: none of the relay's transactions (the float's, its accounts',
the payee's) reverts on-chain; nothing answers with a server error; whatever is included
landed by its deadline. At the end nothing is left pending, and neither relay logged an error.

    BURNIN_SEED=… to replay a run's load (the relay's timing is its own), BURNIN_BLOCKS=… its
    length."""

import asyncio
import logging
import os
import re
import secrets
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from math import ceil
from pathlib import Path
from typing import override

import httpx
import pytest
from eth_abi.abi import encode
from eth_account import Account
from eth_utils.crypto import keccak

from bruno.accounts import derive
from bruno.forks import GasSchedule
from bruno.tests.calibur import calibur_batch, fresh_client
from bruno.tests.deployments import CLIENT_GAS, transfer
from bruno.tests.funds import a_transaction
from bruno.tests.interface import quote
from bruno.tests.localchain import CALIBUR, TEST_FLOAT, USDC, USDT, WETH, LocalChain, LocalToken
from bruno.tests.served import FAST_BATCH_SECONDS, FAST_SLOT_SECONDS, Served, Setup
from bruno.transactions import authorization_json

pytestmark = pytest.mark.burnin

SEED = int(os.environ.get("BURNIN_SEED") or secrets.randbelow(2**32))
BLOCKS = int(os.environ.get("BURNIN_BLOCKS") or 120)
SETUP = Setup(
    batch_seconds=FAST_BATCH_SECONDS,
    slot_seconds=FAST_SLOT_SECONDS,
    accounts=2,
    max_pending=8,
    funding_multiplier=Decimal(2),
    # 10 slots, as 120 seconds is on Ethereum's
    funding_timeout_seconds=10.0 * FAST_SLOT_SECONDS,
)
PAYING = (USDC, USDT, WETH)
# how a client behaves, and how often
KINDS = {"honest": 6, "underpaid": 1, "spender": 1, "short": 1, "twice": 1}
# a submission's deadline, blocks ahead; a short one's
DEADLINE_BLOCKS = (5, 20)
SHORT_DEADLINE_BLOCKS = (1, 2)
TREASURY_EVERY = 8
# a surge: the base fee risen by the most EIP-1559 allows, an eighth a block, for 3 to 12
# blocks; between surges, anvil's own (empty blocks: falling an eighth a block)
SURGE_CHANCE = Fraction(1, 20)
SURGE_BLOCKS = (3, 12)
MAX_RISE = Fraction(9, 8)
# WETH9's balanceOf mapping
WETH_BALANCE_SLOT = 3
WORKERD_LOG = Path("dist/tools/workerd.log")
# a full queue's refusal (SPEC.md, Errors), answered as 503
NO_CAPACITY = -32013


class Draws:
    """The load's choices, drawn from keccak of the seed and a count, as the vectors draw their
    data: the same load for the same seed."""

    def __init__(self, seed: int) -> None:
        self._seed = seed
        self._drawn = 0

    def below(self, n: int) -> int:
        self._drawn += 1
        return int.from_bytes(keccak(text=f"burn-in {self._seed} {self._drawn}")) % n

    def between(self, low: int, high: int) -> int:
        return low + self.below(high - low + 1)

    def choice[T](self, options: Sequence[T]) -> T:
        return options[self.below(len(options))]

    def weighted(self, weights: Mapping[str, int]) -> str:
        at = self.below(sum(weights.values()))
        for option, weight in weights.items():
            if at < weight:
                return option
            at -= weight
        raise AssertionError("a weight drawn past the total")

    def fraction(self, low: Fraction, high: Fraction) -> Fraction:
        return low + (high - low) * Fraction(self.below(10**6), 10**6)

    def chance(self, p: Fraction) -> bool:
        return self.fraction(Fraction(0), Fraction(1)) < p


@dataclass
class Submitted:
    """A client's submission, how it behaved, and what came of it."""

    name: str
    kind: str
    token: LocalToken
    amount: int
    deadline: int
    taken: bool
    location: str | None = None
    status: str = "pending"


class Errors(logging.Handler):
    """The relay's log records at ERROR and above."""

    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.records: list[str] = []

    @override
    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(self.format(record))


async def give(chain: LocalChain, token: LocalToken, holder: str, amount: int) -> None:
    """`holder` given `amount` more of `token`, mining nothing: WETH written into its balances,
    with the ETH behind it, so it unwraps."""
    if token is not WETH:
        await chain.mint(token, holder, amount)
        return
    slot = int.from_bytes(keccak(encode(["address", "uint256"], [holder, WETH_BALANCE_SLOT])))
    held = await chain.balance_of(WETH, holder)
    await chain.store(WETH.address, slot, f"0x{held + amount:064x}")
    await chain.set_eth_balance(WETH.address, await chain.eth_balance(WETH.address) + amount)


async def take_away(chain: LocalChain, token: LocalToken, holder: str) -> None:
    """`holder`'s `token` gone, as if spent elsewhere."""
    held = await chain.balance_of(token, holder)
    if token is WETH:
        slot = int.from_bytes(keccak(encode(["address", "uint256"], [holder, WETH_BALANCE_SLOT])))
        await chain.store(WETH.address, slot, "0x" + "00" * 32)
    else:
        await chain.mint(token, holder, -held)


def server_error(answer: httpx.Response) -> bool:
    """Whether `answer` is the relay failing, not refusing: a 5xx other than a full queue's
    503 (-32013, no capacity)."""
    if answer.status_code < 500:
        return False
    return not (answer.status_code == 503 and answer.json()["error"]["code"] == NO_CAPACITY)


async def diagnosis(chain: LocalChain, logged: str) -> str:
    """For a transaction a log line names (0x and 64 hex), what the chain knows of it: whether
    it's mined, pending or unknown, its nonce and max fee, against its sender's nonce and
    balance and the base fee."""
    found = re.search(r"0x[0-9a-f]{64}", logged)
    if found is None:
        return ""
    tx = await chain.rpc("eth_getTransactionByHash", [found.group()])
    if tx is None:
        return ": unknown to the chain"
    state = "pending" if tx["blockNumber"] is None else f"mined at {int(tx['blockNumber'], 16)}"
    sender = tx["from"]
    return (
        f": {state}, nonce {int(tx['nonce'], 16)} (its sender's {await chain.nonce(sender)}), "
        f"max fee {int(tx['maxFeePerGas'], 16)} (the base fee {await chain.base_fee()}), "
        f"value {int(tx['value'], 16)} (its sender holds {await chain.eth_balance(sender)})"
    )


def names() -> Iterator[str]:
    n = 0
    while True:
        yield f"burn-in {SEED} client {n}"
        n += 1


@pytest.mark.parametrize("served", [SETUP], indirect=True, ids=str)
async def test_burn_in(
    chain: LocalChain, served: Served, schedule: GasSchedule, backend: str
) -> None:
    draws = Draws(SEED)
    print(f"\nburn-in: seed {SEED}, {BLOCKS} blocks, the {backend} relay")
    violations: list[str] = []
    errors = Errors()
    logging.getLogger("bruno").addHandler(errors)
    log_from = WORKERD_LOG.stat().st_size if WORKERD_LOG.exists() else 0
    ours = {
        TEST_FLOAT.address,
        served.payee,
        *(derive(TEST_FLOAT, i).address for i in range(SETUP.accounts)),
    }
    transaction = await a_transaction(chain, served, schedule)
    await chain.set_eth_balance(TEST_FLOAT.address, 3 * transaction)
    await chain.set_eth_balance(served.payee, 2 * transaction)
    chain_id = served.chain_id
    submitted: list[Submitted] = []
    spends: dict[int, list[Submitted]] = {}
    treasury: list[str] = []
    floats: list[int] = []
    client_names = names()
    surge = 0

    async def submit(http: httpx.AsyncClient, head: int) -> None:
        kind = draws.weighted(KINDS)
        token = draws.choice(PAYING)
        name = next(client_names)
        client = fresh_client(name)
        price = await quote(http, chain_id, token.address, authority=client.address)
        if kind == "underpaid":
            amount = max(1, int(price * draws.fraction(Fraction(3, 10), Fraction(9, 10))))
        else:
            amount = int(price * draws.fraction(Fraction(1), Fraction(3, 2))) + 1
        low, high = SHORT_DEADLINE_BLOCKS if kind == "short" else DEADLINE_BLOCKS
        deadline = head + draws.between(low, high)
        await give(chain, token, client.address, amount)
        authorization = Account.sign_authorization(
            {"chainId": chain_id, "address": CALIBUR, "nonce": 0}, client.key
        )
        call = calibur_batch(client, chain_id, [transfer(token, served.payee, amount)])
        body = {
            "call": {"to": call.to, "data": call.data},
            "authorizationList": [authorization_json(authorization)],
            "gas": hex(CLIENT_GAS),
            "payment": {"token": token.address, "payee": served.payee, "amount": hex(amount)},
            "maxBlockNumber": hex(deadline),
        }
        answers = [await http.post(f"/{chain_id}/v1/submissions", json=body)]
        if kind == "twice":
            answers.append(await http.post(f"/{chain_id}/v1/submissions", json=body))
        violations.extend(f"{name}: {a.status_code} {a.text}" for a in answers if server_error(a))
        taken = answers[0].status_code in (200, 202)
        entry = Submitted(name, kind, token, amount, deadline, taken)
        if taken:
            entry.location = answers[0].headers["Location"]
        else:
            entry.status = f"refused {answers[0].status_code}"
        submitted.append(entry)
        if kind == "spender" and taken:
            spends.setdefault(head + draws.between(0, 3), []).append(entry)

    async def check_block(http: httpx.AsyncClient, built: tuple[bytes, ...]) -> None:
        for tx_hash in built:
            receipt = await chain.rpc("eth_getTransactionReceipt", ["0x" + tx_hash.hex()])
            tx = await chain.rpc("eth_getTransactionByHash", ["0x" + tx_hash.hex()])
            sender = tx["from"]
            if any(sender.lower() == a.lower() for a in ours) and receipt["status"] != "0x1":
                violations.append(
                    f"block {int(receipt['blockNumber'], 16)}: {sender}'s "
                    f"0x{tx_hash.hex()} reverted on-chain"
                )
        for entry in submitted:
            if entry.location is None or entry.status != "pending":
                continue
            answer = await http.get(entry.location)
            if server_error(answer):
                violations.append(f"{entry.name}: status {answer.status_code} {answer.text}")
                continue
            if answer.status_code == 404:
                entry.status = "forgotten"
                continue
            status = answer.json()
            entry.status = status["status"]
            if entry.status == "included":
                landed = int(status["blockNumber"], 16)
                if landed > entry.deadline:
                    violations.append(f"{entry.name}: included at {landed}, past {entry.deadline}")

    async def a_block(http: httpx.AsyncClient) -> None:
        await asyncio.sleep(FAST_SLOT_SECONDS)
        built = await served.builder.build_block()
        await check_block(http, built.transactions)
        floats.append(await chain.eth_balance(TEST_FLOAT.address))

    try:
        async with served.interface() as http:
            await served.start()
            for block in range(BLOCKS):
                head = await served.head()
                for _ in range(draws.choice((0, 0, 1, 1, 2, 3))):
                    await submit(http, head)
                for at in [b for b in spends if b <= head]:
                    for entry in spends.pop(at):
                        await take_away(chain, entry.token, fresh_client(entry.name).address)
                if surge == 0 and draws.chance(SURGE_CHANCE):
                    surge = draws.between(*SURGE_BLOCKS)
                if surge > 0:
                    surge -= 1
                    await chain.set_base_fee(ceil(await chain.base_fee() * MAX_RISE))
                if block % TREASURY_EVERY == 0:
                    await served.treasury()
                    root = (await http.get("/")).json()
                    treasury.append(root["chains"][0]["treasury"]["outcome"])
                await a_block(http)
            # drained: no new submissions, until every deadline has passed
            for _ in range(DEADLINE_BLOCKS[1] + 3):
                await a_block(http)
    finally:
        logging.getLogger("bruno").removeHandler(errors)

    for entry in submitted:
        if entry.status == "pending" and entry.location is not None:
            async with served.interface() as http:
                status = (await http.get(entry.location)).json()
                head = await served.head()
            violations.append(
                f"{entry.name} ({entry.kind}): still pending at {head}, its deadline "
                f"{entry.deadline}: {status}"
            )
    violations.extend(
        [f"the Python relay logged: {r}{await diagnosis(chain, r)}" for r in errors.records]
    )
    if backend == "worker" and WORKERD_LOG.exists():
        with WORKERD_LOG.open() as log:
            log.seek(log_from)
            for line in log:
                # what the Worker logs as an error, as the Python relay logs it at ERROR
                errors_seen = ("alarm:", "Uncaught", "Error", "not seen in time", "reverted")
                if any(e in line for e in errors_seen):
                    violations.append(f"workerd logged: {line.strip()}")

    outcomes = Counter((e.kind, e.status) for e in submitted)
    print(f"burn-in: {len(submitted)} submissions")
    for (kind, status), count in sorted(outcomes.items()):
        print(f"  {kind:<9} {status:<14} {count}")
    print(f"burn-in: the float from {min(floats)} to {max(floats)} wei, at the end {floats[-1]}")
    for n, outcome in enumerate(treasury):
        print(f"  treasury run {n}: {outcome}")
    assert not violations, "\n".join([*violations, f"seed {SEED}"])
