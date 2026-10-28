"""Coinbase Smart Wallet, a contract payer: an account created through its factory, owned by a
key, which signs as the account does for ERC-1271. Its isValidSignature takes the owner's
signature of the account's replaySafeHash of a hash (its own EIP-712 domain around it), wrapped
as SignatureWrapper(ownerIndex, signatureData), so far longer than a key's 65 bytes. v1 and
v1.1 sign alike; each has its own factory (`factory`, v1's unless another is given), so the
same owners make a different account in each."""

from eth_abi.abi import encode
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress, Hash32
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from bruno.erc6492 import MAGIC, Deployment
from bruno.tests.localchain import SMART_WALLET_FACTORY as FACTORY
from bruno.tests.localchain import LocalChain


def owners(owner: LocalAccount) -> list[bytes]:
    """createAccount's owners: an address owner is its ABI-encoded address."""
    return [encode(["address"], [owner.address])]


async def address_of(
    chain: LocalChain, owner: LocalAccount, nonce: int = 0, factory: ChecksumAddress = FACTORY
) -> ChecksumAddress:
    """The account `owner`'s owners and `nonce` make: the factory's getAddress, deployed or not."""
    answer = await chain.call(
        factory, "getAddress(bytes[],uint256)", ["bytes[]", "uint256"], [owners(owner), nonce]
    )
    return to_checksum_address(answer[12:32])


async def create(
    chain: LocalChain, owner: LocalAccount, nonce: int = 0, factory: ChecksumAddress = FACTORY
) -> ChecksumAddress:
    """The account deployed, from a dev account: its address."""
    await chain.send(
        await chain.account(0),
        factory,
        "createAccount(bytes[],uint256)",
        ["bytes[]", "uint256"],
        [owners(owner), nonce],
    )
    return await address_of(chain, owner, nonce, factory)


# the wallet's own EIP-712 domain, and the message its replaySafeHash wraps a hash in
_DOMAIN_TYPEHASH = keccak(
    text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)
_MESSAGE_TYPEHASH = keccak(text="CoinbaseSmartWalletMessage(bytes32 hash)")
_CREATE_ACCOUNT = keccak(text="createAccount(bytes[],uint256)")[:4]


def factory_data(owner: LocalAccount, nonce: int = 0) -> bytes:
    """createAccount(owners, nonce)'s calldata: what deploys the account `owner` owns."""
    return _CREATE_ACCOUNT + encode(["bytes[]", "uint256"], [owners(owner), nonce])


def deployment(
    account: ChecksumAddress,
    owner: LocalAccount,
    nonce: int = 0,
    factory: ChecksumAddress = FACTORY,
) -> Deployment:
    return Deployment(account, factory, factory_data(owner, nonce))


def replay_safe_hash(chain_id: int, account: ChecksumAddress, hash_: bytes) -> bytes:
    """`account`'s replaySafeHash of `hash_`, computed as the wallet does: so known before the
    wallet is deployed."""
    separator = keccak(
        encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
            [
                _DOMAIN_TYPEHASH,
                keccak(text="Coinbase Smart Wallet"),
                keccak(text="1"),
                chain_id,
                account,
            ],
        )
    )
    return keccak(
        b"\x19\x01" + separator + keccak(encode(["bytes32", "bytes32"], [_MESSAGE_TYPEHASH, hash_]))
    )


def sign_undeployed(
    chain_id: int,
    account: ChecksumAddress,
    owner: LocalAccount,
    hash_: bytes,
    nonce: int = 0,
    factory: ChecksumAddress = FACTORY,
) -> bytes:
    """`owner`'s signature of `hash_` as `account`'s before it's deployed: ERC-6492-wrapped,
    the factory and its calldata around the signature the wallet will check."""
    signed = owner.unsafe_sign_hash(Hash32(replay_safe_hash(chain_id, account, hash_)))
    inner = encode(["(uint256,bytes)"], [(0, bytes(signed.signature))])
    wrapper = encode(["address", "bytes", "bytes"], [factory, factory_data(owner, nonce), inner])
    return wrapper + MAGIC


async def sign(
    chain: LocalChain, account: ChecksumAddress, owner: LocalAccount, hash_: bytes
) -> bytes:
    """`owner`'s signature of `hash_` as `account`'s: the account's replaySafeHash of it signed,
    wrapped with the owner's index (0, the first)."""
    replay = await chain.call(account, "replaySafeHash(bytes32)", ["bytes32"], [hash_])
    signed = owner.unsafe_sign_hash(Hash32(replay))
    return encode(["(uint256,bytes)"], [(0, bytes(signed.signature))])
