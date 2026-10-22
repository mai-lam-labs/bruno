"""Bundles (DESIGN.md, Bundling): a block's bundle, an optional funding transaction from the
float, then each submission's transaction from its concurrency account, droppable. For a
builder without droppingTxHashes, each submission's transaction alone, from a pre-funded
account.

The funding transaction is a contract creation whose initcode sends each concurrency
account its top-up and self-destructs to the float, leaving nothing in state: one transaction, one float nonce a bundle, its gas
known exactly when it's emitted (the assembler's bounds).
Only it isn't droppable: a builder may leave out any submission's transaction (one that
would revert, or is invalid), never the funding."""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress

from .assembler import Assembly, Op
from .common import BrunoError
from .forks import GasSchedule
from .transactions import Fees, SignedTransaction, sign


class BundleError(BrunoError):
    pass


class Mode(StrEnum):
    """How a run's submissions go to builders. One mode a run: each gives the float's nonce
    one user, the bundles' funding transactions (dropping) or maintenance's (standalone)."""

    DROPPING = "dropping"
    STANDALONE = "standalone"


@dataclass(frozen=True, slots=True)
class TopUp:
    """ETH for a concurrency account; `new_account` if it doesn't exist yet (no balance, no
    nonce, no code), so the transfer creates it."""

    account: ChecksumAddress
    amount: int
    new_account: bool


def shortfall(tx: SignedTransaction, balance: int) -> int:
    """What `tx`'s sender lacks, holding `balance`, for `tx` to be valid at its max fee."""
    return max(0, tx.max_cost - balance)


def funding_initcode(top_ups: Sequence[TopUp], schedule: GasSchedule) -> tuple[bytes, int]:
    """Initcode that sends each top-up (value CALLs given only the stipend: the accounts are
    EOAs) and self-destructs to the float (ORIGIN), created in the same transaction so removed
    (EIP-6780), reverting if any transfer fails; and its most gas."""
    if not top_ups:
        raise BundleError("a funding transaction tops up at least one account")
    asm = Assembly()
    for top_up in top_ups:
        # CALL's arguments, last pushed first: gas 0, to, value, no args, no return
        asm.push(0).push(0).push(0).push(0).push(top_up.amount).push_address(top_up.account)
        new = schedule.new_account if top_up.new_account else 0
        asm.push(0).value_call(schedule.cold_account_access + schedule.call_value + new)
        asm.op(Op.ISZERO).jumpi("failed")
    asm.op(Op.ORIGIN, Op.SELFDESTRUCT)
    asm.end()
    asm.label("failed")
    asm.push(0).push(0).op(Op.REVERT)
    return asm.assemble(())


def sign_funding(
    float_account: LocalAccount,
    chain_id: int,
    nonce: int,
    fees: Fees,
    top_ups: Sequence[TopUp],
    schedule: GasSchedule,
) -> SignedTransaction:
    """The float's funding transaction: a contract creation carrying the top-ups' value, its
    gas `schedule`'s."""
    initcode, execution = funding_initcode(top_ups, schedule)
    standard, floor = schedule.intrinsic(initcode, creation=True)
    gas = max(standard + execution, floor)
    value = sum(t.amount for t in top_ups)
    return sign(float_account, chain_id, nonce, fees, gas, None, initcode, value)


def bundle_params(
    block_number: int,
    funding: SignedTransaction | None,
    submissions: Sequence[SignedTransaction],
) -> dict[str, object]:
    """eth_sendBundle's parameters for one block: the funding transaction (none if no account
    needs ETH) then the submissions', every submission's in droppingTxHashes. No `version`:
    rbuilder takes droppingTxHashes only from a bundle with none, or v2 and later
    (rbuilder-primitives/src/serialize.rs)."""
    txs = ([funding] if funding is not None else []) + list(submissions)
    return {
        "txs": ["0x" + tx.raw.hex() for tx in txs],
        "blockNumber": hex(block_number),
        "droppingTxHashes": ["0x" + tx.hash.hex() for tx in submissions],
    }


def standalone_params(block_number: int, submission: SignedTransaction) -> dict[str, object]:
    """eth_sendBundle's parameters for a builder without droppingTxHashes: the submission's
    transaction alone, nothing listed, so a revert fails the bundle and nothing lands. Its
    concurrency account is funded beforehand: a funding transaction in each such bundle would
    need the float's one nonce in every one of them."""
    return {"txs": ["0x" + submission.raw.hex()], "blockNumber": hex(block_number)}


def atomic_params(block_number: int, txs: Sequence[SignedTransaction]) -> dict[str, object]:
    """eth_sendBundle's parameters for `txs`, all of them or none: nothing listed, so any one
    reverting fails the bundle. Every builder takes it in either mode."""
    return {"txs": ["0x" + tx.raw.hex() for tx in txs], "blockNumber": hex(block_number)}


def bundles(
    mode: Mode,
    block_number: int,
    funding: SignedTransaction | None,
    submissions: Sequence[SignedTransaction],
) -> list[dict[str, object]]:
    """The eth_sendBundle parameters for one block in `mode`: one bundle, funding first,
    every submission droppable; or each submission on its own, its account funded
    beforehand (by maintenance), so no funding."""
    if mode is Mode.DROPPING:
        return [bundle_params(block_number, funding, submissions)]
    if funding is not None:
        raise BundleError("standalone bundles carry no funding: maintenance funds the accounts")
    return [standalone_params(block_number, tx) for tx in submissions]
