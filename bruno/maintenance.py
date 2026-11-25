"""The concurrency accounts, the float, the payee and its forwarder as the chain has them: read
in one batch.
Accounts are keyed by concurrency index: account i is derive(float, i)."""

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress

from .accounts import derive
from .chain import Chain, ChainError, quantity, word_of
from .jsonrpc import Answer, Call, JsonRpcError, result_of
from .tokens import balance_of_call

logger = logging.getLogger(__name__)


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
    chain: Chain, float_account: LocalAccount, indices: Iterable[int], block: str
) -> tuple[AccountState, ...]:
    """Concurrency accounts `indices`, read from the chain in one batch at `block`."""
    accounts = [(index, derive(float_account, index).address) for index in indices]
    answers = await chain.batch(
        [
            Call(method, [address, block])
            for _, address in accounts
            for method in ("eth_getBalance", "eth_getTransactionCount")
        ],
        atomic=True,
    )
    return tuple(
        AccountState(
            index,
            address,
            quantity(result_of(answers[2 * i])),
            quantity(result_of(answers[2 * i + 1])),
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
    """The float, every concurrency account, the payee and its forwarder, as read at block
    `block`: each's balance and nonce (the forwarder's balance only), and what the payee and
    the forwarder hold of each token. What the relay's root shows of them, all of it as of the
    one read."""

    block: int
    float_balance: int
    float_nonce: int
    accounts: tuple[AccountState, ...]
    payee_balance: int
    payee_nonce: int
    payee_tokens: tuple[Holding, ...]
    forwarder_balance: int
    forwarder_tokens: tuple[Holding, ...]


def shown_holdings(
    tokens: Mapping[ChecksumAddress, Denomination], answers: Sequence[Answer]
) -> tuple[Holding, ...]:
    """What an account holds of each of `tokens`, from its balanceOf's `answers` in their
    order, for a read only the root shows (the forwarder's): a token whose answer isn't a
    balance is left out and logged, so what a token says of that account stops nothing."""
    holdings: list[Holding] = []
    for (token, d), answer in zip(tokens.items(), answers, strict=True):
        try:
            holdings.append(Holding(token, word_of(result_of(answer)), d.decimals, d.symbol))
        except (JsonRpcError, ChainError) as e:
            logger.warning("%s's balance of the forwarder unread: %s", token, e)
    return tuple(holdings)


async def survey(
    chain: Chain,
    float_account: LocalAccount,
    count: int,
    block: int,
    payee: ChecksumAddress,
    forwarder: ChecksumAddress,
    tokens: Mapping[ChecksumAddress, Denomination],
) -> AccountsStatus:
    """The float, concurrency accounts 0 to `count` - 1, the payee and its `forwarder`, and
    what the payee and the forwarder hold of each of `tokens` (each's decimals and symbol),
    read in one batch as of the latest block, `block`. The forwarder's tokens are read for the
    root alone: one that doesn't answer is left out (shown_holdings)."""
    accounts = [(i, derive(float_account, i).address) for i in range(count)]
    addresses = [float_account.address, *(a for _, a in accounts), payee]
    answers = await chain.batch(
        [
            *(
                Call(method, [address, hex(block)])
                for address in addresses
                for method in ("eth_getBalance", "eth_getTransactionCount")
            ),
            *(balance_of_call(token, payee, hex(block)) for token in tokens),
            Call("eth_getBalance", [forwarder, hex(block)]),
            *(balance_of_call(token, forwarder, hex(block)) for token in tokens),
        ],
        atomic=True,
    )
    at = 2 * len(addresses)
    forwarded = at + len(tokens)
    values = [quantity(result_of(a)) for a in answers[: forwarded + 1]]
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
        values[forwarded],
        shown_holdings(tokens, answers[forwarded + 1 :]),
    )
