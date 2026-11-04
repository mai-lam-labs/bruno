"""A call quoted whole, its gas not given: estimated as it will run (made by a runner, after
the transaction's authorizations, its payment's transfer priced from its bound), the gas answered
with the price; the estimate alone, without its headroom, enough for the submission to land, its
transfer made before its call; a call that reverts refused at the quote, and one whose
authority's delegate is unknown refused for want of its gas."""

from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from bruno.abi import address_word, selector
from bruno.tests.calibur import fresh_client
from bruno.tests.interface import quoting, rpc, signed_call, submission
from bruno.tests.localchain import USDC, LocalChain
from bruno.tests.served import MAX_DEADLINE_BLOCKS, Served
from bruno.tests.transfers import CALL_GAS, balance_call, by_key, transfer_json, usdc_transfer

# what the client's call pays the payee: more than any quote here
PAID = 10**7
# a deadline this many blocks ahead
DEADLINE_BLOCKS = 10


def estimate_of(quoted_gas: int) -> int:
    """The estimate a quote's gas is, its headroom (a tenth) taken off: exactly, as it was
    rounded up."""
    return quoted_gas * 10 // 11


async def test_a_delegated_accounts_call_is_estimated_and_lands(
    chain: LocalChain, served: Served
) -> None:
    """A client's call through its EIP-7702 delegation, quoted with its signed authorization and
    no gas: estimated as the delegated account runs it, priced below what it pays; signed with
    the estimate alone, its headroom off, it lands, the payee paid."""
    client = fresh_client("a delegated call, estimated")
    await chain.mint(USDC, client.address, PAID)
    call, auth = await signed_call(chain, client, served.payee, PAID)
    before = await chain.balance_of(USDC, served.payee)
    async with served.interface() as http:
        quoted = await quoting(
            http,
            served.chain_id,
            gas=None,
            call_data_length=None,
            call=call,
            authorizationList=[auth],
        )
        assert quoted.status_code == 200, quoted.text
        gas = int(quoted.json()["gas"], 16)
        body = submission(call, auth, served.payee, PAID, await served.head() + MAX_DEADLINE_BLOCKS)
        body["gas"] = hex(estimate_of(gas))
        taken = await http.post(f"/{served.chain_id}/v1/submissions", json=body)
        assert taken.status_code == 202, taken.text
        await served.land()
        status = (await http.get(taken.headers["Location"])).json()

    assert 0 < gas < CALL_GAS * 4
    assert int(quoted.json()["quotes"][0]["amount"], 16) < PAID
    assert status["status"] == "included", status
    assert await chain.balance_of(USDC, served.payee) - before == PAID


async def test_a_transfers_call_lands_with_the_estimate_alone(
    chain: LocalChain, served: Served
) -> None:
    """A call reading the payee's USDC, paid by a key's signed transfer of USDC: estimated
    without the transfer, which is priced from its bound and made before the call, warming what
    the call reads. Signed with the estimate alone, its headroom off, it lands."""
    key = fresh_client("a transfer's payer, its call estimated")
    data = selector("balanceOf(address)") + address_word(served.payee)
    call = {"to": USDC.address, "data": "0x" + data.hex()}
    async with served.interface() as http:
        quoted = await quoting(
            http, served.chain_id, gas=None, call_data_length=None, call=call, payer=key.address
        )
        assert quoted.status_code == 200, quoted.text
        gas = int(quoted.json()["gas"], 16)
        amount = int(quoted.json()["quotes"][0]["amount"], 16)
        await chain.mint(USDC, key.address, amount)
        transfer = await usdc_transfer(
            served.chain_id, key.address, served.payee, amount, by_key(key)
        )
        body = {
            "call": call,
            "gas": hex(estimate_of(gas)),
            "payment": {
                "token": USDC.address,
                "payee": served.payee,
                "amount": hex(amount),
                "transfer": transfer_json(transfer),
            },
            "maxBlockNumber": hex(await served.head() + DEADLINE_BLOCKS),
        }
        taken = await http.post(f"/{served.chain_id}/v1/submissions", json=body)
        assert taken.status_code == 202, taken.text
        await served.land()
        status = (await http.get(taken.headers["Location"])).json()

    assert status["status"] == "included", status


async def test_a_call_quoted_whole_is_priced_at_its_estimate(served: Served) -> None:
    """A call quoted whole costs no more than the same call given more gas than it needs, and
    the same over REST and JSON-RPC: the gas answered is what it's priced at."""
    reader = fresh_client("a balance read, estimated")
    call = balance_call(reader)["call"]
    async with served.interface() as http:
        rest = await quoting(http, served.chain_id, gas=None, call_data_length=None, call=call)
        json_rpc = await rpc(
            http, served.chain_id, "postpaid_quote", [{"call": call, "tokens": [USDC.address]}]
        )
        generous = await quoting(
            http, served.chain_id, gas=CALL_GAS, call_data_length=None, call=call
        )

    assert rest.status_code == 200, rest.text
    estimated = int(rest.json()["gas"], 16)
    assert 0 < estimated < CALL_GAS
    assert json_rpc["result"] == rest.json()
    assert int(generous.json()["gas"], 16) == CALL_GAS
    amount = int(rest.json()["quotes"][0]["amount"], 16)
    assert amount < int(generous.json()["quotes"][0]["amount"], 16)


async def test_a_call_that_reverts_is_refused_at_its_quote(served: Served) -> None:
    """A transfer of USDC the runner doesn't hold reverts when it's run to estimate its gas:
    simulation failed, with its revert data, over REST and JSON-RPC alike."""
    recipient = fresh_client("a transfer's recipient").address
    data = selector("transfer(address,uint256)") + address_word(recipient) + (1).to_bytes(32)
    call = {"to": USDC.address, "data": "0x" + data.hex()}
    async with served.interface() as http:
        rest = await quoting(http, served.chain_id, gas=None, call_data_length=None, call=call)
        json_rpc = await rpc(
            http, served.chain_id, "postpaid_quote", [{"call": call, "tokens": [USDC.address]}]
        )

    assert rest.status_code == 422, rest.text
    error = rest.json()["error"]
    assert error["code"] == -32011, rest.text
    assert len(error["data"]["revertData"]) > 2, rest.text
    assert json_rpc["error"]["code"] == -32011, json_rpc
    assert json_rpc["error"]["data"] == error["data"], json_rpc


async def test_an_undelegated_authority_needs_its_gas(served: Served) -> None:
    """A call quoted with only its authorization's signer, the account not delegated yet, can't
    be run, its delegate unknown: invalid params, `gas`. Given its gas, it's quoted."""
    authority = to_checksum_address(keccak(text="an authority not delegated yet")[-20:])
    call = {"to": authority, "data": "0x"}
    async with served.interface() as http:
        refused = await quoting(
            http, served.chain_id, gas=None, call_data_length=None, call=call, authority=authority
        )
        given = await quoting(
            http,
            served.chain_id,
            gas=CALL_GAS,
            call_data_length=None,
            call=call,
            authority=authority,
        )

    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["data"] == {"field": "gas"}, refused.text
    assert given.status_code == 200, given.text
