"""The smart wallets' gas schedule (bruno/wallets.py), measured: each wallet deployed through its
vendored factory on the local chain, its signature check's gas and signature's length as the
schedule says, at this fork, and the deployment that makes it the one the schedule finds."""

from itertools import count

import pytest
from eth_account import Account
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from bruno.eip3009 import TransferAuthorization
from bruno.forks import GasSchedule
from bruno.tests import kernel, smartwallet
from bruno.tests.localchain import SMART_WALLET_FACTORY_1_1, USDC, USDC_DOMAIN, LocalChain
from bruno.tests.transfers import NEVER, Signs, by_kernel, by_key, by_wallet, signed
from bruno.wallets import COINBASE_SMART_WALLET, COINBASE_SMART_WALLET_1_1, KERNEL_3_2, WalletGas

# a fixed nonce: nothing is sent, so it's never spent
_NONCE = keccak(text="test_wallets nonce")
_CHEAPER_V = 27
# a payee no one uses
_PAYEE = to_checksum_address("0x" + "fee1" * 10)


# installed by eth_call's state override: copies its calldata to memory, reads GAS, calls the
# token with it, reads GAS again, and returns the call's success and the difference (memory is
# expanded before the first GAS, so only the call's own gas is between them)
_PROBE = to_checksum_address("0x" + "be" * 20)


def _probe(token: str) -> str:
    return (
        "0x365f5f37"  # CALLDATACOPY(0, 0, CALLDATASIZE)
        + "5a"  # GAS: g1
        + "5f5f365f5f73"
        + token.removeprefix("0x").lower()
        + "5af1"  # CALL(GAS, token, 0, ...)
        + "5a905f52"  # GAS: g2; success to memory[0]
        + "9003602052"  # g1 - g2 to memory[32]
        + "60405ff3"  # RETURN 64 bytes
    )


async def _execution(chain: LocalChain, transfer: TransferAuthorization) -> int:
    """A zero-value transfer's gas, measured in the EVM: exact, unlike an estimate (nodes
    search to within a margin of it) or a receipt (its calldata floor may be what's charged)."""
    tx = {"to": _PROBE, "data": "0x" + transfer.calldata().hex(), "gas": hex(_PROBE_GAS)}
    overrides = {_PROBE: {"code": _probe(USDC.address)}}
    answer = bytes.fromhex((await chain.rpc("eth_call", [tx, "latest", overrides]))[2:])
    assert answer[:32] == (1).to_bytes(32), "the transfer failed"
    return int.from_bytes(answer[32:64])


# ample for a zero-value transfer, a wallet's signature check included
_PROBE_GAS = 1_000_000


async def _signed(sign: Signs, chain_id: int, payer: ChecksumAddress) -> TransferAuthorization:
    """`payer`'s zero-value transfer to the payee no one uses, never expiring, its nonce the
    fixed one: signed by `sign`."""
    return await signed(sign, USDC_DOMAIN, chain_id, USDC.address, payer, _PAYEE, 0, NEVER, _NONCE)


async def _by_key(chain_id: int) -> TransferAuthorization:
    """A key's zero-value transfer, signed with v 27: the token's cheaper path (28 takes 20 gas
    more), so a wallet's figure over it is the larger, and the quote never short. The first of
    a fixed sequence of keys whose signature has it, so the measurement is the same each run."""
    for i in count():
        key = Account.from_key(keccak(text=f"test_wallets key {i}"))
        transfer = await _signed(by_key(key), chain_id, key.address)
        if transfer.signature[64] == _CHEAPER_V:
            return transfer
    raise AssertionError("unreachable")


@pytest.mark.parametrize(
    ("wallet_gas", "factory"),
    [
        (COINBASE_SMART_WALLET, smartwallet.FACTORY),
        (COINBASE_SMART_WALLET_1_1, SMART_WALLET_FACTORY_1_1),
    ],
    ids=["v1", "v1.1"],
)
async def test_coinbase_smart_wallet(
    chain: LocalChain, schedule: GasSchedule, wallet_gas: WalletGas, factory: ChecksumAddress
) -> None:
    """A Coinbase Smart Wallet owned by a key, each version from its own factory: its transfer's
    gas over a key's own, and its signature's length."""
    chain_id = await chain.chain_id()
    owner = Account.from_key(keccak(text="test_wallets owner"))
    wallet = await smartwallet.create(chain, owner, factory=factory)
    by_the_wallet = await _signed(by_wallet(chain, wallet, owner), chain_id, wallet)

    verify_gas = await _execution(chain, by_the_wallet) - await _execution(
        chain, await _by_key(chain_id)
    )

    assert wallet_gas.factory == factory
    assert (verify_gas, len(by_the_wallet.signature)) == (
        wallet_gas.verify_gas[schedule.name],
        wallet_gas.signature_bytes,
    )


async def test_kernel(chain: LocalChain, schedule: GasSchedule) -> None:
    """ZeroDev's Kernel v3.2 owned by a key, as AgentKit's ZeroDevWalletProvider makes it
    (through the meta-factory, the intent executor installed): its transfer's gas over a key's
    own, and its signature's length; and its deployment is the one the schedule names."""
    chain_id = await chain.chain_id()
    owner = Account.from_key(keccak(text="test_wallets owner"))
    account = await kernel.create(chain, owner)
    by_the_account = await _signed(by_kernel(chain_id, account, owner), chain_id, account)
    verify_gas = await _execution(chain, by_the_account) - await _execution(
        chain, await _by_key(chain_id)
    )

    assert KERNEL_3_2.deploys(kernel.deployment(account, owner))
    assert (verify_gas, len(by_the_account.signature)) == (
        KERNEL_3_2.verify_gas[schedule.name],
        KERNEL_3_2.signature_bytes,
    )
