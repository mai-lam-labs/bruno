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
    """What's read of a submission's calls' accounts: those EIP-7702 delegated in its
    transaction (its authorities, and each target delegated already), how many of its
    authorities are empty, and which of them are delegated already (`delegating`)."""

    delegated: frozenset[ChecksumAddress]
    empty: int
    delegating: frozenset[ChecksumAddress]


async def call_accounts(
    chain: Chain, targets: Sequence[ChecksumAddress], authorities: Sequence[ChecksumAddress]
) -> CallAccounts:
    """Each of `targets`' code, and each authority's balance, nonce and code, read in one batch
    at the highest block seen."""
    reads = account_reads(targets, authorities, chain.seen_tag)
    return accounts_of(targets, authorities, await chain.batch(reads))


def account_reads(
    targets: Sequence[ChecksumAddress], authorities: Sequence[ChecksumAddress], block: str
) -> list[Call]:
    """What's read of a submission's calls' accounts at `block` (CallAccounts): each target's
    code, in the calls' order, then each authority's balance, nonce and code."""
    calls = [Call("eth_getCode", [target, block]) for target in targets]
    calls += [c for authority in authorities for c in authority_reads(authority, block)]
    return calls


def accounts_of(
    targets: Sequence[ChecksumAddress],
    authorities: Sequence[ChecksumAddress],
    answers: Sequence[Answer],
) -> CallAccounts:
    """A submission's calls' accounts from their reads' `answers` (account_reads)."""
    delegated = set(authorities)
    for target, code in zip(targets, answers, strict=False):
        if code_of(result_of(code)).startswith(_DELEGATION_PREFIX):
            delegated.add(target)
    theirs = answers[len(targets) :]
    delegating = frozenset(
        authority
        for i, authority in enumerate(authorities)
        if code_of(result_of(theirs[2 + 3 * i])).startswith(_DELEGATION_PREFIX)
    )
    return CallAccounts(frozenset(delegated), empty_among(theirs), delegating)


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
