"""A chain through its nodes: reads, fees, and sending a transaction and waiting for it.
Each call goes to the first node that answers."""

import asyncio
from typing import Any

import httpx

from .forks import GasSchedule, schedule_at
from .net.jsonrpc import call
from .transactions import Fees, SignedTransaction

# how often a sent transaction's receipt is looked for: an engineering bound, under a slot
RECEIPT_POLL_SECONDS = 2
# the slot time (Ethereum and its testnets): the next block is a slot after the latest
SLOT_SECONDS = 12


class ChainError(Exception):
    """The nodes failed, or a transaction did: unexpected, not a client's doing, so not a
    BrunoError."""


class Chain:
    def __init__(
        self,
        client: httpx.AsyncClient,
        chain_id: int,
        rpc_urls: tuple[str, ...],
        forks: tuple[tuple[int, GasSchedule], ...],
    ) -> None:
        """`forks`: the chain's gas schedules, each from its activation timestamp
        (bruno.forks.network_forks for a network)."""
        self._client = client
        self.chain_id = chain_id
        self.rpc_urls = rpc_urls
        self.forks = forks

    async def rpc(self, method: str, params: list[Any]) -> Any:
        """`method` at the first node that answers. A node's error answer (JsonRpcError, a
        revert included) is raised as it is: the next node would answer the same."""
        failures: list[str] = []
        for url in self.rpc_urls:
            try:
                return await call(self._client, url, method, params)
            except httpx.HTTPError as e:
                failures.append(f"{url}: {e}")
        raise ChainError(f"{method}: no node answered: {'; '.join(failures)}")

    async def check_chain(self) -> None:
        """Raise unless the nodes are the chain this was made for."""
        chain_id = int(await self.rpc("eth_chainId", []), 16)
        if chain_id != self.chain_id:
            raise ChainError(f"the nodes are chain {chain_id}, not {self.chain_id}")

    async def balance(self, account: str) -> int:
        return int(await self.rpc("eth_getBalance", [account, "latest"]), 16)

    async def nonce(self, account: str) -> int:
        return int(await self.rpc("eth_getTransactionCount", [account, "latest"]), 16)

    async def code(self, account: str) -> str:
        return await self.rpc("eth_getCode", [account, "latest"])

    async def head(self) -> int:
        return int(await self.rpc("eth_blockNumber", []), 16)

    async def schedule(self) -> GasSchedule:
        """The gas schedule of the next block: the one active a slot after the latest's."""
        block = await self.rpc("eth_getBlockByNumber", ["latest", False])
        return schedule_at(self.forks, int(block["timestamp"], 16) + SLOT_SECONDS)

    async def base_fee(self) -> int:
        block = await self.rpc("eth_getBlockByNumber", ["latest", False])
        return int(block["baseFeePerGas"], 16)

    async def next_base_fee(self) -> int:
        """The next block's base fee: eth_feeHistory's last, which follows from the latest
        block's header."""
        history = await self.rpc("eth_feeHistory", [hex(1), "latest", []])
        return int(history["baseFeePerGas"][-1], 16)

    async def call(self, to: str, data: bytes) -> bytes:
        """eth_call of `data` to `to`, at the latest block."""
        result: str = await self.rpc("eth_call", [{"to": to, "data": "0x" + data.hex()}, "latest"])
        return bytes.fromhex(result[2:])

    async def fees(self, priority_fee: int) -> Fees:
        """Fees that stay valid through several blocks of rising base fee: twice the base fee,
        and `priority_fee`."""
        base = await self.base_fee()
        return Fees(max_fee_per_gas=2 * base + priority_fee, max_priority_fee_per_gas=priority_fee)

    async def receipt(self, tx_hash: bytes) -> dict[str, Any] | None:
        return await self.rpc("eth_getTransactionReceipt", ["0x" + tx_hash.hex()])

    async def send(self, tx: SignedTransaction, what: str, timeout: float) -> dict[str, Any]:
        """Send `tx` to the public mempool and wait up to `timeout` seconds for its receipt,
        which must be a success."""
        await self.rpc("eth_sendRawTransaction", ["0x" + tx.raw.hex()])
        for _ in range(int(timeout / RECEIPT_POLL_SECONDS) + 1):
            receipt = await self.receipt(tx.hash)
            if receipt is not None:
                if receipt["status"] != "0x1":
                    raise ChainError(f"{what} reverted: 0x{tx.hash.hex()}")
                return receipt
            await asyncio.sleep(RECEIPT_POLL_SECONDS)
        raise ChainError(f"{what}: no receipt for 0x{tx.hash.hex()} in {timeout}s")
