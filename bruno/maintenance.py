"""The concurrency accounts, the float and the payee as the chain has them: read in one batch.
Accounts are keyed by concurrency index: account i is derive(float, i)."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress

from .accounts import derive
from .chain import Chain, quantity
from .jsonrpc import Call
from .tokens import balance_of_call


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
            quantity(answers[2 * i].value()),
            quantity(answers[2 * i + 1].value()),
        )
        for i, (index, address) in enumerate(accounts)
    )


@dataclass(frozen=True, slots=True)
class Denomination:
    """A token's decimals and symbol: how its amounts are written."""

    decimals: int
    symbol: str


@dataclass(frozen=True, slots=True)
class Holding:
    """What an account holds of a token, and the token's decimals and symbol: ETH's as
    ERC-7528's address."""

    token: ChecksumAddress
    amount: int
    decimals: int
    symbol: str


@dataclass(frozen=True, slots=True)
class AccountsStatus:
    """The float, every concurrency account and the payee, as read at block `block`: each's
    balance and nonce, and what the payee holds of each token. What the relay's root shows of
    them, all of it as of the one read."""

    block: int
    float_balance: int
    float_nonce: int
    accounts: tuple[AccountState, ...]
    payee_balance: int
    payee_nonce: int
    payee_tokens: tuple[Holding, ...]


async def survey(
    chain: Chain,
    float_account: LocalAccount,
    count: int,
    block: int,
    payee: ChecksumAddress,
    tokens: Mapping[ChecksumAddress, Denomination],
) -> AccountsStatus:
    """The float, concurrency accounts 0 to `count` - 1 and the payee, and what the payee
    holds of each of `tokens` (each's decimals and symbol), read in one batch as of the latest
    block, `block`."""
    accounts = [(i, derive(float_account, i).address) for i in range(count)]
    addresses = [float_account.address, *(a for _, a in accounts), payee]
    answers = await chain.batch(
        [
            *(
                Call(method, [address, "latest"])
                for address in addresses
                for method in ("eth_getBalance", "eth_getTransactionCount")
            ),
            *(balance_of_call(token, payee) for token in tokens),
        ]
    )
    values = [quantity(a.value()) for a in answers]
    at = 2 * len(addresses)
    return AccountsStatus(
        block,
        values[0],
        values[1],
        tuple(
            AccountState(index, address, values[2 + 2 * n], values[3 + 2 * n])
            for n, (index, address) in enumerate(accounts)
        ),
        values[at - 2],
        values[at - 1],
        tuple(
            Holding(token, values[at + n], d.decimals, d.symbol)
            for n, (token, d) in enumerate(tokens.items())
        ),
    )
