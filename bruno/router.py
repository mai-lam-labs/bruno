"""Swapping a token for ETH through Uniswap's SwapRouter02 (swap-router-contracts), in one
call: multicall of each leg of the token's path, then unwrapWETH9 paying the ETH out.

A leg is a run of hops of one kind: a v3 leg is exactInput along its pools, a v2 leg
swapExactTokensForTokens along its tokens (the router finds each pair from its own Uniswap V2
factory). Each leg's output stays in the router (ADDRESS_THIS); the first leg pays in from the
caller, each later one swaps the router's whole balance (CONTRACT_BALANCE). Only the last leg,
and the unwrap, hold to the minimum out."""

from collections.abc import Sequence
from dataclasses import dataclass

from eth_abi.abi import encode
from eth_typing import ChecksumAddress
from eth_utils.crypto import keccak

from .config import PoolKind

# swap-router-contracts' Constants: an amountIn of 0 swaps the router's own balance, and
# address 2 is the router itself
CONTRACT_BALANCE = 0
ADDRESS_THIS = "0x0000000000000000000000000000000000000002"
_EXACT_INPUT = keccak(text="exactInput((bytes,address,uint256,uint256))")[:4]
_SWAP_EXACT_TOKENS_FOR_TOKENS = keccak(
    text="swapExactTokensForTokens(uint256,uint256,address[],address)"
)[:4]
_UNWRAP_WETH9 = keccak(text="unwrapWETH9(uint256,address)")[:4]
_MULTICALL = keccak(text="multicall(bytes[])")[:4]
_APPROVE = keccak(text="approve(address,uint256)")[:4]
_WITHDRAW = keccak(text="withdraw(uint256)")[:4]


@dataclass(frozen=True, slots=True)
class Step:
    """A hop of a token's path, as swapped: from `token_in` to `token_out` through a pool of
    `kind`, its fee tier (v3; 0 for v2)."""

    kind: PoolKind
    token_in: ChecksumAddress
    token_out: ChecksumAddress
    fee: int


def swap_calldata(
    steps: Sequence[Step], amount_in: int, min_out: int, recipient: ChecksumAddress
) -> bytes:
    """The router's multicall swapping `amount_in` of the first step's token along `steps`
    to WETH, unwrapped, at least `min_out` wei paid to `recipient`."""
    calls: list[bytes] = []
    legs = _legs(steps)
    for i, leg in enumerate(legs):
        amount = amount_in if i == 0 else CONTRACT_BALANCE
        least = min_out if i == len(legs) - 1 else 0
        if leg[0].kind is PoolKind.UNISWAP_V3:
            path = bytes.fromhex(leg[0].token_in[2:]) + b"".join(
                s.fee.to_bytes(3) + bytes.fromhex(s.token_out[2:]) for s in leg
            )
            calls.append(
                _EXACT_INPUT
                + encode(["(bytes,address,uint256,uint256)"], [(path, ADDRESS_THIS, amount, least)])
            )
        else:
            tokens = [leg[0].token_in, *(s.token_out for s in leg)]
            calls.append(
                _SWAP_EXACT_TOKENS_FOR_TOKENS
                + encode(
                    ["uint256", "uint256", "address[]", "address"],
                    [amount, least, tokens, ADDRESS_THIS],
                )
            )
    calls.append(_UNWRAP_WETH9 + encode(["uint256", "address"], [min_out, recipient]))
    return _MULTICALL + encode(["bytes[]"], [calls])


def _legs(steps: Sequence[Step]) -> list[list[Step]]:
    """`steps` in runs of one pool kind."""
    legs: list[list[Step]] = []
    for step in steps:
        if legs and legs[-1][0].kind is step.kind:
            legs[-1].append(step)
        else:
            legs.append([step])
    return legs


def approve_calldata(spender: ChecksumAddress, amount: int) -> bytes:
    """ERC-20 approve(spender, amount)."""
    return _APPROVE + encode(["address", "uint256"], [spender, amount])


def withdraw_calldata(amount: int) -> bytes:
    """WETH's withdraw(amount): `amount` of the caller's WETH as ETH."""
    return _WITHDRAW + encode(["uint256"], [amount])
