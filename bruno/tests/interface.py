"""The relay's interface as the tests' clients use it: a quote over REST, a call over JSON-RPC,
x402's challenge and a payment's held request, and a client's call signed through Calibur and
submitted."""

import asyncio
from typing import Any

import httpx
from eth_account import Account
from eth_account.signers.local import LocalAccount

from bruno.tests.calibur import calibur_batch, fresh_client
from bruno.tests.deployments import CLIENT_GAS, transfer
from bruno.tests.localchain import CALIBUR, USDC, LocalChain
from bruno.tests.served import MAX_DEADLINE_BLOCKS, X402_DEADLINE_BLOCKS, Served
from bruno.tests.transfers import decoded
from bruno.transactions import authorization_json

# the client's call's size, as a quote is asked for it: room for a signed batch
CLIENT_CALL_BYTES = 2_000


def origin(served: Served) -> str:
    """Where the relay's interface is: its URLs in what it answers begin so."""
    return str(served.http.base_url).rstrip("/")


async def rpc(
    http: httpx.AsyncClient, chain_id: int, method: str, params: list[object]
) -> dict[str, Any]:
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    response = await http.post(f"/{chain_id}", json=body)
    assert response.status_code == 200
    return response.json()


async def quoting(
    http: httpx.AsyncClient,
    chain: int | str,
    token: str = USDC.address,
    *,
    gas: int = CLIENT_GAS,
    call_bytes: int = CLIENT_CALL_BYTES,
    **extra: str,
) -> httpx.Response:
    """The quote over REST, at `chain` (its id or its name), in `token`, of a call of `gas` and
    `call_bytes`, with `extra` query parameters: the response, whatever it is."""
    params = {"gas": hex(gas), "callBytes": hex(call_bytes), "tokens": [token], **extra}
    return await http.get(f"/{chain}/v1/quote", params=params)


async def quote(
    http: httpx.AsyncClient,
    chain_id: int,
    token: str = USDC.address,
    *,
    gas: int = CLIENT_GAS,
    call_bytes: int = CLIENT_CALL_BYTES,
    **extra: str,
) -> int:
    """The amount quoted over REST in `token` for a call of `gas` and `call_bytes` (the
    client's call, unless given), with `extra` query parameters."""
    response = await quoting(http, chain_id, token, gas=gas, call_bytes=call_bytes, **extra)
    assert response.status_code == 200, response.text
    return int(response.json()["quotes"][0]["amount"], 16)


async def challenged(http: httpx.AsyncClient, chain_id: int, body: object) -> dict[str, Any]:
    """The 402's PAYMENT-REQUIRED for `body`, checked to be the response's body too."""
    response = await http.post(f"/{chain_id}/v1/x402", json=body)
    assert response.status_code == 402, response.text
    required = decoded(response.headers["PAYMENT-REQUIRED"])
    assert required == response.json()
    return required


async def landing(served: Served, sent: asyncio.Task[httpx.Response]) -> httpx.Response:
    """`sent`'s answer, blocks landing while its request is held: a batch closed and a block
    built each half second, an x402 deadline's worth and a few more."""
    for _ in range(X402_DEADLINE_BLOCKS + 5):
        done, _ = await asyncio.wait({sent}, timeout=0.5)
        if done:
            break
        await served.land()
    return await sent


async def signed_call(
    chain: LocalChain, client: LocalAccount, payee: str, paid: int
) -> tuple[dict[str, str], dict[str, str]]:
    """The client's call, paying `paid` USDC to `payee`, and its authorization to Calibur, as
    JSON."""
    chain_id = await chain.chain_id()
    authorization = Account.sign_authorization(
        {"chainId": chain_id, "address": CALIBUR, "nonce": 0}, client.key
    )
    call = calibur_batch(client, chain_id, [transfer(USDC, payee, paid)])
    return {"to": call.to, "data": call.data}, authorization_json(authorization)


def submission(
    call: dict[str, str], auth: dict[str, str], payee: str, amount: int, deadline: int
) -> dict[str, object]:
    return {
        "call": call,
        "authorizationList": [auth],
        "gas": hex(CLIENT_GAS),
        "payment": {"token": USDC.address, "payee": payee, "amount": hex(amount)},
        "maxBlockNumber": hex(deadline),
    }


async def submit_paying(
    http: httpx.AsyncClient, chain: LocalChain, served: Served, name: str, amount: int
) -> httpx.Response:
    """A fresh client, holding `amount` USDC, submitting a call that pays it all."""
    client = fresh_client(name)
    await chain.mint(USDC, client.address, amount)
    call, auth = await signed_call(chain, client, served.payee, amount)
    deadline = await served.head() + MAX_DEADLINE_BLOCKS
    body = submission(call, auth, served.payee, amount, deadline)
    return await http.post(f"/{served.chain_id}/v1/submissions", json=body)
