"""A payout address's receipt wallet, which a merchant's buyers pay (the hosted x402
facilitator, server/facilitator.py): the key Bruno holds for it. Derived, never stored: from a
key of its own on the float's (BIP-32, as the concurrency accounts are, on a path apart from
theirs and the salts'), hashed with the payout address. So a payout address has one receipt
address for a float, the same in both relays, and a receipt key says nothing of another's or
of the float's."""

from eth_account import Account
from eth_account.hdaccount import key_from_seed
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress
from eth_utils.crypto import keccak

# the path the receipt keys' root is derived on from the float's key, apart from the
# concurrency accounts' (m/44'/60'/0'/0/i) and the salts' (m/44'/60'/1'/0/0)
RECEIPT_ROOT_PATH = "m/44'/60'/2'/0/0"


def receipt_root(float_account: LocalAccount) -> bytes:
    """The key every receipt key is hashed from: a private key derived from the float's on a
    path of its own, used for nothing else."""
    return key_from_seed(float_account.key, RECEIPT_ROOT_PATH)


def receipt_key(root: bytes, payout: ChecksumAddress) -> bytes:
    """The receipt wallet's private key for `payout`: keccak256 of `root` and the address's
    twenty bytes. A hash that isn't a private key (zero, or the curve's order or more, about
    one in 2**128) leaves that payout address with no receipt wallet."""
    return keccak(root + bytes.fromhex(payout[2:]))


def receipt_account(root: bytes, payout: ChecksumAddress) -> LocalAccount:
    """The receipt wallet for `payout`, as it signs."""
    return Account.from_key(receipt_key(root, payout))
