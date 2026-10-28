"""Set up a local anvil chain for the relay's mechanism: the runner's deployer, the contracts
a client's calls and the prepaid runner name, and a token for each payment path.

The deployer is deployed as on any chain: through Arachnid's deterministic deployer, which
anvil installs by default.

`make anvil-start` runs this right after anvil comes up:

    python -m bruno.tests.localchain http://127.0.0.1:8546

Every contract in fixtures/evm/manifest.json is installed at its address there
(anvil_setCode), its runtime code fetched from a live chain (each entry's note says which,
and at what block). setCode gives code without storage, so each entry's `storage` (what its
constructor set, read off the same chain) is written after it; USDC is initialised through
its own initialize* functions instead.

The tokens, one per way a token can be paid or approved:
- USDC (FiatTokenV2_2): EIP-3009 and EIP-2612;
- UNI: EIP-2612, no EIP-3009;
- WETH9: neither;
- USDT: neither, and its transfer returns no bool.
Native ETH needs nothing. Balances are left to the tests: mint().

Uniswap's pools between them hold real liquidity, put there through their own code: each v3
pool initialised at its price and given a full-range position through Uniswap's position
manager, the v2 pair funded and minted. ETH is at 2,500 USD. Swaps go through Uniswap's
SwapRouter02, which finds the pools at their Ethereum addresses as it would there.

Idempotent: running it on an already set-up chain changes nothing.
"""

import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from eth_abi.abi import encode
from eth_account import Account
from eth_typing import ChecksumAddress, HexStr
from eth_utils.abi import function_signature_to_4byte_selector
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from bruno.config import Eip712Domain
from bruno.deployer import ARACHNID, DEPLOYER, RUNTIME, arachnid_calldata
from bruno.evm import EncodedCall
from bruno.net.jsonrpc import JsonRpcError, call

FIXTURES = Path(__file__).parent / "fixtures" / "evm"
RECEIPT_POLL_SECONDS = 0.1
RECEIPT_ATTEMPTS = 50
# anvil's dev accounts: #1 owns the tokens (USDC's owner, master minter, pauser and
# blacklister), #2 plays a user's wallet where one needs ETH
TOKEN_OWNER_ACCOUNT = 1
DEPOSITOR_ACCOUNT = 2
# the test service instance's float, whose key its concurrency accounts derive from: none of
# anvil's dev accounts
TEST_FLOAT = Account.from_key(keccak(text="the test instance's float"))
# PUSH0 PUSH0 REVERT: code that refuses every call and every payment
REVERT_ALWAYS = "0x5f5ffd"


@dataclass(frozen=True, slots=True)
class LocalToken:
    """A token on the local chain, and how a test gets some or approves it: `balance_slot`
    and `allowance_slot` are its mappings' slots, written directly (no transaction, so no
    block mined); a token without a balance slot (WETH) is wrapped instead."""

    contract: str
    address: ChecksumAddress
    name: str
    symbol: str
    decimals: int
    balance_slot: int | None = None
    allowance_slot: int | None = None


def _manifest() -> dict[str, Any]:
    with (FIXTURES / "manifest.json").open() as f:
        return json.load(f)


def _token(contract: str, name: str, symbol: str, decimals: int) -> LocalToken:
    entry = _manifest()["contracts"][contract]
    return LocalToken(
        contract=contract,
        address=to_checksum_address(entry["address"]),
        name=name,
        symbol=symbol,
        decimals=decimals,
        balance_slot=entry.get("balance_slot"),
        allowance_slot=entry.get("allowance_slot"),
    )


# Calibur, an EIP-7702 delegate a client may sign its authorization to, and Permit2
CALIBUR = to_checksum_address(_manifest()["contracts"]["calibur"]["address"])
PERMIT2 = to_checksum_address(_manifest()["contracts"]["permit2"]["address"])
USDC = _token("usdc_fiat_token_v2_2", "USD Coin", "USDC", 6)
UNI = _token("uni", "Uniswap", "UNI", 18)
WETH = _token("weth9", "Wrapped Ether", "WETH", 18)
USDT = _token("usdt", "Tether USD", "USDT", 6)
TOKENS = (USDC, UNI, WETH, USDT)
# the local USDC's EIP-712 domain, as FiatTokenV2_2 is initialised (_initialize_usdc)
USDC_DOMAIN = Eip712Domain("USD Coin", "2")


def _address(contract: str) -> ChecksumAddress:
    return to_checksum_address(_manifest()["contracts"][contract]["address"])


# Coinbase Smart Wallet's factory: a contract payer's accounts (tests/smartwallet.py)
SMART_WALLET_FACTORY = _address("coinbase_smart_wallet_factory")
# and v1.1's, which CDP's and the Base Account SDK's new accounts come from
SMART_WALLET_FACTORY_1_1 = _address("coinbase_smart_wallet_factory_1_1")
# ERC-4337's EntryPoint v0.7, at its address on every chain
ENTRY_POINT_0_7 = _address("entry_point_0_7")
# ZeroDev's Kernel v3.2, as AgentKit makes it (tests/kernel.py): the meta-factory its accounts
# name, the factory that calls, the root validator, and the executor installed with it
KERNEL_META_FACTORY = _address("kernel_meta_factory")
KERNEL_FACTORY = _address("kernel_factory_3_2")
KERNEL_ECDSA_VALIDATOR = _address("kernel_ecdsa_validator")
ZERODEV_INTENT_EXECUTOR = _address("zerodev_intent_executor_0_0_3")


# Uniswap's pools between them, with liquidity at these prices, and its router
UNISWAP_V2_USDC_WETH = _address("uniswap_v2_usdc_weth")
UNISWAP_V3_USDC_WETH = _address("uniswap_v3_usdc_weth")
UNISWAP_V3_WETH_USDT = _address("uniswap_v3_weth_usdt")
UNISWAP_V3_USDC_USDT = _address("uniswap_v3_usdc_usdt")
SWAP_ROUTER = _address("uniswap_swap_router02")
POSITION_MANAGER = _address("uniswap_v3_position_manager")
# ETH at 2,500 USD: a USDC or USDT base unit (10**-6 USD) is 4 x 10**8 wei
WEI_A_USD_UNIT = 400_000_000
# USDC/WETH's sqrtPriceX96: token0 USDC in token1 WETH, sqrt(4 x 10**8) = 20,000
USDC_WETH_SQRT_PRICE_X96 = 20_000 * 2**96
# WETH/USDT's: token0 WETH in token1 USDT, sqrt(1 / (4 x 10**8)), rounded down
WETH_USDT_SQRT_PRICE_X96 = 2**96 // 20_000
# USDC/USDT's: 1 to 1
USDC_USDT_SQRT_PRICE_X96 = 2**96
# each pool's liquidity: a million dollars a side
POOL_USD = 10**12
POOL_ETH = 400 * 10**18
# a position's mint: anvil's estimate falls short of the position manager's, and Amsterdam prices
# new state higher
MINT_GAS = 5_000_000
# v3's tick range (TickMath), each pool's full range a multiple of its tick spacing
MIN_TICK, MAX_TICK = -887_272, 887_272


class LocalChainError(Exception):
    pass


class LocalChain:
    def __init__(self, rpc_url: str) -> None:
        self._rpc_url = rpc_url

    async def rpc(self, method: str, params: list[Any]) -> Any:
        # a client of its own each time: a test and its fixtures may run on different loops
        async with httpx.AsyncClient() as client:
            try:
                return await call(client, self._rpc_url, method, params)
            except JsonRpcError as e:
                raise LocalChainError(str(e)) from e

    @staticmethod
    def calldata(signature: str, types: list[str], args: list[Any]) -> HexStr:
        return HexStr(
            "0x" + (function_signature_to_4byte_selector(signature) + encode(types, args)).hex()
        )

    async def call(self, to: str, signature: str, types: list[str], args: list[Any]) -> bytes:
        data = self.calldata(signature, types, args)
        return bytes.fromhex((await self.rpc("eth_call", [{"to": to, "data": data}, "latest"]))[2:])

    async def account(self, index: int) -> ChecksumAddress:
        return to_checksum_address((await self.rpc("eth_accounts", []))[index])

    async def send(
        self,
        sender: str,
        to: str,
        signature: str,
        types: list[str],
        args: list[Any],
        gas: int | None = None,
    ) -> dict[str, Any]:
        """Send from an unlocked anvil account and wait for a successful receipt: with `gas`,
        if given, otherwise anvil's estimate."""
        return await self.send_call(
            sender,
            EncodedCall(to_checksum_address(to), self.calldata(signature, types, args)),
            signature,
            gas,
        )

    async def send_call(
        self, sender: str, call: EncodedCall, what: str, gas: int | None = None
    ) -> dict[str, Any]:
        """Send `call` from an unlocked anvil account, at `gas` (none: anvil's estimate), and
        wait for a successful receipt: it."""
        tx = {"from": sender, "to": call.to, "data": call.data, "value": hex(call.value)}
        if gas is not None:
            tx["gas"] = hex(gas)
        receipt = await self.receipt(await self.rpc("eth_sendTransaction", [tx]))
        if receipt["status"] != "0x1":
            raise LocalChainError(f"{what} reverted: {await self._why(receipt['transactionHash'])}")
        return receipt

    async def submit(self, raw_tx: bytes) -> dict[str, Any]:
        """Send a signed transaction and return its receipt, a reverted one included: anvil
        mines a transaction that reverts, and its sender pays the gas."""
        return await self.receipt(await self.rpc("eth_sendRawTransaction", ["0x" + raw_tx.hex()]))

    async def receipt(self, tx_hash: str) -> dict[str, Any]:
        for _ in range(RECEIPT_ATTEMPTS):
            receipt = await self.rpc("eth_getTransactionReceipt", [tx_hash])
            if receipt is not None:
                return receipt
            await asyncio.sleep(RECEIPT_POLL_SECONDS)
        raise LocalChainError(f"no receipt for {tx_hash}")

    async def _why(self, tx_hash: str) -> str:
        """Why a transaction reverted: out of gas, if it used all it was given; otherwise it
        run again as a call, against the state before its block, and what that said."""
        tx = await self.rpc("eth_getTransactionByHash", [tx_hash])
        receipt = await self.rpc("eth_getTransactionReceipt", [tx_hash])
        if receipt["gasUsed"] == tx["gas"]:
            return f"out of gas: it used all of the {int(tx['gas'], 16)} it was given"
        block = hex(int(tx["blockNumber"], 16) - 1)
        call = {"from": tx["from"], "to": tx["to"], "data": tx["input"], "value": tx["value"]}
        try:
            await self.rpc("eth_call", [call, block])
        except LocalChainError as e:
            return str(e)
        return "nothing, run again as a call"

    async def setup(self) -> None:
        await self._install_deployer()
        for name, contract in _manifest()["contracts"].items():
            await self.rpc("anvil_setCode", [contract["address"], _code(name)])
            for slot, value in contract.get("storage", {}).items():
                await self.store(contract["address"], int(slot, 0), value)
        await self._initialize_usdc()
        await self._provide_liquidity()

    async def _install_deployer(self) -> None:
        if await self.code(ARACHNID) in ("0x", "0x0"):
            raise LocalChainError(
                f"no Arachnid deployer at {ARACHNID}: anvil installs it by default"
            )
        if await self.code(DEPLOYER) in ("0x", "0x0"):
            call = EncodedCall(ARACHNID, HexStr("0x" + arachnid_calldata().hex()))
            await self.send_call(await self.account(0), call, "the deployer's deployment")
        if await self.code(DEPLOYER) != "0x" + RUNTIME.hex():
            raise LocalChainError(f"{DEPLOYER} doesn't hold the deployer's runtime")

    async def _initialize_usdc(self) -> None:
        master_minter = await self.call(USDC.address, "masterMinter()", [], [])
        if int.from_bytes(master_minter) != 0:
            return
        owner = await self.account(TOKEN_OWNER_ACCOUNT)
        steps: list[tuple[str, list[str], list[Any]]] = [
            (
                "initialize(string,string,string,uint8,address,address,address,address)",
                ["string", "string", "string", "uint8"] + ["address"] * 4,
                [USDC.name, USDC.symbol, "USD", USDC.decimals, owner, owner, owner, owner],
            ),
            ("initializeV2(string)", ["string"], [USDC.name]),
            ("initializeV2_1(address)", ["address"], [owner]),
            ("initializeV2_2(address[],string)", ["address[]", "string"], [[], USDC.symbol]),
            ("configureMinter(address,uint256)", ["address", "uint256"], [owner, 2**255]),
        ]
        for signature, types, args in steps:
            await self.send(owner, USDC.address, signature, types, args)

    async def _provide_liquidity(self) -> None:
        """Each v3 pool initialised at its price, with a full-range position minted through the
        position manager; the v2 pair funded and minted. Done once: a pool initialised is
        left."""
        if int.from_bytes((await self.call(UNISWAP_V3_USDC_WETH, "slot0()", [], []))[:32]):
            return
        owner = await self.account(TOKEN_OWNER_ACCOUNT)
        # (pool, token0, token1, fee, tick spacing, sqrtPriceX96, amount0, amount1)
        pools = (
            (
                UNISWAP_V3_USDC_WETH,
                USDC,
                WETH,
                500,
                10,
                USDC_WETH_SQRT_PRICE_X96,
                POOL_USD,
                POOL_ETH,
            ),
            (
                UNISWAP_V3_WETH_USDT,
                WETH,
                USDT,
                3000,
                60,
                WETH_USDT_SQRT_PRICE_X96,
                POOL_ETH,
                POOL_USD,
            ),
            (
                UNISWAP_V3_USDC_USDT,
                USDC,
                USDT,
                100,
                1,
                USDC_USDT_SQRT_PRICE_X96,
                POOL_USD,
                POOL_USD,
            ),
        )
        for token in (USDC, WETH, USDT):
            needed = sum(a0 for _, t0, _, _, _, _, a0, _ in pools if t0 is token) + sum(
                a1 for _, _, t1, _, _, _, _, a1 in pools if t1 is token
            )
            await self.mint(token, owner, needed)
            await self.send(
                owner,
                token.address,
                "approve(address,uint256)",
                ["address", "uint256"],
                [POSITION_MANAGER, needed],
            )
        for pool, token0, token1, fee, spacing, price, amount0, amount1 in pools:
            await self.send(owner, pool, "initialize(uint160)", ["uint160"], [price])
            lower = -(-MIN_TICK // spacing) * spacing
            upper = MAX_TICK // spacing * spacing
            params = (
                token0.address,
                token1.address,
                fee,
                lower,
                upper,
                amount0,
                amount1,
                0,
                0,
                owner,
                2**64,
            )
            await self.send(
                owner,
                POSITION_MANAGER,
                "mint((address,address,uint24,int24,int24,uint256,uint256,uint256,uint256,address,uint256))",
                [
                    "(address,address,uint24,int24,int24,uint256,uint256,uint256,uint256,address,uint256)"
                ],
                [params],
                gas=MINT_GAS,
            )
        await self.mint(USDC, UNISWAP_V2_USDC_WETH, POOL_USD)
        await self.mint(WETH, UNISWAP_V2_USDC_WETH, POOL_ETH)
        await self.send(owner, UNISWAP_V2_USDC_WETH, "mint(address)", ["address"], [owner])

    async def store(self, contract: str, slot: int, value: str) -> None:
        await self.rpc("anvil_setStorageAt", [contract, hex(slot), value])

    async def mint(self, token: LocalToken, to: str, amount: int) -> None:
        """`to` given `amount` more of `token`: written into its balance mapping, mining
        nothing; WETH, which has none in the manifest, wrapped from a funded dev account's
        ETH."""
        if token.balance_slot is not None:
            key = keccak(encode(["address", "uint256"], [to, token.balance_slot]))
            held = await self.balance_of(token, to)
            await self.store(token.address, int.from_bytes(key), "0x" + f"{held + amount:064x}")
            return
        if token is not WETH:
            raise LocalChainError(f"{token.symbol} has no balance slot in the manifest")
        depositor = await self.account(DEPOSITOR_ACCOUNT)
        await self._funded(depositor, amount)
        await self.send_call(
            depositor,
            EncodedCall(WETH.address, self.calldata("deposit()", [], []), amount),
            "deposit()",
        )
        await self.send(
            depositor,
            WETH.address,
            "transfer(address,uint256)",
            ["address", "uint256"],
            [to, amount],
        )

    async def set_token_balance(self, token: LocalToken, holder: str, amount: int) -> None:
        """`holder`'s balance of `token` set to `amount`, written into its balance mapping."""
        if token.balance_slot is None:
            raise LocalChainError(f"{token.symbol} has no balance slot in the manifest")
        key = keccak(encode(["address", "uint256"], [holder, token.balance_slot]))
        await self.store(token.address, int.from_bytes(key), "0x" + f"{amount:064x}")

    async def approve(self, token: LocalToken, owner: str, spender: str, amount: int) -> None:
        """`owner`'s allowance to `spender` set to `amount`, written into the allowance
        mapping, mining nothing."""
        if token.allowance_slot is None:
            raise LocalChainError(f"{token.symbol} has no allowance slot in the manifest")
        inner = keccak(encode(["address", "uint256"], [owner, token.allowance_slot]))
        key = keccak(encode(["address"], [spender]) + inner)
        await self.store(token.address, int.from_bytes(key), "0x" + f"{amount:064x}")

    async def _funded(self, account: str, amount: int) -> None:
        """A dev account given ETH, if it holds too little to part with `amount` (and its gas)."""
        held = await self.eth_balance(account)
        if held < amount + 10**18:
            await self.set_eth_balance(account, amount + held + 10**18)

    async def balance_of(self, token: LocalToken, account: str) -> int:
        return int.from_bytes(
            await self.call(token.address, "balanceOf(address)", ["address"], [account])
        )

    async def snapshot(self) -> str:
        """The chain's state now, to go back to (revert)."""
        return str(await self.rpc("evm_snapshot", []))

    async def revert(self, snapshot: str) -> None:
        if not await self.rpc("evm_revert", [snapshot]):
            raise LocalChainError(f"anvil has no snapshot {snapshot} to go back to")

    async def base_fee(self) -> int:
        return int((await self.rpc("eth_getBlockByNumber", ["latest", False]))["baseFeePerGas"], 16)

    async def set_base_fee(self, wei: int) -> None:
        """Set the base fee from the next block on (EIP-1559 then moves it block by block)."""
        await self.rpc("anvil_setNextBlockBaseFeePerGas", [hex(wei)])
        await self.rpc("evm_mine", [])

    @property
    def url(self) -> str:
        return self._rpc_url

    async def chain_id(self) -> int:
        return int(await self.rpc("eth_chainId", []), 16)

    async def set_eth_balance(self, account: str, wei: int) -> None:
        await self.rpc("anvil_setBalance", [account, hex(wei)])

    async def eth_balance(self, account: str) -> int:
        return int(await self.rpc("eth_getBalance", [account, "latest"]), 16)

    async def code(self, account: str) -> str:
        return await self.rpc("eth_getCode", [account, "latest"])

    async def nonce(self, account: str) -> int:
        return int(await self.rpc("eth_getTransactionCount", [account, "latest"]), 16)


async def without_deployer(chain: LocalChain) -> None:
    """The deployer's address as on a network that lacks it: no code, and no nonce (a contract's
    is 1, and CREATE2 can't deploy over an account with one)."""
    await chain.rpc("anvil_setCode", [DEPLOYER, "0x"])
    await chain.rpc("anvil_setNonce", [DEPLOYER, "0x0"])


def _code(contract: str) -> str:
    return (FIXTURES / f"{contract}.hex").read_text().strip()


if __name__ == "__main__":
    asyncio.run(LocalChain(sys.argv[1]).setup())
    tokens = ", ".join(f"{t.symbol} at {t.address}" for t in TOKENS)
    print(f">> local chain ready: deployer at {DEPLOYER}, Calibur, Permit2, x402 proxy, {tokens}")
