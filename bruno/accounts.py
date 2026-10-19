"""The service instance's accounts: the float, which is the instance's own
key, and the concurrency accounts derived from it, BIP-32 with the float's private key as
the seed on m/44'/60'/0'/0/i. Concurrency account i is derive(float, i)."""

from pathlib import Path

from eth_account import Account
from eth_account.hdaccount import key_from_seed
from eth_account.signers.local import LocalAccount


def derive(float_account: LocalAccount, index: int) -> LocalAccount:
    """The account at m/44'/60'/0'/0/`index` from the float's key."""
    return Account.from_key(key_from_seed(float_account.key, f"m/44'/60'/0'/0/{index}"))


def read_key(path: Path) -> LocalAccount:
    """The account whose private key `path` holds, as hex."""
    return Account.from_key(bytes.fromhex(path.read_text().strip().removeprefix("0x")))
