"""Maintaining the concurrency accounts (NPD-SPEC.md §6.3): reading them, topping them up to
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
    """Concurrency accounts `indices`, read from the chain."""
    states: list[AccountState] = []
    for index in indices:
        address = derive(float_account, index).address
        balance = await chain.balance(address)
        nonce = await chain.nonce(address)
        states.append(AccountState(index, address, balance, nonce))
    return tuple(states)


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
