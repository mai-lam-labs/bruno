"""Submissions paid by a signed transfer (EIP-3009) over REST and JSON-RPC, and accounts
deployed by the transaction that uses them (a submission's own deployment, or an ERC-6492
signature's): one price for the same call, payer and deployment on every interface; what's
refused, with nothing landing."""

from typing import Any

import httpx
import pytest
from eth_abi.abi import encode
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from bruno.erc6492 import MAGIC, Deployment
from bruno.tests import smartwallet
from bruno.tests.calibur import fresh_client
from bruno.tests.interface import challenged, quote, quoting, rpc
from bruno.tests.localchain import USDC, LocalChain
from bruno.tests.served import Served
from bruno.tests.transfers import (
    CALL_BYTES,
    CALL_GAS,
    Signs,
    as_given,
    balance_call,
    by_key,
    by_undeployed_wallet,
    deployment_json,
    transfer_json,
    usdc_transfer,
)

# a deadline this many blocks ahead
DEADLINE_BLOCKS = 10


def submitted(
    owner: LocalAccount,
    payee: ChecksumAddress,
    amount: int,
    transfer: dict[str, str],
    deadline: int,
) -> dict[str, object]:
    """A submission of a call that pays nothing itself, paid by `transfer`."""
    return {
        **balance_call(owner),
        "payment": {
            "token": USDC.address,
            "payee": payee,
            "amount": hex(amount),
            "transfer": transfer,
        },
        "maxBlockNumber": hex(deadline),
    }


async def paid_by_transfer(
    served: Served,
    caller: LocalAccount,
    payer: ChecksumAddress,
    sign: Signs,
    amount: int,
    deployment: dict[str, str] | None = None,
) -> dict[str, object]:
    """A submission of `caller`'s call, `payer` given `amount` USDC and paying it all by a
    transfer `sign` signs, naming `deployment` if given."""
    await served.chain.mint(USDC, payer, amount)
    transfer = await usdc_transfer(served.chain_id, payer, served.payee, amount, sign)
    deadline = await served.head() + DEADLINE_BLOCKS
    body = submitted(caller, served.payee, amount, transfer_json(transfer), deadline)
    if deployment is not None:
        body["deployment"] = deployment
    return body


async def submit(served: Served, http: httpx.AsyncClient, body: dict[str, object]) -> str:
    """`body` taken over REST: its status's location."""
    taken = await http.post(f"/{served.chain_id}/v1/submissions", json=body)
    assert taken.status_code == 202, taken.text
    return taken.headers["Location"]


async def landed(served: Served, http: httpx.AsyncClient, location: str) -> dict[str, Any]:
    await served.land()
    status: dict[str, Any] = (await http.get(location)).json()
    return status


@pytest.mark.parametrize("over", ["rest", "json-rpc"])
async def test_a_submission_paid_by_a_signed_transfer(
    chain: LocalChain, served: Served, over: str
) -> None:
    """A key holding only USDC pays for a call that pays nothing itself, by a signed transfer
    in the submission, at the price quoted for it as payer; it lands, and the payee gains the
    amount."""
    key = fresh_client(f"a signed transfer's payer, over {over}")
    async with served.interface() as http:
        amount = await quote(
            http, served.chain_id, gas=CALL_GAS, call_bytes=CALL_BYTES, payer=key.address
        )
        body = await paid_by_transfer(served, key, key.address, by_key(key), amount)
        before = await chain.balance_of(USDC, served.payee)
        if over == "rest":
            location = await submit(served, http, body)
        else:
            answer = await rpc(http, served.chain_id, "postpaid_sendTransaction", [body])
            location = f"/{served.chain_id}/v1/submissions/{answer['result']['id']}"
        status = await landed(served, http, location)

    assert status["status"] == "included", status
    assert await chain.balance_of(USDC, served.payee) - before == amount


async def test_one_price_on_every_interface(chain: LocalChain, served: Served) -> None:
    """The same call, payer and deployment cost the same over REST's quote, JSON-RPC's and
    x402's challenge, more than for a key; an undeployed wallet pays a submission exactly
    that by an ERC-6492 signature, and its transaction deploys it."""
    owner = fresh_client("one price's wallet owner")
    wallet = await smartwallet.address_of(chain, owner)
    deployment = deployment_json(smartwallet.deployment(wallet, owner))
    async with served.interface() as http:
        rest = await quote(
            http, served.chain_id, gas=CALL_GAS, call_bytes=CALL_BYTES, payer=wallet, **deployment
        )
        json_rpc = await rpc(
            http,
            served.chain_id,
            "postpaid_quote",
            [
                {
                    "gas": hex(CALL_GAS),
                    "callBytes": hex(CALL_BYTES),
                    "tokens": [USDC.address],
                    "payer": wallet,
                    "deployment": deployment,
                }
            ],
        )
        x402 = await challenged(
            http,
            served.chain_id,
            {**balance_call(owner), "payer": wallet, "deployment": deployment},
        )
        by_a_key = await quote(
            http,
            served.chain_id,
            gas=CALL_GAS,
            call_bytes=CALL_BYTES,
            payer=fresh_client("a key").address,
        )
        sign = by_undeployed_wallet(served.chain_id, wallet, owner)
        body = await paid_by_transfer(served, owner, wallet, sign, rest)
        status = await landed(served, http, await submit(served, http, body))

    assert int(json_rpc["result"]["quotes"][0]["amount"], 16) == rest
    assert int(x402["accepts"][0]["amount"]) == rest
    assert rest > by_a_key
    assert status["status"] == "included", status
    assert await chain.code(wallet) != "0x"


async def test_a_wallet_deployed_meanwhile_is_not_deployed_again(
    chain: LocalChain, served: Served
) -> None:
    """A submission whose ERC-6492 signature deploys its wallet, the wallet deployed by someone
    else before it lands: its runner skips the deployment, and it lands all the same."""
    owner = fresh_client("a wallet deployed meanwhile")
    wallet = await smartwallet.address_of(chain, owner)
    deployment = deployment_json(smartwallet.deployment(wallet, owner))
    async with served.interface() as http:
        amount = await quote(
            http, served.chain_id, gas=CALL_GAS, call_bytes=CALL_BYTES, payer=wallet, **deployment
        )
        sign = by_undeployed_wallet(served.chain_id, wallet, owner)
        location = await submit(
            served, http, await paid_by_transfer(served, owner, wallet, sign, amount)
        )
        await smartwallet.create(chain, owner)
        status = await landed(served, http, location)

    assert status["status"] == "included", status


async def test_a_deployment_for_the_call(chain: LocalChain, served: Served) -> None:
    """A submission names an account to deploy that isn't its payer (the account its call
    reads), paid by a key's signed transfer: its transaction deploys it first."""
    key = fresh_client("a deployment's payer")
    owner = fresh_client("a deployed account's owner")
    account = await smartwallet.address_of(chain, owner)
    deployment = deployment_json(smartwallet.deployment(account, owner))
    async with served.interface() as http:
        amount = await quote(
            http,
            served.chain_id,
            gas=CALL_GAS,
            call_bytes=CALL_BYTES,
            payer=key.address,
            **deployment,
        )
        body = await paid_by_transfer(served, key, key.address, by_key(key), amount, deployment)
        status = await landed(served, http, await submit(served, http, body))

    assert status["status"] == "included", status
    assert await chain.code(account) != "0x"


@pytest.mark.parametrize(
    ("case", "code"),
    [("factory fails", -32011), ("wrong owner", -32011), ("two deployments", -32602)],
)
async def test_refused(chain: LocalChain, served: Served, case: str, code: int) -> None:
    """Refused when taken, nothing deployed: a deployment whose factory call fails, and an
    undeployed wallet's signature by someone else (both simulation failed); a submission
    naming a deployment other than its signature's (invalid params)."""
    owner = fresh_client(f"a refused wallet's owner: {case}")
    wallet = await smartwallet.address_of(chain, owner)
    amount = 10**6
    if case == "factory fails":
        key = fresh_client("a failed deployment's payer")
        failing = deployment_json(Deployment(wallet, smartwallet.FACTORY, bytes(4)))
        body = await paid_by_transfer(served, key, key.address, by_key(key), amount, failing)
    else:
        signer = fresh_client("not the owner") if case == "wrong owner" else owner
        sign = by_undeployed_wallet(served.chain_id, wallet, signer)
        body = await paid_by_transfer(served, owner, wallet, sign, amount)
        if case == "two deployments":
            other = fresh_client("another account's owner")
            body["deployment"] = deployment_json(
                smartwallet.deployment(await smartwallet.address_of(chain, other), other)
            )
    async with served.interface() as http:
        answer = await http.post(f"/{served.chain_id}/v1/submissions", json=body)

    assert answer.status_code in (400, 422), answer.text
    assert answer.json()["error"]["code"] == code, answer.text
    assert await chain.code(wallet) == "0x"


async def test_a_verify_gas_over_the_most_gas_is_refused(served: Served) -> None:
    """A quote whose payer's signature check is said to take more gas than a call may have is
    invalid params, over REST and over JSON-RPC alike."""
    payer = fresh_client("a payer claiming a costly check").address
    async with served.interface() as http:
        most = int((await http.get(f"/{served.chain_id}/v1/config")).json()["maxGas"], 16)
        over = hex(most + 1)
        rest = await quoting(http, served.chain_id, payer=payer, verifyGas=over)
        json_rpc = await rpc(
            http,
            served.chain_id,
            "postpaid_quote",
            [
                {
                    "gas": hex(CALL_GAS),
                    "callBytes": hex(CALL_BYTES),
                    "tokens": [USDC.address],
                    "payer": payer,
                    "verifyGas": over,
                }
            ],
        )

    assert rest.status_code == 400, rest.text
    expected = {"code": -32602, "message": f"verifyGas: over {most}", "data": "verifyGas"}
    assert rest.json()["error"] == expected, rest.text
    assert json_rpc["error"] == expected, json_rpc


# A wallet that's deployed but can't check a signature until it's prepared (ERC-6492's second
# case), assembled by hand: isValidSignature(bytes32,bytes) answers ERC-1271's magic value only
# once storage slot 0 is set, whatever the signature; any other call sets it.
#   PUSH0 CALLDATALOAD PUSH1 0xe0 SHR PUSH4 0x1626ba7e EQ PUSH1 0x13 JUMPI
#   PUSH1 1 PUSH0 SSTORE STOP
#   0x13: JUMPDEST PUSH0 SLOAD PUSH4 0x1626ba7e PUSH1 0xe0 SHL MUL PUSH0 MSTORE
#   PUSH1 32 PUSH0 RETURN
PREPARED_WALLET = bytes.fromhex(
    "5f3560e01c631626ba7e1460135760015f55005b5f54631626ba7e60e01b025f5260205ff3"
)
# what the wallet takes as its preparation: any call but isValidSignature
PREPARE = bytes(4)
# its signature, whatever it is: once prepared, it takes any
PREPARED_SIGNATURE = bytes(range(70))


async def prepared_wallet(
    chain: LocalChain, served: Served, http: httpx.AsyncClient, wrapped: bool
) -> tuple[ChecksumAddress, int, httpx.Response]:
    """A wallet to prepare installed, holding USDC, and its submission sent: paid by its
    transfer, its signature ERC-6492-wrapped with its preparation or not. The wallet, the amount
    and the relay's answer."""
    wallet = to_checksum_address(keccak(text=f"a wallet to prepare, wrapped: {wrapped}")[-20:])
    await chain.rpc("anvil_setCode", [wallet, "0x" + PREPARED_WALLET.hex()])
    # its preparation isn't quoted: twice the price, the rest a tip
    amount = 2 * await quote(
        http, served.chain_id, gas=CALL_GAS, call_bytes=CALL_BYTES, payer=wallet
    )
    signature = PREPARED_SIGNATURE
    if wrapped:
        signature = encode(["address", "bytes", "bytes"], [wallet, PREPARE, signature]) + MAGIC
    caller = fresh_client("a prepared wallet's call")
    body = await paid_by_transfer(served, caller, wallet, as_given(signature), amount)
    return wallet, amount, await http.post(f"/{served.chain_id}/v1/submissions", json=body)


async def prepared(chain: LocalChain, wallet: ChecksumAddress) -> int:
    """The wallet's slot 0: 1 once it's prepared."""
    return int(await chain.rpc("eth_getStorageAt", [wallet, "0x0", "latest"]), 16)


async def test_a_wallet_prepared_by_its_signature(chain: LocalChain, served: Served) -> None:
    """A deployed wallet that checks no signature until a setup call: its ERC-6492 wrapper names
    the wallet itself and the setup, so the relay, its transfer failing without it, makes the
    call first (ERC-6492's prepare), and it lands, the wallet prepared and the payee paid."""
    before = await chain.balance_of(USDC, served.payee)
    async with served.interface() as http:
        wallet, amount, taken = await prepared_wallet(chain, served, http, wrapped=True)
        assert taken.status_code == 202, taken.text
        status = await landed(served, http, taken.headers["Location"])

    assert status["status"] == "included", status
    assert await prepared(chain, wallet) == 1
    assert await chain.balance_of(USDC, served.payee) - before == amount


async def test_a_wallet_unprepared_is_refused(chain: LocalChain, served: Served) -> None:
    """The same wallet's signature, not wrapped: no preparation to make, its transfer fails
    simulation, and nothing lands."""
    async with served.interface() as http:
        wallet, _, taken = await prepared_wallet(chain, served, http, wrapped=False)

    assert taken.status_code == 422, taken.text
    assert taken.json()["error"]["code"] == -32011
    assert await prepared(chain, wallet) == 0
