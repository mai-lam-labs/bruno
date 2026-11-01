"""ABI encoding's pieces, written by hand where eth_abi's encode isn't needed: a function's
selector, a word, an address in one; and ERC-20's balanceOf, which the runner, the user
operations' balance reader, tokens and the treasury all call."""

from eth_utils.crypto import keccak


def selector(signature: str) -> bytes:
    """The function `signature`'s selector: its hash's first four bytes."""
    return keccak(text=signature)[:4]


def word(value: int) -> bytes:
    """`value` as one 32-byte word."""
    return value.to_bytes(32)


def address_word(address: str) -> bytes:
    """`address` as one word, left-padded."""
    return bytes(12) + bytes.fromhex(address[2:])


BALANCE_OF = selector("balanceOf(address)")
