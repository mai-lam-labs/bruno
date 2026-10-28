"""ZeroDev's Kernel v3.2, a contract payer, as AgentKit's ZeroDevWalletProvider makes it (and
@zerodev/sdk's createKernelAccount with these options): an account created through ZeroDev's
meta-factory, which calls Kernel v3.2's factory; owned by a key through the ECDSA validator as
its root validator; ZeroDev's intent executor installed as it's created. Its isValidSignature
takes the owner's signature of its own EIP-712 Kernel(bytes32 hash) around a hash, after the
validator's identifier (0x01 and its address), as the SDK signs typed data."""

from eth_abi.abi import encode
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress, Hash32
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from bruno.chain import Chain
from bruno.erc6492 import MAGIC, Deployment
from bruno.tests.localchain import KERNEL_ECDSA_VALIDATOR as VALIDATOR
from bruno.tests.localchain import KERNEL_FACTORY as FACTORY
from bruno.tests.localchain import KERNEL_META_FACTORY as META_FACTORY
from bruno.tests.localchain import ZERODEV_INTENT_EXECUTOR as INTENT_EXECUTOR
from bruno.tests.localchain import LocalChain

VERSION = "0.3.2"

# a root validator's id: its type (a validator, 0x01) and its address
_VALIDATOR_ID = b"\x01" + bytes.fromhex(VALIDATOR[2:])
# an executor module's type, for installModule
_EXECUTOR = 2
_INITIALIZE = keccak(text="initialize(bytes21,address,bytes,bytes,bytes[])")[:4]
_INSTALL_MODULE = keccak(text="installModule(uint256,address,bytes)")[:4]
_DEPLOY_WITH_FACTORY = keccak(text="deployWithFactory(address,bytes,bytes32)")[:4]
_GET_ADDRESS = keccak(text="getAddress(bytes,bytes32)")[:4]

_DOMAIN_TYPEHASH = keccak(
    text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)
_KERNEL_TYPEHASH = keccak(text="Kernel(bytes32 hash)")


def _install_intent_executor() -> bytes:
    """installModule's calldata for the intent executor (@zerodev/intent's
    installIntentExecutor): no hook (the zero address), and empty data for it and the hook."""
    data = bytes(20) + encode(["bytes", "bytes"], [b"", b""])
    return _INSTALL_MODULE + encode(
        ["uint256", "address", "bytes"], [_EXECUTOR, INTENT_EXECUTOR, data]
    )


def init_data(owner: LocalAccount) -> bytes:
    """The account's initialize calldata: the ECDSA validator its root, `owner` its key, no
    hook, and the intent executor installed."""
    return _INITIALIZE + encode(
        ["bytes21", "address", "bytes", "bytes", "bytes[]"],
        [
            _VALIDATOR_ID,
            "0x" + "00" * 20,
            bytes.fromhex(owner.address[2:]),
            b"",
            [_install_intent_executor()],
        ],
    )


def factory_data(owner: LocalAccount, index: int = 0) -> bytes:
    """The meta-factory's deployWithFactory calldata: what deploys the account `owner` owns."""
    return _DEPLOY_WITH_FACTORY + encode(
        ["address", "bytes", "bytes32"], [FACTORY, init_data(owner), index.to_bytes(32)]
    )


def deployment(account: ChecksumAddress, owner: LocalAccount, index: int = 0) -> Deployment:
    return Deployment(account, META_FACTORY, factory_data(owner, index))


async def address_of(chain: LocalChain, owner: LocalAccount, index: int = 0) -> ChecksumAddress:
    """The account `owner` and `index` make: the factory's getAddress, deployed or not."""
    answer = await chain.call(
        FACTORY,
        "getAddress(bytes,bytes32)",
        ["bytes", "bytes32"],
        [init_data(owner), index.to_bytes(32)],
    )
    return to_checksum_address(answer[12:32])


async def address_on(chain: Chain, owner: LocalAccount, index: int = 0) -> ChecksumAddress:
    """address_of on a live chain: the account `owner` and `index` make there."""
    answer = await chain.call(
        FACTORY, _GET_ADDRESS + encode(["bytes", "bytes32"], [init_data(owner), index.to_bytes(32)])
    )
    return to_checksum_address(answer[12:32])


async def create(chain: LocalChain, owner: LocalAccount, index: int = 0) -> ChecksumAddress:
    """The account deployed through the meta-factory, from a dev account: its address."""
    await chain.send(
        await chain.account(0),
        META_FACTORY,
        "deployWithFactory(address,bytes,bytes32)",
        ["address", "bytes", "bytes32"],
        [FACTORY, init_data(owner), index.to_bytes(32)],
    )
    return await address_of(chain, owner, index)


def kernel_hash(chain_id: int, account: ChecksumAddress, hash_: bytes) -> bytes:
    """`hash_` as the account's EIP-712 Kernel(bytes32 hash), under its own domain: what its
    owner signs, computed as the SDK does, so known before the account is deployed."""
    separator = keccak(
        encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
            [_DOMAIN_TYPEHASH, keccak(text="Kernel"), keccak(text=VERSION), chain_id, account],
        )
    )
    return keccak(
        b"\x19\x01" + separator + keccak(encode(["bytes32", "bytes32"], [_KERNEL_TYPEHASH, hash_]))
    )


def sign(chain_id: int, account: ChecksumAddress, owner: LocalAccount, hash_: bytes) -> bytes:
    """`owner`'s signature of `hash_` as the account's: the validator's identifier, then the
    owner's signature of its Kernel hash."""
    signed = owner.unsafe_sign_hash(Hash32(kernel_hash(chain_id, account, hash_)))
    return _VALIDATOR_ID + bytes(signed.signature)


def sign_undeployed(
    chain_id: int, account: ChecksumAddress, owner: LocalAccount, hash_: bytes, index: int = 0
) -> bytes:
    """`owner`'s signature of `hash_` as the account's before it's deployed: ERC-6492-wrapped,
    the meta-factory and its calldata around the signature the account will check."""
    inner = sign(chain_id, account, owner, hash_)
    wrapper = encode(
        ["address", "bytes", "bytes"], [META_FACTORY, factory_data(owner, index), inner]
    )
    return wrapper + MAGIC
