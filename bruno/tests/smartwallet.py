"""Coinbase Smart Wallet, a contract payer: an account created through its factory, owned by a
key, which signs as the account does for ERC-1271. Its isValidSignature takes the owner's
signature of the account's replaySafeHash of a hash (its own EIP-712 domain around it), wrapped
as SignatureWrapper(ownerIndex, signatureData), so far longer than a key's 65 bytes."""

from eth_abi.abi import encode
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress, Hash32
from eth_utils.address import to_checksum_address

from bruno.tests.localchain import SMART_WALLET_FACTORY as FACTORY
from bruno.tests.localchain import LocalChain


def owners(owner: LocalAccount) -> list[bytes]:
    """createAccount's owners: an address owner is its ABI-encoded address."""
    return [encode(["address"], [owner.address])]


async def address_of(chain: LocalChain, owner: LocalAccount, nonce: int = 0) -> ChecksumAddress:
    """The account `owner`'s owners and `nonce` make: the factory's getAddress, deployed or not."""
    answer = await chain.call(
        FACTORY, "getAddress(bytes[],uint256)", ["bytes[]", "uint256"], [owners(owner), nonce]
    )
    return to_checksum_address(answer[12:32])


async def create(chain: LocalChain, owner: LocalAccount, nonce: int = 0) -> ChecksumAddress:
    """The account deployed, from a dev account: its address."""
    await chain.send(
        await chain.account(0),
        FACTORY,
        "createAccount(bytes[],uint256)",
        ["bytes[]", "uint256"],
        [owners(owner), nonce],
    )
    return await address_of(chain, owner, nonce)


async def sign(
    chain: LocalChain, account: ChecksumAddress, owner: LocalAccount, hash_: bytes
) -> bytes:
    """`owner`'s signature of `hash_` as `account`'s: the account's replaySafeHash of it signed,
    wrapped with the owner's index (0, the first)."""
    replay = await chain.call(account, "replaySafeHash(bytes32)", ["bytes32"], [hash_])
    signed = owner.unsafe_sign_hash(Hash32(replay))
    return encode(["(uint256,bytes)"], [(0, bytes(signed.signature))])
