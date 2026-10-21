"""Maintaining the concurrency accounts: reading them, topping them up to
the funds each must hold, and sweeping them back into the float. Accounts are keyed by
concurrency index: account i is derive(float, i).

Topping up sends one transaction from the float through the public mempool: the funding
transaction (bruno.bundle.sign_funding), sending each account only its shortfall."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress

from .accounts import derive
from .bundle import TopUp, sign_funding
from .chain import Chain
from .common import BrunoError
from .forks import GasSchedule
from .net.jsonrpc import Call
from .transactions import Fees, SignedTransaction, sign_drain


class MaintenanceError(BrunoError):
    pass


@dataclass(frozen=True, slots=True)
class AccountState:
    """A concurrency account as the chain has it."""

    index: int
    address: ChecksumAddress
    balance: int
    nonce: int

    @property
    def exists(self) -> bool:
        """Whether the account is in the state: a transfer to one that isn't creates it."""
        return self.balance > 0 or self.nonce > 0


async def monitor(
    chain: Chain, float_account: LocalAccount, indices: Iterable[int]
) -> tuple[AccountState, ...]:
    """Concurrency accounts `indices`, read from the chain in one batch."""
    accounts = [(index, derive(float_account, index).address) for index in indices]
    answers = await chain.batch(
        [
            Call(method, [address, "latest"])
            for _, address in accounts
            for method in ("eth_getBalance", "eth_getTransactionCount")
        ]
    )
    return tuple(
        AccountState(
            index,
            address,
            int(answers[2 * i].value(), 16),
            int(answers[2 * i + 1].value(), 16),
        )
        for i, (index, address) in enumerate(accounts)
    )


@dataclass(frozen=True, slots=True)
class AccountsStatus:
    """The float and every concurrency account, as read at block `block`: what the relay's
    root shows of them."""

    block: int
    float_balance: int
    float_nonce: int
    accounts: tuple[AccountState, ...]


async def survey(
    chain: Chain, float_account: LocalAccount, count: int, block: int
) -> AccountsStatus:
    """The float and concurrency accounts 0 to `count` - 1, read in one batch as of the
    latest block, `block`."""
    accounts = [(i, derive(float_account, i).address) for i in range(count)]
    addresses = [float_account.address, *(a for _, a in accounts)]
    answers = await chain.batch(
        [
            Call(method, [address, "latest"])
            for address in addresses
            for method in ("eth_getBalance", "eth_getTransactionCount")
        ]
    )
    values = [int(a.value(), 16) for a in answers]
    return AccountsStatus(
        block,
        values[0],
        values[1],
        tuple(
            AccountState(index, address, values[2 + 2 * n], values[3 + 2 * n])
            for n, (index, address) in enumerate(accounts)
        ),
    )


def top_ups(states: Iterable[AccountState], required: Mapping[int, int]) -> list[TopUp]:
    """What brings each account in `required` to at least its required wei: its shortfall,
    for those short; nothing for the rest."""
    by_index = {s.index: s for s in states}
    missing = set(required) - set(by_index)
    if missing:
        raise MaintenanceError(f"no state for concurrency accounts {sorted(missing)}")
    return [
        TopUp(by_index[i].address, amount - by_index[i].balance, new_account=not by_index[i].exists)
        for i, amount in sorted(required.items())
        if amount > by_index[i].balance
    ]


async def replenish(
    chain: Chain,
    float_account: LocalAccount,
    required: Mapping[int, int],
    fees: Fees,
    schedule: GasSchedule,
    timeout: float,
) -> SignedTransaction | None:
    """Top the accounts in `required` up to it, in one funding transaction from the float,
    waited for; None, and nothing sent, if none is short."""
    states = await monitor(chain, float_account, required)
    needed = top_ups(states, required)
    if not needed:
        return None
    nonce = await chain.nonce(float_account.address)
    tx = sign_funding(float_account, chain.chain_id, nonce, fees, needed, schedule)
    await chain.send(tx, "topping up concurrency accounts", timeout)
    return tx


async def sweep(
    chain: Chain,
    float_account: LocalAccount,
    indices: Iterable[int],
    gas_price: int,
    schedule: GasSchedule,
    timeout: float,
) -> list[SignedTransaction]:
    """Drain each of concurrency accounts `indices` into the float, to exactly 0 (a type-1
    transfer at `gas_price`, sign_drain). An account holding less than its drain's gas is
    an error: what it holds can't be moved."""
    sent: list[SignedTransaction] = []
    for state in await monitor(chain, float_account, indices):
        if state.balance == 0:
            continue
        account = derive(float_account, state.index)
        gas = schedule.transfer(new_recipient=False)  # the float exists
        tx = sign_drain(
            account,
            chain.chain_id,
            state.nonce,
            state.balance,
            gas_price,
            float_account.address,
            gas,
        )
        if tx is None:
            raise MaintenanceError(
                f"concurrency account {state.index} holds {state.balance} wei, less than its "
                f"drain's gas at {gas_price} wei"
            )
        await chain.send(tx, f"sweeping concurrency account {state.index}", timeout)
        sent.append(tx)
    return sent
