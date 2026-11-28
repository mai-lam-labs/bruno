"""Submissions paid by a signed transfer (EIP-3009) over REST and JSON-RPC, and accounts
deployed by the transaction that uses them (a submission's own deployment, or an ERC-6492
signature's): one price for the same call, payer and deployment on every interface; what's
refused, with nothing landing."""

from typing import Any

import httpx
import pytest
from eth_abi.abi import encode
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_typing import ChecksumAddress
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from bruno.abi import selector
from bruno.deployer import DEPLOYER
from bruno.erc6492 import MAGIC, Deployment
from bruno.forwarder import forwarding
from bruno.tests import smartwallet
from bruno.tests.calibur import fresh_client
from bruno.tests.interface import challenged, quote, quoting, rpc
from bruno.tests.localchain import REVERT_ALWAYS, SIMPLE_7702_ACCOUNT, USDC, LocalChain
from bruno.tests.served import Served
from bruno.tests.transfers import (
    CALL_BYTES,
    CALL_GAS,
    MULTICALL3,
    Signs,
    as_given,
    balance_call,
    balance_read,
    by_key,
    by_undeployed_wallet,
    deployment_json,
    listed_read,
    one_call,
    transfer_json,
    usdc_transfer,
)
from bruno.transactions import authorization_json

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
            http, served.chain_id, gas=CALL_GAS, call_data_length=CALL_BYTES, payer=key.address
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
    (only,) = status["transactionHashes"]
    sent = await chain.rpc("eth_getTransactionByHash", [only])
    assert to_checksum_address(sent["to"]) == DEPLOYER  # its call isn't to a listed target


@pytest.mark.parametrize("over", ["rest", "json-rpc"])
async def test_a_listed_call_paid_by_a_transfer_is_sent_directly(
    chain: LocalChain, served: Served, over: str
) -> None:
    """A call to a listed target, paid by a key's signed transfer: the relay's account makes
    the call itself, then the transfer, each a transaction of its own and no runner, both in
    one block. It's quoted less than the same call to a target that isn't listed, which needs a
    runner. Its status's transaction is its last, the transfer."""
    key = fresh_client(f"pays for a listed call, over {over}")
    to_multicall = listed_read(key.address)
    async with served.interface() as http:
        amount = await quote(
            http,
            served.chain_id,
            gas=CALL_GAS,
            call_data_length=None,
            call=to_multicall,
            payer=key.address,
        )
        through_a_runner = await quote(
            http,
            served.chain_id,
            gas=CALL_GAS,
            call_data_length=None,
            call=balance_read(key),
            payer=key.address,
        )
        body = await paid_by_transfer(served, key, key.address, by_key(key), amount)
        body |= one_call(to_multicall)
        before = await chain.balance_of(USDC, served.payee)
        if over == "rest":
            location = await submit(served, http, body)
        else:
            answer = await rpc(http, served.chain_id, "postpaid_sendTransaction", [body])
            location = f"/{served.chain_id}/v1/submissions/{answer['result']['id']}"
        status = await landed(served, http, location)

    assert status["status"] == "included", status
    block = await chain.rpc("eth_getBlockByNumber", [status["blockNumber"], True])
    call, transfer = block["transactions"]
    assert to_checksum_address(call["to"]) == MULTICALL3
    assert to_checksum_address(transfer["to"]) == USDC.address
    assert call["from"] == transfer["from"]
    # each of its transactions, in order: the call's receipt holds the call's logs
    assert status["transactionHashes"] == [call["hash"], transfer["hash"]]
    assert await chain.balance_of(USDC, served.payee) - before == amount
    assert amount < through_a_runner


async def test_a_listed_call_can_earn_what_pays_for_it(chain: LocalChain, served: Served) -> None:
    """A payer holding no USDC, whose call is a transfer to it that another account signed (a
    listed function of USDC's), and whose own transfer pays the relay out of that. The call is
    made first, so both land: the payee gains the amount, and the payer keeps the rest."""
    funder = fresh_client("funds a payer that holds nothing")
    payer = fresh_client("a payer that holds nothing")
    funded = 10_000_000
    await chain.mint(USDC, funder.address, funded)
    earning = await usdc_transfer(
        served.chain_id, funder.address, payer.address, funded, by_key(funder)
    )
    call = {"to": USDC.address, "data": "0x" + earning.calldata().hex()}
    async with served.interface() as http:
        quoted = await quoting(
            http,
            served.chain_id,
            USDC.address,
            gas=None,
            call_data_length=None,
            call=call,
            payer=payer.address,
        )
        assert quoted.status_code == 200, quoted.text
        amount = int(quoted.json()["quotes"][0]["amount"], 16)
        paying = await usdc_transfer(
            served.chain_id, payer.address, served.payee, amount, by_key(payer)
        )
        deadline = await served.head() + DEADLINE_BLOCKS
        body = submitted(payer, served.payee, amount, transfer_json(paying), deadline)
        body |= one_call(call, quoted.json()["calls"][0]["gas"])
        before = await chain.balance_of(USDC, served.payee)
        status = await landed(served, http, await submit(served, http, body))

    assert status["status"] == "included", status
    assert await chain.balance_of(USDC, served.payee) - before == amount
    assert await chain.balance_of(USDC, payer.address) == funded - amount


async def test_a_keys_transfer_is_given_its_gas_by_the_relay(
    chain: LocalChain, served: Served
) -> None:
    """A call that's a key's signed USDC transfer states no gas: the relay bounds it itself,
    from the token's measurement. Its quote is answered without running it (its signer holds
    nothing yet, so an estimate of it would revert), and its submission, still stating no gas,
    lands once the signer is funded, in one transaction with the transfer that pays for it,
    Multicall3's. The same call from a signer with code isn't bounded, the
    token checking that signature by the account's own code: stating no gas, it's refused for
    it."""
    funder = fresh_client("signs a transfer the relay bounds")
    payer = fresh_client("a payer brought what it pays by a bounded call")
    funded = 10_000_000
    earning = await usdc_transfer(
        served.chain_id, funder.address, payer.address, funded, by_key(funder)
    )
    call = {"to": USDC.address, "data": "0x" + earning.calldata().hex()}
    async with served.interface() as http:
        quoted = await quoting(
            http,
            served.chain_id,
            USDC.address,
            gas=None,
            call_data_length=None,
            call=call,
            payer=payer.address,
        )
        assert quoted.status_code == 200, quoted.text
        amount = int(quoted.json()["quotes"][0]["amount"], 16)
        await chain.mint(USDC, funder.address, funded)
        paying = await usdc_transfer(
            served.chain_id, payer.address, served.payee, amount, by_key(payer)
        )
        deadline = await served.head() + DEADLINE_BLOCKS
        body = submitted(payer, served.payee, amount, transfer_json(paying), deadline)
        body["calls"] = [call]
        status = await landed(served, http, await submit(served, http, body))
        # the signer given code: the relay bounds another transfer of its no longer
        await chain.rpc("anvil_setCode", [funder.address, REVERT_ALWAYS])
        again = await usdc_transfer(
            served.chain_id, funder.address, payer.address, funded, by_key(funder)
        )
        repaying = await usdc_transfer(
            served.chain_id, payer.address, served.payee, amount, by_key(payer)
        )
        second = submitted(payer, served.payee, amount, transfer_json(repaying), deadline)
        second["calls"] = [{"to": USDC.address, "data": "0x" + again.calldata().hex()}]
        unbounded = await http.post(f"/{served.chain_id}/v1/submissions", json=second)

    assert status["status"] == "included", status
    (only,) = status["transactionHashes"]
    sent = await chain.rpc("eth_getTransactionByHash", [only])
    assert to_checksum_address(sent["to"]) == MULTICALL3
    assert await chain.balance_of(USDC, payer.address) == funded - amount
    assert unbounded.status_code == 400, unbounded.text
    assert unbounded.json()["error"]["data"] == {"reason": "call-no-gas", "field": "calls[0].gas"}


@pytest.mark.parametrize("kind", ["listed", "runner"])
async def test_a_payer_its_submission_delegates_pays_by_its_keys_signature(
    chain: LocalChain, served: Served, kind: str
) -> None:
    """A key the same submission delegates (EIP-7702, to a delegate that takes the key's own
    signature as the account's) pays by a transfer signed as a key signs one. Once delegated,
    the token checks that signature by the account's new code, not by recovering the key, so
    the relay prices and plans the transfer as a contract's. With a call to a listed target,
    the call's transaction carries the authorization and the transfer's follows it; with any
    other, one runner makes both. Either way it lands at the price quoted, the payee gains
    the amount, and the account is delegated."""
    payer = fresh_client(f"a payer its own submission delegates: {kind}")
    await chain.mint(USDC, payer.address, 10**9)
    delegation = Account.sign_authorization(
        {"chainId": served.chain_id, "address": SIMPLE_7702_ACCOUNT, "nonce": 0}, payer.key
    )
    authorizations = [authorization_json(delegation)]
    call = listed_read(payer.address) if kind == "listed" else balance_read(payer)
    async with served.interface() as http:
        quoted = await quoting(
            http,
            served.chain_id,
            USDC.address,
            gas=CALL_GAS,
            call_data_length=None,
            call=call,
            payer=payer.address,
            authorizationList=authorizations,
        )
        assert quoted.status_code == 200, quoted.text
        amount = int(quoted.json()["quotes"][0]["amount"], 16)
        paying = await usdc_transfer(
            served.chain_id, payer.address, served.payee, amount, by_key(payer)
        )
        deadline = await served.head() + DEADLINE_BLOCKS
        body = submitted(payer, served.payee, amount, transfer_json(paying), deadline)
        body |= one_call(call)
        body["authorizationList"] = authorizations
        before = await chain.balance_of(USDC, served.payee)
        status = await landed(served, http, await submit(served, http, body))

    assert status["status"] == "included", status
    assert await chain.balance_of(USDC, served.payee) - before == amount
    assert await chain.code(payer.address) == "0xef0100" + SIMPLE_7702_ACCOUNT[2:].lower()
    sent = [
        await chain.rpc("eth_getTransactionByHash", [each]) for each in status["transactionHashes"]
    ]
    targets = [to_checksum_address(each["to"]) for each in sent]
    assert targets == ([MULTICALL3, USDC.address] if kind == "listed" else [DEPLOYER])


async def test_a_call_whose_signer_its_submission_delegates_is_estimated_and_lands(
    chain: LocalChain, served: Served
) -> None:
    """A call that is a transfer signed by a key the same submission delegates. The relay
    doesn't bound it, the token checking that signature by the account's new code: stating no
    gas, it's estimated at its quote with the authorization applied, and at that gas it lands,
    bringing the payer what it pays with by a transfer of its own."""
    funder = fresh_client("signs a transfer as a key, and is delegated with it")
    payer = fresh_client("a payer brought what it pays by a delegated signer's transfer")
    funded = 100_000_000
    await chain.mint(USDC, funder.address, funded)
    delegation = Account.sign_authorization(
        {"chainId": served.chain_id, "address": SIMPLE_7702_ACCOUNT, "nonce": 0}, funder.key
    )
    authorizations = [authorization_json(delegation)]
    earning = await usdc_transfer(
        served.chain_id, funder.address, payer.address, funded, by_key(funder)
    )
    call = {"to": USDC.address, "data": "0x" + earning.calldata().hex()}
    async with served.interface() as http:
        quoted = await quoting(
            http,
            served.chain_id,
            USDC.address,
            gas=None,
            call_data_length=None,
            call=call,
            payer=payer.address,
            authorizationList=authorizations,
        )
        assert quoted.status_code == 200, quoted.text
        amount = int(quoted.json()["quotes"][0]["amount"], 16)
        paying = await usdc_transfer(
            served.chain_id, payer.address, served.payee, amount, by_key(payer)
        )
        deadline = await served.head() + DEADLINE_BLOCKS
        body = submitted(payer, served.payee, amount, transfer_json(paying), deadline)
        body |= one_call(call, quoted.json()["calls"][0]["gas"])
        body["authorizationList"] = authorizations
        before = await chain.balance_of(USDC, served.payee)
        status = await landed(served, http, await submit(served, http, body))

    assert status["status"] == "included", status
    assert await chain.balance_of(USDC, served.payee) - before == amount
    assert await chain.balance_of(USDC, payer.address) == funded - amount
    assert await chain.code(funder.address) == "0xef0100" + SIMPLE_7702_ACCOUNT[2:].lower()


async def test_several_calls_are_quoted_and_sent_over_rest(
    chain: LocalChain, served: Served
) -> None:
    """A quote and a submission of two calls over REST, as over x402. The quote answers each
    call's gas: a transfer the relay bounds its bound, and a read that states none its
    estimate. The submission gives each call that gas and lands whole, its calls and its
    payment's transfer a bundle, the first call bringing the payer what it pays with."""
    funder = fresh_client("signs a transfer, the first of several calls")
    payer = fresh_client("a payer brought what it pays by the first of several calls")
    funded = 10_000_000
    earning = await usdc_transfer(
        served.chain_id, funder.address, payer.address, funded, by_key(funder)
    )
    calls = [
        {"to": USDC.address, "data": "0x" + earning.calldata().hex()},
        listed_read(payer.address),
    ]
    async with served.interface() as http:
        asked = {"tokens": [USDC.address], "calls": calls, "payer": payer.address}
        quoted = await http.post(f"/{served.chain_id}/v1/quote", json=asked)
        assert quoted.status_code == 200, quoted.text
        amount = int(quoted.json()["quotes"][0]["amount"], 16)
        gas = [each["gas"] for each in quoted.json()["calls"]]
        await chain.mint(USDC, funder.address, funded)
        paying = await usdc_transfer(
            served.chain_id, payer.address, served.payee, amount, by_key(payer)
        )
        deadline = await served.head() + DEADLINE_BLOCKS
        body = submitted(payer, served.payee, amount, transfer_json(paying), deadline)
        body["calls"] = [{**call, "gas": given} for call, given in zip(calls, gas, strict=True)]
        status = await landed(served, http, await submit(served, http, body))

    assert status["status"] == "included", status
    assert len(status["transactionHashes"]) == 3
    assert await chain.balance_of(USDC, payer.address) == funded - amount


async def test_a_call_through_a_runner_can_earn_what_pays_for_it(
    chain: LocalChain, served: Served
) -> None:
    """A payer holding no USDC, whose call isn't to a listed target, so it's made by a runner:
    it has a forwarder of the payer's own send the payer the USDC it holds. The payer's signed
    transfer pays the relay out of that. The runner makes the call first and a key's transfer
    after it, so it lands as one transaction: the payee gains the amount, and the payer keeps
    the rest."""
    payer = fresh_client("a payer that holds nothing, its call through a runner")
    funded = 10_000_000
    its = await chain.deploy_forwarder(payer.address)
    await chain.mint(USDC, its.address, funded)
    earning = forwarding(its.address, USDC.address, funded)
    call = {"to": earning.to, "data": "0x" + earning.data.hex()}
    async with served.interface() as http:
        quoted = await quoting(
            http,
            served.chain_id,
            USDC.address,
            gas=None,
            call_data_length=None,
            call=call,
            payer=payer.address,
        )
        assert quoted.status_code == 200, quoted.text
        amount = int(quoted.json()["quotes"][0]["amount"], 16)
        paying = await usdc_transfer(
            served.chain_id, payer.address, served.payee, amount, by_key(payer)
        )
        deadline = await served.head() + DEADLINE_BLOCKS
        body = submitted(payer, served.payee, amount, transfer_json(paying), deadline)
        body |= one_call(call, quoted.json()["calls"][0]["gas"])
        before = await chain.balance_of(USDC, served.payee)
        status = await landed(served, http, await submit(served, http, body))

    assert status["status"] == "included", status
    assert len(status["transactionHashes"]) == 1, status
    sent = await chain.rpc("eth_getTransactionByHash", [status["transactionHashes"][0]])
    assert to_checksum_address(sent["to"]) == DEPLOYER
    assert await chain.balance_of(USDC, served.payee) - before == amount
    # the forwarder keeps one unit of what it held
    assert await chain.balance_of(USDC, payer.address) == funded - 1 - amount


@pytest.mark.parametrize("listed", [False, True], ids=["through a runner", "directly"])
@pytest.mark.parametrize("failing", ["transfer", "call"])
async def test_a_transfer_or_call_failing_in_its_block_lands_nothing(
    chain: LocalChain, served: Served, failing: str, listed: bool
) -> None:
    """A submission paid by a key's transfer, taken and sent to the builder, then its transfer
    failing (its payer's USDC gone) or its call (its target reverting by then) when the block
    is built: through a runner, its transaction reverts and the builder drops it; sent directly,
    its call and its transfer are one bundle, which the builder leaves out whole. Neither is
    made: the payer keeps its USDC, the payee gains nothing, and no call is in the block."""
    key = fresh_client(f"a payer whose {failing} fails in its block, {listed}")
    target = fresh_client(f"what a call that fails in its block calls: {failing}").address
    made: dict[str, str] = {"to": target, "data": "0x"}
    if listed:  # Multicall3 making the same call, which fails it when the call does
        calls = encode(["(address,bool,bytes)[]"], [[(target, False, b"")]])
        aggregate3 = selector("aggregate3((address,bool,bytes)[])") + calls
        made = {"to": MULTICALL3, "data": "0x" + aggregate3.hex()}
    async with served.interface() as http:
        amount = await quote(
            http, served.chain_id, gas=CALL_GAS, call_data_length=CALL_BYTES, payer=key.address
        )
        body = await paid_by_transfer(served, key, key.address, by_key(key), amount)
        body |= one_call(made)
        before = await chain.balance_of(USDC, served.payee)
        location = await submit(served, http, body)
        await served.send()
        if failing == "transfer":
            await chain.set_token_balance(USDC, key.address, amount - 1)
        else:
            await chain.rpc("anvil_setCode", [target, REVERT_ALWAYS])
        await served.builder.build_block()
        await served.on_block(await served.head())
        status = (await http.get(location)).json()

    block = await chain.rpc("eth_getBlockByNumber", ["latest", True])
    assert status["status"] != "included", status
    assert [t for t in block["transactions"] if t["to"] is not None] == []
    assert await chain.balance_of(USDC, served.payee) == before
    assert await chain.balance_of(USDC, key.address) >= amount - 1


async def test_one_price_on_every_interface(chain: LocalChain, served: Served) -> None:
    """The same call, payer and deployment cost the same over REST's quote, JSON-RPC's and
    x402's challenge, more than for a key; an undeployed wallet pays a submission exactly
    that by an ERC-6492 signature, and its transaction deploys it."""
    owner = fresh_client("one price's wallet owner")
    wallet = await smartwallet.address_of(chain, owner)
    deployment = deployment_json(smartwallet.deployment(wallet, owner))
    paid_call = balance_call(owner)
    its_call = balance_read(owner)
    async with served.interface() as http:
        rest = await quote(
            http,
            served.chain_id,
            gas=CALL_GAS,
            call_data_length=None,
            call=its_call,
            payer=wallet,
            deployment=deployment,
        )
        json_rpc = await rpc(
            http,
            served.chain_id,
            "postpaid_quote",
            [
                {
                    **paid_call,
                    "tokens": [USDC.address],
                    "payer": wallet,
                    "deployment": deployment,
                }
            ],
        )
        x402 = await challenged(
            http, served.chain_id, {**paid_call, "payer": wallet, "deployment": deployment}
        )
        by_a_key = await quote(
            http,
            served.chain_id,
            gas=CALL_GAS,
            call_data_length=None,
            call=its_call,
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
            http,
            served.chain_id,
            gas=CALL_GAS,
            call_data_length=CALL_BYTES,
            payer=wallet,
            deployment=deployment,
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
            call_data_length=CALL_BYTES,
            payer=key.address,
            deployment=deployment,
        )
        body = await paid_by_transfer(served, key, key.address, by_key(key), amount, deployment)
        status = await landed(served, http, await submit(served, http, body))

    assert status["status"] == "included", status
    assert await chain.code(account) != "0x"


# ERC-6492 wrappers that aren't: what's before the magic bytes doesn't decode, or its data's
# offset points into the wrapper's own head
_WRAPPER = encode(["address", "bytes", "bytes"], [smartwallet.FACTORY, bytes(4), bytes(65)])
MALFORMED_WRAPPERS = {
    "a wrapper that doesn't decode": bytes(10) + MAGIC,
    "an offset into the wrapper's head": _WRAPPER[:32] + (32).to_bytes(32) + _WRAPPER[64:] + MAGIC,
}


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("factory fails", -32011),
        ("wrong owner", -32011),
        ("two deployments", -32602),
        *((case, -32602) for case in MALFORMED_WRAPPERS),
    ],
)
async def test_refused(chain: LocalChain, served: Served, case: str, code: int) -> None:
    """Refused when taken, nothing deployed: a deployment whose factory call fails, and an
    undeployed wallet's signature by someone else (both simulation failed); a submission
    naming a deployment other than its signature's, and a signature with ERC-6492's magic
    bytes whose wrapper isn't one (invalid params)."""
    owner = fresh_client(f"a refused wallet's owner: {case}")
    wallet = await smartwallet.address_of(chain, owner)
    amount = 10**6
    if case == "factory fails":
        key = fresh_client("a failed deployment's payer")
        failing = deployment_json(Deployment(wallet, smartwallet.FACTORY, bytes(4)))
        body = await paid_by_transfer(served, key, key.address, by_key(key), amount, failing)
    elif case in MALFORMED_WRAPPERS:
        sign = as_given(MALFORMED_WRAPPERS[case])
        body = await paid_by_transfer(served, owner, wallet, sign, amount)
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


async def quoted_both_ways(
    http: httpx.AsyncClient,
    chain_id: int,
    deployment: dict[str, str] | None = None,
    **params: str,
) -> tuple[httpx.Response, dict[str, Any]]:
    """A quote of the usual call with `params` and `deployment` added, over REST and over
    JSON-RPC, the same object."""
    declared = {} if deployment is None else {"deployment": deployment}
    rest = await quoting(
        http, chain_id, gas=CALL_GAS, call_data_length=CALL_BYTES, deployment=deployment, **params
    )
    json_rpc = await rpc(
        http,
        chain_id,
        "postpaid_quote",
        [
            {
                "calls": [{"gas": hex(CALL_GAS), "callDataLength": hex(CALL_BYTES)}],
                "tokens": [USDC.address],
                **params,
                **declared,
            }
        ],
    )
    return rest, json_rpc


async def test_a_verify_gas_over_the_most_gas_is_refused(served: Served) -> None:
    """A quote whose payer's signature check is said to take more gas than a call may have is
    invalid params, over REST and over JSON-RPC alike."""
    payer = fresh_client("a payer claiming a costly check").address
    async with served.interface() as http:
        most = int((await http.get(f"/{served.chain_id}/v1/config")).json()["maxGas"], 16)
        rest, json_rpc = await quoted_both_ways(
            http, served.chain_id, payer=payer, verifyGas=hex(most + 1)
        )

    assert rest.status_code == 400, rest.text
    expected = {
        "code": -32602,
        "data": {"reason": "gas-over", "field": "verifyGas", "most": hex(most)},
    }
    assert {k: rest.json()["error"][k] for k in expected} == expected, rest.text
    assert {k: json_rpc["error"][k] for k in expected} == expected, json_rpc


async def test_a_verify_gas_without_a_payer_is_refused(chain: LocalChain, served: Served) -> None:
    """A quote with a signature check's gas but no payer, so no transfer to check (the call
    pays, its deployment made first): invalid params, over REST and over JSON-RPC alike, as
    the gas would otherwise be dropped."""
    owner = fresh_client("a deployment with a check but no payer")
    account = await smartwallet.address_of(chain, owner)
    deployment = deployment_json(smartwallet.deployment(account, owner))
    async with served.interface() as http:
        rest, json_rpc = await quoted_both_ways(
            http, served.chain_id, deployment, verifyGas=hex(30_000)
        )

    assert rest.status_code == 400, rest.text
    expected = {"code": -32602, "data": {"reason": "verify-gas-no-payer", "field": "verifyGas"}}
    assert {k: rest.json()["error"][k] for k in expected} == expected, rest.text
    assert {k: json_rpc["error"][k] for k in expected} == expected, json_rpc


async def test_a_quote_whose_factory_call_fails_is_refused(
    chain: LocalChain, served: Served
) -> None:
    """A quote whose deployment's factory call reverts: invalid params, the node's own message
    given, over REST and over JSON-RPC alike."""
    payer = fresh_client("a payer with a failing deployment").address
    account = await smartwallet.address_of(chain, fresh_client("a failing deployment's owner"))
    failing = deployment_json(Deployment(account, smartwallet.FACTORY, bytes(4)))
    async with served.interface() as http:
        rest, json_rpc = await quoted_both_ways(http, served.chain_id, failing, payer=payer)

    assert rest.status_code == 400, rest.text
    error = rest.json()["error"]
    assert error["code"] == -32602, error
    assert error["data"] == {"reason": "factory-fails", "field": "deployment"}, error
    assert json_rpc["error"] == error, json_rpc


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
# what its check adds to a transfer, over a key's, at most: the wallet cold, a cold SLOAD
PREPARED_CHECK_GAS = 8_000


async def prepared_wallet(
    chain: LocalChain, served: Served, http: httpx.AsyncClient, wrapped: bool
) -> tuple[ChecksumAddress, int, httpx.Response]:
    """A wallet to prepare installed, holding USDC, and its submission sent: paid by its
    transfer of exactly the quote, its signature ERC-6492-wrapped with its preparation (and
    quoted with it declared) or not. The wallet, the amount and the relay's answer."""
    wallet = to_checksum_address(keccak(text=f"a wallet to prepare, wrapped: {wrapped}")[-20:])
    await chain.rpc("anvil_setCode", [wallet, "0x" + PREPARED_WALLET.hex()])
    # quoted as it's paid: its own check's gas, and the preparation its wrapper makes declared
    declared = deployment_json(Deployment(wallet, wallet, PREPARE)) if wrapped else None
    amount = await quote(
        http,
        served.chain_id,
        gas=CALL_GAS,
        call_data_length=CALL_BYTES,
        payer=wallet,
        verifyGas=hex(PREPARED_CHECK_GAS),
        deployment=declared,
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


async def test_a_preparation_declared_is_priced(chain: LocalChain, served: Served) -> None:
    """A deployed wallet's quote declaring the preparation its wrapper will make costs more
    than one without: the relay may make that call, so it's priced (the fee's headroom alone
    would hide it)."""
    wallet = to_checksum_address(keccak(text="a wallet whose preparation is quoted")[-20:])
    await chain.rpc("anvil_setCode", [wallet, "0x" + PREPARED_WALLET.hex()])
    declared = deployment_json(Deployment(wallet, wallet, PREPARE))
    check = hex(PREPARED_CHECK_GAS)
    async with served.interface() as http:
        plain = await quote(
            http,
            served.chain_id,
            gas=CALL_GAS,
            call_data_length=CALL_BYTES,
            payer=wallet,
            verifyGas=check,
        )
        prepared = await quote(
            http,
            served.chain_id,
            gas=CALL_GAS,
            call_data_length=CALL_BYTES,
            payer=wallet,
            verifyGas=check,
            deployment=declared,
        )

    assert prepared > plain


async def test_a_wallet_unprepared_is_refused(chain: LocalChain, served: Served) -> None:
    """The same wallet's signature, not wrapped: no preparation to make, its transfer fails
    simulation, and nothing lands."""
    async with served.interface() as http:
        wallet, _, taken = await prepared_wallet(chain, served, http, wrapped=False)

    assert taken.status_code == 422, taken.text
    assert taken.json()["error"]["code"] == -32011
    assert await prepared(chain, wallet) == 0
