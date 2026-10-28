"""In-EVM gas, measured exactly on a chain, read-only: the figures bruno/forks.py's schedules
are checked against (make gas-probe).

A probe contract, installed by eth_call's state override, runs GAS, the code under test, GAS,
and returns the difference: each case's figure is its code's gas plus the second GAS's 2, the
baseline's (nothing) showing that. Nothing is sent: any node answers it, a live one included.

    python tools/gas_probe.py RPC_URL

A build tool, kept out of the relay's package.
"""

import asyncio
import sys

import httpx

from bruno.net.jsonrpc import JsonRpcError, call

PROBE = "0x" + "be" * 20
# given an EIP-7702 delegation by the override, to Calibur
DELEGATED = "0x" + "de" * 20
CALIBUR = "000000005c84f8fd50b21cac312528a64437030e"
# the deterministic deployment proxy: at the same address on every chain, so an existing account
# that starts cold (the block's fee recipient is warm, EIP-3651)
CREATE2_PROXY = "0x4e59b44847b379578588920cA78FbF26c0B4956C"
# addresses no one uses: a cold access to an empty account
FRESH = ["0x" + f"{0xF0000 + i:040x}" for i in range(10)]
PROBE_GAS = 5_000_000

GAS, SUB, SWAP1, POP, PUSH0 = "5a", "03", "90", "50", "5f"
# g1 - g2 to memory, RETURN its 32 bytes
_TAIL = SWAP1 + SUB + PUSH0 + "52" + "6020" + PUSH0 + "f3"


def push20(address: str) -> str:
    return "73" + address.removeprefix("0x").lower()


def probe(body: str) -> str:
    """GAS <body> GAS, returning g1 - g2: the body's gas, plus the second GAS's 2."""
    return "0x" + GAS + body + GAS + _TAIL


def cases() -> dict[str, str]:
    """Each case's code."""
    return {
        "baseline (nothing)": "",
        "PUSH0 POP": PUSH0 + POP,
        "PUSH1 POP": "6001" + POP,
        "PUSH20 POP": push20(FRESH[0]) + POP,
        "BALANCE cold (fresh address)": push20(FRESH[1]) + "31" + POP,
        "BALANCE cold, then warm": push20(FRESH[2]) + "31" + POP + push20(FRESH[2]) + "31" + POP,
        "BALANCE cold (existing account)": push20(CREATE2_PROXY) + "31" + POP,
        "STATICCALL gas 0, cold fresh": PUSH0 * 4 + push20(FRESH[3]) + PUSH0 + "fa" + POP,
        "CALL value 1 gas 0 to fresh (new account)": (
            PUSH0 * 4 + "6001" + push20(FRESH[4]) + PUSH0 + "f1" + POP
        ),
        "CALL value 1 gas 0 to existing (cold)": (
            PUSH0 * 4 + "6001" + push20(CREATE2_PROXY) + PUSH0 + "f1" + POP
        ),
        "CALL value 0 gas 0 to delegated EOA (cold)": (
            PUSH0 * 5 + push20(DELEGATED) + PUSH0 + "f1" + POP
        ),
        "SLOAD cold": PUSH0 + "54" + POP,
        "SLOAD cold, then warm": PUSH0 + "54" + POP + PUSH0 + "54" + POP,
        "MSTORE at 0 (1 word expansion)": PUSH0 + PUSH0 + "52",
        "MSTORE at 0, then at 32 (2nd word)": PUSH0 + PUSH0 + "52" + PUSH0 + "6020" + "52",
        "KECCAK256 of 32 bytes (memory expanded first)": (
            PUSH0 + PUSH0 + "52" + "6020" + PUSH0 + "20" + POP
        ),
        "CREATE2, empty initcode": PUSH0 * 4 + "f5" + POP,
        "CODECOPY 32 bytes to 0": "6020" + PUSH0 + PUSH0 + "39",
        "JUMPDEST": "5b",
        "SSTORE 0 -> 1 (cold slot 1)": "6001" + "6001" + "55",
    }


async def main(url: str) -> None:
    async with httpx.AsyncClient() as client:
        head = await call(client, url, "eth_getBlockByNumber", ["latest", False])
        chain_id = int(await call(client, url, "eth_chainId", []), 16)
        print(f"block {int(head['number'], 16)}, chain {chain_id}")
        for name, body in cases().items():
            overrides = {
                PROBE: {"balance": hex(10**18), "code": probe(body)},
                DELEGATED: {"code": "0xef0100" + CALIBUR},
            }
            tx = {"from": head["miner"], "to": PROBE, "gas": hex(PROBE_GAS)}
            try:
                out = await call(client, url, "eth_call", [tx, "latest", overrides])
            except JsonRpcError as e:
                print(f"   error  {name}: {e}")
                continue
            print(f"{int(out, 16):8d}  {name}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
