"""What's read of the accounts a plan depends on, each in one batch at the highest block
seen: one account's state (AccountState), and a call's accounts, its target and its EIP-7702
authorities (CallAccounts). The reads themselves (account_reads, authority_reads) and what
their answers say (accounts_of, empty_among) are a plan's to batch with its own."""

from collections.abc import Sequence
from dataclasses import dataclass

from eth_typing import ChecksumAddress

from .chain import Chain, code_of, has_code, quantity
from .jsonrpc import Answer, Call, result_of

_DELEGATION_PREFIX = bytes.fromhex("ef0100")


@dataclass(frozen=True, slots=True)
class AccountState:
    """An account as read: whether it's empty (no balance, no nonce, no code), its nonce, and
    whether it has code, a delegation's included: what runs when it's sent ETH."""

    empty: bool
    nonce: int
    has_code: bool


async def account_state(chain: Chain, account: ChecksumAddress) -> AccountState:
    """`account`'s state, read in one batch at the highest block seen."""
    reads = await chain.batch(authority_reads(account, chain.seen_tag))
    return AccountState(
        empty_among(reads) == 1, quantity(result_of(reads[1])), has_code(result_of(reads[2]))
    )


async def count_empty(chain: Chain, authorities: Sequence[ChecksumAddress]) -> int:
    """How many of `authorities` are empty (no balance, no nonce, no code), read in one
    batch at the highest block seen: nothing read if there are none."""
    at = chain.seen_tag
    return empty_among(
        await chain.batch([c for authority in authorities for c in authority_reads(authority, at)])
    )


@dataclass(frozen=True, slots=True)
class CallAccounts:
    """What's read of a call's accounts: those EIP-7702 delegated in its transaction (its
    authorities, and its target if delegated already), how many of its authorities are empty,
    and which of them are delegated already (`delegating`)."""

    delegated: frozenset[ChecksumAddress]
    empty: int
    delegating: frozenset[ChecksumAddress]


async def call_accounts(
    chain: Chain, target: ChecksumAddress, authorities: Sequence[ChecksumAddress]
) -> CallAccounts:
    """`target`'s code, and each authority's balance, nonce and code, read in one batch at the
    highest block seen."""
    reads = account_reads(target, authorities, chain.seen_tag)
    return accounts_of(target, authorities, await chain.batch(reads))


def account_reads(
    target: ChecksumAddress, authorities: Sequence[ChecksumAddress], block: str
) -> list[Call]:
    """What's read of a call's accounts at `block` (CallAccounts): its target's code, then each
    authority's balance, nonce and code."""
    calls = [Call("eth_getCode", [target, block])]
    calls += [c for authority in authorities for c in authority_reads(authority, block)]
    return calls


def accounts_of(
    target: ChecksumAddress, authorities: Sequence[ChecksumAddress], answers: Sequence[Answer]
) -> CallAccounts:
    """A call's accounts from their reads' `answers` (account_reads)."""
    delegated = set(authorities)
    if code_of(result_of(answers[0])).startswith(_DELEGATION_PREFIX):
        delegated.add(target)
    delegating = frozenset(
        authority
        for i, authority in enumerate(authorities)
        if code_of(result_of(answers[3 + 3 * i])).startswith(_DELEGATION_PREFIX)
    )
    return CallAccounts(frozenset(delegated), empty_among(answers[1:]), delegating)


def authority_reads(authority: ChecksumAddress, block: str) -> list[Call]:
    """An authority's reads at `block`, to tell whether it's empty: its balance, nonce and
    code."""
    return [
        Call("eth_getBalance", [authority, block]),
        Call("eth_getTransactionCount", [authority, block]),
        Call("eth_getCode", [authority, block]),
    ]


def empty_among(answers: Sequence[Answer]) -> int:
    """How many of the authorities whose reads `answers` are, three each, are empty."""
    return sum(
        1
        for i in range(0, len(answers), 3)
        if quantity(result_of(answers[i])) == 0
        and quantity(result_of(answers[i + 1])) == 0
        and not has_code(result_of(answers[i + 2]))
    )
