"""Smart wallets' gas schedule: for each wallet the relay knows, the deployment that makes it
(its factory called, or a meta-factory calling it), and what that wallet's signature check adds
to an EIP-3009 transfer it pays by, over a key's, per fork.
A quote prices a contract payer's transfer by it before the payment's signed (relay.py); the
payment itself is priced by its transaction's estimate (plan.py).

`verify_gas` is the gas a wallet's transfer takes (its bytes overload, the token checking the
wallet's ERC-1271 signature) less a key's (the (v, r, s) overload, ecrecover, with v 27: the
token's path for 28 takes 20 gas more, so this is the larger figure): both zero-value
transfers to the same payee, measured exactly in the EVM (a probe's GAS before and after the
call; an estimate may stop short of the exact figure, and a receipt may be its calldata
floor). `signature_bytes` is its inner signature's length, before any ERC-6492
wrapper. Each figure is measured by bruno/tests/test_wallets.py, on an anvil of each fork,
against the wallet's contracts fetched from Ethereum (tests/fixtures/evm): `make check` fails
with the measured figure if a fork or a contract moves one.

A wallet the relay doesn't know is priced at the largest figures here. Passkey (WebAuthn)
owners aren't here yet: their P-256 check costs far more, and which wallets take them is still
to be researched (docs/development/plans/smart-wallets.md)."""

from collections.abc import Mapping
from dataclasses import dataclass

from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from .erc6492 import Deployment
from .forks import GasSchedule

# a meta-factory's call (ZeroDev's FactoryStaker): deployWithFactory(factory, initData, salt),
# the factory it calls first
_DEPLOY_WITH_FACTORY = keccak(text="deployWithFactory(address,bytes,bytes32)")[:4]


@dataclass(frozen=True, slots=True)
class Verification:
    """What a contract payer's signature check adds to its transfer: `gas` (execution), and its
    signature's length, `signature_bytes`."""

    gas: int
    signature_bytes: int


@dataclass(frozen=True, slots=True)
class WalletGas:
    """A wallet `factory` deploys, called directly or, if `through` names one, by that
    meta-factory; its signature's length, and its `verify_gas` by fork."""

    name: str
    factory: ChecksumAddress
    signature_bytes: int
    verify_gas: Mapping[str, int]
    through: ChecksumAddress | None = None

    def deploys(self, deployment: Deployment) -> bool:
        """Whether `deployment` makes this wallet: its factory called, or its meta-factory's
        deployWithFactory naming it."""
        if self.through is None:
            return deployment.factory == self.factory
        data = deployment.factory_data
        return (
            deployment.factory == self.through
            and data[:4] == _DEPLOY_WITH_FACTORY
            and len(data) >= 36
            and data[4:16] == bytes(12)
            and to_checksum_address(data[16:36]) == self.factory
        )

    def verification(self, schedule: GasSchedule) -> Verification:
        return Verification(self.verify_gas[schedule.name], self.signature_bytes)


# Coinbase Smart Wallet v1 with a key as its owner (viem's toCoinbaseSmartAccount's default): its
# factory at the same address on Ethereum, Base and Sepolia; its signature
# SignatureWrapper(ownerIndex, the owner's 65 bytes) ABI-encoded
COINBASE_SMART_WALLET = WalletGas(
    name="Coinbase Smart Wallet v1, a key owner",
    factory=to_checksum_address("0x0BA5ED0c6AA8c49038F819E587E2633c4A9F428a"),
    signature_bytes=224,
    # measured 2026-10-27 (test_wallets.py)
    verify_gas={"prague": 14_693, "amsterdam": 15_093},
)
# v1.1 (coinbase/smart-wallet v1.1.0, July 2025), what CDP's smart accounts (AgentKit's among
# them) and the Base Account SDK's new accounts are: signing as v1 does, built anew (via_ir,
# Prague), so its check costs differently; its factory at the same address on each chain
COINBASE_SMART_WALLET_1_1 = WalletGas(
    name="Coinbase Smart Wallet v1.1, a key owner",
    factory=to_checksum_address("0xBA5ED110eFDBa3D005bfC882d75358ACBbB85842"),
    signature_bytes=224,
    # measured 2026-10-27 (test_wallets.py)
    verify_gas={"prague": 14_368, "amsterdam": 14_768},
)
# ZeroDev's Kernel v3.2 with a key as its owner, as AgentKit's ZeroDevWalletProvider makes it:
# created through ZeroDev's meta-factory (the factory its accounts name, for every Kernel v3),
# which calls v3.2's factory; ZeroDev's ECDSA validator its root, the intent executor installed;
# its signature the validator's identifier (0x01 and its address), then the owner's 65 bytes
KERNEL_3_2 = WalletGas(
    name="Kernel v3.2, a key owner",
    factory=to_checksum_address("0x7a1dBAB750f12a90EB1B60D2Ae3aD17D4D81EfFe"),
    through=to_checksum_address("0xd703aaE79538628d27099B8c4f621bE4CCd142d5"),
    signature_bytes=86,
    # measured 2026-10-28 (test_wallets.py)
    verify_gas={"prague": 16_636, "amsterdam": 17_436},
)
WALLETS = (COINBASE_SMART_WALLET, COINBASE_SMART_WALLET_1_1, KERNEL_3_2)


def verification(deployment: Deployment | None, schedule: GasSchedule) -> Verification:
    """The verification of the wallet `deployment` makes, under `schedule`: its own if it's
    known, else the largest gas and signature length of those that are."""
    for wallet in WALLETS:
        if deployment is not None and wallet.deploys(deployment):
            return wallet.verification(schedule)
    return Verification(
        max(w.verify_gas[schedule.name] for w in WALLETS),
        max(w.signature_bytes for w in WALLETS),
    )
