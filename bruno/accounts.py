"""The service instance's accounts: the float, which is the instance's own
key, and the concurrency accounts derived from it, BIP-32 with the float's private key as
the seed on m/44'/60'/0'/0/i. Concurrency account i is derive(float, i)."""

import os
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


def new_key(path: Path) -> LocalAccount:
    """A new account, its private key written to `path` as hex: readable by its owner only, in
    a directory only its owner can enter. A key already there is never overwritten."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("x") as file:
        path.chmod(0o600)
        key = os.urandom(32)
        file.write(key.hex() + "\n")
    return Account.from_key(key)
