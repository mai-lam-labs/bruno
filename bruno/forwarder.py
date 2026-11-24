"""The forwarder: a contract with the payee in its code, which a payer pays where no runner
measures the payment, and which sends all it holds on to the payee, reverting unless the payee
gained at least the amount asked. Bruno's last transaction in a bundle calls it, so the bundle
lands only if Bruno was paid.

No owner and no storage. Anyone may call it: whatever it forwards goes to the payee and nowhere
else. It's one contract for ETH and every ERC-20, told apart by its calldata's length:

- none: ETH sent with it is kept (how a payer pays in ETH);
- one word, `least`: read the payee's balance; send the payee all the ETH held, reverting with
  the payee's revert data if that fails; read the balance again, and revert
  ERCNNNN_Underpaid() unless after >= before and after - before >= least;
- two words, `token` and `least`: read what it holds of the token (balanceOf, reverting
  ERCNNNN_BalanceUnreadable() unless the call succeeded with exactly 32 bytes, as for each
  read), reverting ERCNNNN_Underpaid() if that's nothing; read the payee's balance; call
  transfer(payee, held - 1), whatever it answers or does ignored; read the payee's balance
  again, and revert ERCNNNN_Underpaid() unless after >= before and after - before + 1 >= least.

The payment is the payee's gain, as the runner has it: a token answering false moves nothing, a
taxed token's gain is what arrived, one that burns or misroutes shows no gain. Each call the
forwarder makes is given all the gas left.

It always sends one unit of a token less than it holds, so its balance slot in the token is
never cleared and set again (forks.py's storage_set), and its check allows for the unit that
stays behind: the payee gains at least one unit under `least`.

Its address is Arachnid's deployer's for its initcode, which holds the payee: a payee's
forwarder is at one address on every chain. `make NETWORK-forwarder-deploy` deploys it where it
isn't, through the configuration's nodes:

    python -m bruno.forwarder NETWORK KEY CONFIG PAYEE
"""

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address

from .abi import BALANCE_OF, address_word, selector, word
from .accounts import read_key
from .assembler import Assembly, Op
from .chain import Chain
from .deployer import arachnid_address, deploy_code, network_chain
from .evm import NATIVE_ASSET, WARM_ACCOUNT_ACCESS, EncodedCall
from .forks import GasSchedule
from .runner import BALANCE_UNREADABLE, UNDERPAID, call_failed, revert_with

TRANSFER = selector("transfer(address,uint256)")
# a word, and the calldata of each of the forwarder's calls to a token
_WORD = 32
_BALANCE_OF_BYTES = 4 + _WORD
_TRANSFER_BYTES = 4 + 2 * _WORD
# the initcode before the runtime: PUSH2 its length, DUP1, PUSH1 where it starts, PUSH0,
# CODECOPY, PUSH0, RETURN
_INITCODE_PREFIX_BYTES = 10


@dataclass(frozen=True, slots=True)
class Forwarder:
    """A payee's forwarder: its code, the initcode that deploys it, and where."""

    payee: ChecksumAddress
    runtime: bytes
    initcode: bytes
    address: ChecksumAddress

    def forwarding(self, asset: ChecksumAddress, least: int) -> EncodedCall:
        """The call that forwards all it holds of `asset` (a token, or ERC-7528's address for
        ETH) to the payee, reverting unless the payee gains `least` (for a token, less the one
        unit kept)."""
        if asset == NATIVE_ASSET:
            return EncodedCall(self.address, word(least))
        return EncodedCall(self.address, address_word(asset) + word(least))

    def token_gas(self, schedule: GasSchedule) -> int:
        """The most gas forwarding a token takes of the forwarder's own, under `schedule`: all
        but what the token's balanceOf and transfer use."""
        return _program(self.payee, schedule.cold_account_access, 0, eth_first=False).gas

    def eth_gas(self, schedule: GasSchedule, *, new_payee: bool) -> int:
        """The most gas forwarding ETH takes under `schedule`, to a payee whose code uses none:
        a `new_payee` is an account the ETH creates (no balance, no nonce, no code)."""
        sending = schedule.call_value + (schedule.new_account if new_payee else 0)
        return _program(self.payee, schedule.cold_account_access, sending, eth_first=True).gas


def forwarder_of(payee: ChecksumAddress) -> Forwarder:
    """`payee`'s forwarder."""
    runtime = _program(payee, 0, 0, eth_first=False).code
    initcode = (
        bytes([Op.PUSH2])
        + len(runtime).to_bytes(2)
        + bytes([Op.DUP1, Op.PUSH1, _INITCODE_PREFIX_BYTES, Op.PUSH0, Op.CODECOPY])
        + bytes([Op.PUSH0, Op.RETURN])
        + runtime
    )
    return Forwarder(payee, runtime, initcode, arachnid_address(initcode))


@dataclass(frozen=True, slots=True)
class _Program:
    code: bytes
    gas: int


def _program(payee: ChecksumAddress, cold: int, sending: int, *, eth_first: bool) -> _Program:
    """The forwarder's code for `payee`, and the gas of the path laid straight after its
    calldata's length is read: a token's, as it's deployed, or ETH's (`eth_first`), laid out
    so only for the assembler to sum it. `cold` is an account's first access, `sending` what a
    CALL is charged for the value it sends: both only count, the code the same whatever they
    are."""
    asm = Assembly()
    asm.op(Op.CALLDATASIZE, Op.ISZERO).jumpi("paid")
    asm.push(2 * _WORD).op(Op.CALLDATASIZE, Op.LT).jumpi("eth")
    if eth_first:
        asm.label("eth")
        _forward_eth(asm, payee, cold, sending)
        asm.end()
        _forward_token(asm, payee, cold)
    else:
        _forward_token(asm, payee, cold)
        asm.end()
        asm.label("eth")
        _forward_eth(asm, payee, cold, sending)
    asm.label("paid")
    asm.op(Op.STOP)
    call_failed(asm)
    revert_with(asm, "underpaid", UNDERPAID)
    revert_with(asm, "balance_unreadable", BALANCE_UNREADABLE)
    assembled = asm.assemble(())
    return _Program(assembled.code, assembled.gas)


def _forward_eth(asm: Assembly, payee: ChecksumAddress, cold: int, sending: int) -> None:
    """Send the payee all the ETH held, and require its balance to rise by calldata's one word."""
    asm.push_address(payee).balance(cold)  # stack: before
    # CALL's arguments, last pushed first: gas, to, value, no args, no return
    asm.push(0).push(0).push(0).push(0).op(Op.SELFBALANCE).push_address(payee).op(Op.GAS)
    asm.value_call(WARM_ACCOUNT_ACCESS + sending)
    asm.op(Op.ISZERO).jumpi("call_failed")
    asm.push_address(payee).balance(WARM_ACCOUNT_ACCESS)  # stack: before after
    _require_gain(asm, least_at=0)
    asm.op(Op.STOP)


def _forward_token(asm: Assembly, payee: ChecksumAddress, cold: int) -> None:
    """Send the payee all but one unit held of the token (calldata's first word), and require
    its balance to rise by calldata's second word, less the unit."""
    asm.op(Op.ADDRESS)
    _balance_of(asm, cold)  # stack: held
    asm.op(Op.DUP1, Op.ISZERO).jumpi("underpaid")
    asm.push_address(payee)
    _balance_of(asm, WARM_ACCOUNT_ACCESS)  # stack: held before
    # memory[0:68] = transfer(payee, held - 1); whatever it answers is left unread
    asm.push(int.from_bytes(TRANSFER) << 224).mstore_at(0)
    asm.push_address(payee).mstore_at(4)
    asm.push(1).op(Op.DUP3, Op.SUB).mstore_at(_BALANCE_OF_BYTES)
    asm.push(0).push(0).push(_TRANSFER_BYTES).push(0).push(0)
    asm.push(0).op(Op.CALLDATALOAD, Op.GAS).client_call(WARM_ACCOUNT_ACCESS)
    asm.op(Op.POP)
    asm.push_address(payee)
    _balance_of(asm, WARM_ACCOUNT_ACCESS)  # stack: held before after
    _require_gain(asm, least_at=_WORD, kept=1)
    asm.op(Op.STOP)


def _balance_of(asm: Assembly, access: int) -> None:
    """Replace the account on the stack with its balance of the token (calldata's first word),
    the token costing `access`: memory[0:36] = balanceOf(account), its answer to memory[0:32]."""
    asm.push(int.from_bytes(BALANCE_OF) << 224).mstore_at(0)
    asm.mstore_at(4)
    asm.push(_WORD).push(0).push(_BALANCE_OF_BYTES).push(0)
    asm.push(0).op(Op.CALLDATALOAD, Op.GAS).client_staticcall(access)
    asm.op(Op.ISZERO).jumpi("balance_unreadable")
    # a token with no code answers nothing: success, but no 32 bytes
    asm.op(Op.RETURNDATASIZE).push(_WORD).op(Op.EQ, Op.ISZERO).jumpi("balance_unreadable")
    asm.mload_at(0)


def _require_gain(asm: Assembly, *, least_at: int, kept: int = 0) -> None:
    """With the payee's balance before and after on the stack: Underpaid() unless it rose by
    the word of calldata at `least_at`, allowing for `kept` units left behind."""
    # after < before: the payee lost funds
    asm.op(Op.DUP2, Op.DUP2, Op.LT).jumpi("underpaid")
    asm.op(Op.SUB)  # stack: after - before
    if kept:
        asm.push(kept).op(Op.ADD)
    asm.push(least_at).op(Op.CALLDATALOAD, Op.DUP2, Op.LT).jumpi("underpaid")


async def _main(network: str, key: Path, config: Path, payee: str) -> None:
    chain_id, chain_config = network_chain(network, config)
    own = chain_config.payee if chain_config.payee is not None else payee
    forwarder = forwarder_of(to_checksum_address(own))
    async with httpx.AsyncClient() as client:
        chain = Chain(client, chain_id, chain_config.nodes)
        await chain.check_chain()
        what = f"{forwarder.payee}'s forwarder"
        await deploy_code(chain, read_key(key), forwarder.initcode, forwarder.runtime, what)


if __name__ == "__main__":
    asyncio.run(_main(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]))
