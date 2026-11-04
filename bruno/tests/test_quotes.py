"""A call quoted whole, its gas not given: estimated as it will run (made by a runner, after
the transaction's authorizations, its payment's transfer priced from its bound), the gas answered
with the price; the estimate alone, without its headroom, enough for the submission to land, its
transfer made before its call; a call that reverts refused at the quote, and one whose
authority's delegate is unknown refused for want of its gas."""

import pytest
from eth_account import Account
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from bruno.abi import address_word, selector
from bruno.tests.calibur import fresh_client
from bruno.tests.conftest import ANVIL_HARDFORK
from bruno.tests.interface import quoting, rpc, signed_call, submission
from bruno.tests.localchain import USDC, USDT, LocalChain
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


# anvil's estimates leave out an empty authority's EIP-8037 state gas, which its mining charges
# (foundry-rs/foundry#18126; docs/development/upstream.md): on Amsterdam, an empty authority's
# call is estimated short until anvil's fixed, and then this fails as unexpectedly passing, to
# undo the workaround
_ANVIL_EMPTY_AUTHORITY = pytest.mark.xfail(
    ANVIL_HARDFORK == "amsterdam",
    reason="anvil's estimate leaves out an empty authority's EIP-8037 state gas",
    strict=True,
)


@pytest.mark.parametrize(
    "authority",
    ["funded", pytest.param("empty", marks=_ANVIL_EMPTY_AUTHORITY)],
)
async def test_a_delegated_accounts_call_is_estimated_and_lands(
    chain: LocalChain, served: Served, authority: str
) -> None:
    """A client's call through its EIP-7702 delegation, quoted with its signed authorization and
    no gas: estimated as the delegated account runs it, priced below what it pays; signed with
    the estimate alone, its headroom off, it lands, the payee paid. Its account holding a wei,
    or empty, which EIP-8037 charges for creating."""
    client = fresh_client(f"a delegated call, estimated, its account {authority}")
    await chain.mint(USDC, client.address, PAID)
    if authority == "funded":
        await chain.set_eth_balance(client.address, 1)
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

    assert gas > 0
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


async def test_a_quote_is_broken_down(chain: LocalChain, served: Served) -> None:
    """A call's quote in parts that sum to its amount in every token: the call's always; the
    payment's only with a payer, whose transfer it prices; the setup's only with an
    authorization, whose delegation it prices; the relay's, the rest."""
    client = fresh_client("a call broken down")
    call, auth = await signed_call(chain, client, served.payee, PAID)
    payer = fresh_client("its payer").address
    async with served.interface() as http:
        plain = await quoting(http, served.chain_id, gas=CALL_GAS, call_data_length=None, call=call)
        paid = await quoting(
            http, served.chain_id, gas=CALL_GAS, call_data_length=None, call=call, payer=payer
        )
        delegated = await quoting(
            http,
            served.chain_id,
            gas=CALL_GAS,
            call_data_length=None,
            call=call,
            authorizationList=[auth],
        )

    parts: dict[str, dict[str, dict[str, int]]] = {}
    for name, answer in (("plain", plain), ("paid", paid), ("delegated", delegated)):
        assert answer.status_code == 200, answer.text
        (quote,) = answer.json()["quotes"]
        breakdown = {
            k: {f: int(v, 16) for f, v in p.items()} for k, p in quote["breakdown"].items()
        }
        assert sum(p["amount"] for p in breakdown.values()) == int(quote["amount"], 16), quote
        assert all(p["gas"] >= 0 and p["amount"] >= 0 for p in breakdown.values()), quote
        parts[name] = breakdown
    assert parts["plain"]["call"]["gas"] > CALL_GAS
    assert parts["plain"]["payment"]["gas"] == parts["plain"]["setup"]["gas"] == 0
    assert parts["paid"]["payment"]["gas"] > 0
    assert parts["delegated"]["setup"]["gas"] > 0
    assert parts["plain"]["relay"]["amount"] > 0


async def test_the_transfer_to_sign_is_the_payment(chain: LocalChain, served: Served) -> None:
    """With a payer, each token taking EIP-3009 comes with the transfer to sign, as EIP-712's
    JSON writes it: signed as it is, it pays for the call, which lands. Without a payer there's
    none, nor for a token taking no signed transfer."""
    key = fresh_client("a payer signing what it's given")
    call = balance_call(key)["call"]
    tokens = [USDC.address, USDT.address]
    # the transfer is valid from the latest block's time: the local chain's brought up to now,
    # as a live chain's latest block always is, a slot at most behind
    await served.land()
    async with served.interface() as http:
        unpaid = await quoting(
            http, served.chain_id, gas=CALL_GAS, call_data_length=None, call=call
        )
        quoted = await quoting(
            http,
            served.chain_id,
            gas=CALL_GAS,
            call_data_length=None,
            call=call,
            payer=key.address,
            tokens=tokens,
        )
        assert quoted.status_code == 200, quoted.text
        usdc, usdt = quoted.json()["quotes"]
        typed = usdc["typedData"]
        message = typed["message"]
        await chain.mint(USDC, key.address, int(message["value"]))
        signature = Account.sign_typed_data(key.key, full_message=typed).signature
        body = {
            "call": call,
            "gas": quoted.json()["gas"],
            "payment": {
                "token": USDC.address,
                "payee": served.payee,
                "amount": usdc["amount"],
                "transfer": {
                    "from": message["from"],
                    "validAfter": hex(int(message["validAfter"])),
                    "validBefore": hex(int(message["validBefore"])),
                    "nonce": message["nonce"],
                    "signature": "0x" + bytes(signature).hex(),
                },
            },
            "maxBlockNumber": hex(await served.head() + DEADLINE_BLOCKS),
        }
        taken = await http.post(f"/{served.chain_id}/v1/submissions", json=body)
        assert taken.status_code == 202, taken.text
        await served.land()
        status = (await http.get(taken.headers["Location"])).json()

    assert "typedData" not in unpaid.json()["quotes"][0]
    assert "typedData" not in usdt
    assert (message["from"], message["to"]) == (key.address, served.payee)
    assert int(message["value"]) == int(usdc["amount"], 16)
    assert typed["domain"]["verifyingContract"] == USDC.address
    assert typed["domain"]["chainId"] == served.chain_id
    assert status["status"] == "included", status
