"""Transaction-level gas, measured exactly on a local anvil: the intrinsic figures in
bruno/forks.py's schedules are checked against (make gas-receipts).

Each case is sent from one of anvil's dev accounts with ample gas, and its receipt's gasUsed
read: the base, a recipient, value, a new account, calldata, a creation, and EIP-7702
authorizations (an empty authority, an existing one, and a re-delegation).

    python tools/tx_gas_receipts.py ANVIL_URL

A build tool, kept out of the relay's package. It changes the chain (anvil_setCode,
anvil_setBalance), so it's for a local anvil only.
"""

import asyncio
import sys
from dataclasses import dataclass

import httpx
from eth_account import Account
from eth_utils.crypto import keccak

from bruno.net.jsonrpc import call
from bruno.transactions import authorization_json

CALIBUR = "0x000000005c84F8Fd50b21CAC312528A64437030e"
SENT_GAS = 3_000_000
RECEIPT_POLL_SECONDS = 0.1


@dataclass(frozen=True, slots=True)
class Anvil:
    client: httpx.AsyncClient
    url: str

    async def rpc(self, method: str, params: list[object]) -> object:
        return await call(self.client, self.url, method, params)

    async def used(self, tx: dict[str, object]) -> tuple[int, str]:
        """A transaction's gasUsed and status, sent with ample gas."""
        sent = await call(
            self.client, self.url, "eth_sendTransaction", [{**tx, "gas": hex(SENT_GAS)}]
        )
        while (
            receipt := await call(self.client, self.url, "eth_getTransactionReceipt", [sent])
        ) is None:
            await asyncio.sleep(RECEIPT_POLL_SECONDS)
        return int(receipt["gasUsed"], 16), receipt["status"]


def fresh(label: str) -> str:
    """An address no one uses."""
    return "0x" + keccak(text=label)[-20:].hex()


async def main(url: str) -> None:
    async with httpx.AsyncClient() as client:
        anvil = Anvil(client, url)
        sender, other = (await call(client, url, "eth_accounts", []))[:2]
        chain_id = int(await call(client, url, "eth_chainId", []), 16)
        # any code: a delegate target
        await anvil.rpc("anvil_setCode", [CALIBUR, "0x00"])
        cases: dict[str, dict[str, object]] = {
            "self-transfer, no value": {"from": sender, "to": sender},
            "transfer 0 to existing": {"from": sender, "to": other},
            "transfer 1 wei to existing": {"from": sender, "to": other, "value": "0x1"},
            "transfer 1 wei to fresh": {"from": sender, "to": fresh("f1"), "value": "0x1"},
            "100 zero bytes to existing": {"from": sender, "to": other, "data": "0x" + "00" * 100},
            "100 nonzero bytes to existing": {
                "from": sender,
                "to": other,
                "data": "0x" + "11" * 100,
            },
            "creation, empty initcode": {"from": sender, "data": "0x"},
            "creation, 64 bytes returning nothing": {
                "from": sender,
                "data": "0x5f5ff3" + "00" * 61,
            },
        }
        for name, tx in cases.items():
            gas, _ = await anvil.used(tx)
            print(f"{gas:>8}  {name}")
        for label, key, funded in (
            ("empty authority, new delegation", b"\x11" * 32, False),
            ("existing authority (funded), new delegation", b"\x12" * 32, True),
        ):
            authority = Account.from_key(key)
            if funded:
                await anvil.rpc("anvil_setBalance", [authority.address, hex(10**18)])
            for nonce in (0, 1) if funded else (0,):
                signed = Account.sign_authorization(
                    {"chainId": chain_id, "address": CALIBUR, "nonce": nonce}, authority.key
                )
                tx = {
                    "from": sender,
                    "to": other,
                    "authorizationList": [authorization_json(signed)],
                }
                gas, status = await anvil.used(tx)
                what = label if nonce == 0 else "re-delegation, same delegate"
                print(f"{gas:>8}  type-4 to existing, 1 authorization: {what} (status {status})")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
