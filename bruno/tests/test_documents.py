"""The relay's documents and where it's found, on anvil, against both relays: its OpenRPC
document valid and its examples answered, its root listing its chains, as JSON or a page, and
a chain reached by its network's name as by its id."""

import json
from pathlib import Path
from typing import Any

import msgspec
from jsonschema import Draft7Validator, Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT7

from bruno.accounts import derive
from bruno.evm import NATIVE_ASSET
from bruno.server.front import page, qr, svg
from bruno.server.wire import IndexResult
from bruno.tests.calibur import fresh_client
from bruno.tests.interface import origin, quote, rpc, signed_call, submission
from bruno.tests.localchain import TEST_FLOAT, USDC, LocalChain
from bruno.tests.served import Served

# OpenRPC's meta-schema, as released: github.com/open-rpc/meta-schema/releases/tag/1.14.9,
# open-rpc-meta-schema.json (sha256 e5f534fb...71d0f82), the one that admits "1.3.2"; and the
# JSON Schema meta-schema it refers to, at https://meta.json-schema.tools/:
# github.com/json-schema-tools/meta-schema/releases/tag/1.8.3, schema.json (sha256
# 88ca4358...c4a780c8). Both vendored, so validating fetches nothing
FIXTURES = Path(__file__).parent / "fixtures"
OPENRPC_META_SCHEMA = FIXTURES / "open-rpc-meta-schema-1.14.9.json"
JSON_SCHEMA_TOOLS_META_SCHEMA = FIXTURES / "json-schema-tools-meta-schema-1.8.3.json"
JSON_SCHEMA_TOOLS = "https://meta.json-schema.tools"


def openrpc_validator() -> Draft7Validator:
    """OpenRPC's meta-schema, its one remote reference resolved from the fixture."""
    tools = Resource.from_contents(
        json.loads(JSON_SCHEMA_TOOLS_META_SCHEMA.read_text()), default_specification=DRAFT7
    )
    registry = Registry().with_resources(
        [(JSON_SCHEMA_TOOLS, tools), (JSON_SCHEMA_TOOLS + "/", tools)]
    )
    return Draft7Validator(json.loads(OPENRPC_META_SCHEMA.read_text()), registry=registry)


def method_param(document: dict[str, Any], name: str) -> dict[str, Any]:
    """The parameter schema of method `name`, with the components its references name."""
    method = next(m for m in document["methods"] if m["name"] == name)
    return {**method["params"][0]["schema"], "components": document["components"]}


async def test_openrpc_document(chain: LocalChain, served: Served) -> None:
    """The OpenRPC document is valid against OpenRPC's meta-schema; its servers are the
    chains served, and rpc.discover answers it with the one chain's; and its parameter schemas
    take a real submission and refuse one whose quantity isn't one."""
    payee = served.payee
    chain_id = served.chain_id
    client = fresh_client("an OpenRPC client")
    async with served.interface() as http:
        document = (await http.get("/openrpc.json")).json()
        discovered = await rpc(http, chain_id, "rpc.discover", [])
    call, auth = await signed_call(chain, client, payee, 1)
    body = submission(call, auth, payee, 1, 1)
    send = Draft202012Validator(method_param(document, "postpaid_sendTransaction"))

    endpoint = {"name": "anvil", "url": f"{origin(served)}/anvil"}

    openrpc_validator().validate(document)
    openrpc_validator().validate(discovered["result"])
    assert document["servers"] == [endpoint]
    assert discovered["result"] == {**document, "servers": [endpoint]}
    assert {m["name"] for m in document["methods"]} == {
        "postpaid_config",
        "postpaid_quote",
        "postpaid_sendTransaction",
        "postpaid_getStatus",
        "eth_estimateUserOperationGas",
        "eth_sendUserOperation",
        "eth_getUserOperationReceipt",
        "eth_supportedEntryPoints",
        "eth_chainId",
    }
    send.validate(body)
    assert not send.is_valid({**body, "gas": "0x01"})


async def test_the_documents_examples_work(served: Served) -> None:
    """Each example in the documents, as the relay serves them, is a request it answers: the
    OpenRPC methods' over JSON-RPC, and the OpenAPI quote's query over REST."""
    chain_id = served.chain_id
    async with served.interface() as http:
        document = (await http.get("/openrpc.json")).json()
        openapi = (await http.get("/openapi.json")).json()
        examples = {
            m["name"]: [p["value"] for p in m["examples"][0]["params"]]
            for m in document["methods"]
            if "examples" in m
        }
        answers = {name: await rpc(http, chain_id, name, p) for name, p in examples.items()}
        parameters = openapi["paths"]["/{chain}/v1/quote"]["get"]["parameters"]
        query = {
            p["name"]: next(iter(p["examples"].values()))["value"]
            for p in parameters
            if "examples" in p
        }
        rest = await http.get(f"/{chain_id}/v1/quote", params=query)

    assert set(examples) == {
        "postpaid_config",
        "postpaid_quote",
        "eth_supportedEntryPoints",
        "eth_chainId",
    }
    assert all("result" in a for a in answers.values()), answers
    assert set(query) == {"gas", "callBytes", "tokens"}
    assert rest.status_code == 200, rest.text
    assert rest.json()["quotes"][0]["token"] == NATIVE_ASSET
    assert int(rest.json()["quotes"][0]["amount"], 16) > 0


async def test_the_root_lists_the_chains_and_documents(served: Served) -> None:
    """The root says what the relay is, where its documents are and which chains it serves,
    each by its network's name, with its payee: JSON, or a page for a browser, the Python
    reference's page from the same JSON, a QR code of the payee's address among it. Any origin may
    call it."""
    async with served.interface() as http:
        root = await http.get("/")
        browsed = await http.get("/", headers={"accept": "text/html"})
        openapi = (await http.get("/openapi.json")).json()
        preflight = await http.options(
            "/anvil",
            headers={
                "origin": "https://dapp.example",
                "access-control-request-method": "POST",
                "access-control-request-headers": "content-type",
            },
        )
        config = await http.get("/anvil/v1/config", headers={"origin": "https://dapp.example"})
        nowhere = await http.get("/nowhere/v1/config")

    at = origin(served)
    assert root.status_code == 200, root.text
    assert root.json()["chains"] == [
        {
            "chainId": 31337,
            "name": "anvil",
            "jsonRpc": f"{at}/anvil",
            "rest": f"{at}/anvil/v1",
            "payee": served.payee,
            "floatAccount": TEST_FLOAT.address,
            "concurrencyAccounts": [derive(TEST_FLOAT, i).address for i in range(2)],
            "x402": f"{at}/anvil/v1/x402",
            "x402Tokens": [USDC.address],
        }
    ]
    assert "<h2>Accounts</h2>" in browsed.text
    assert f"<code>{TEST_FLOAT.address[:6]}…{TEST_FLOAT.address[-4:]}</code>" in browsed.text
    assert root.json()["openrpc"] == f"{at}/openrpc.json"
    assert root.json()["openapi"] == f"{at}/openapi.json"
    assert root.json()["documentation"] == "https://docs.mailam.app/bruno/"
    assert browsed.headers["content-type"].startswith("text/html")
    assert browsed.text == page(at, msgspec.convert(root.json(), IndexResult))
    assert svg(qr(served.payee)) in browsed.text
    assert "/{chain}/v1/quote" in openapi["paths"]
    assert openapi["servers"] == [{"url": at}]
    assert preflight.status_code in (200, 204), preflight.text
    assert preflight.headers["access-control-allow-origin"] == "*"
    assert config.headers["access-control-allow-origin"] == "*"
    assert nowhere.status_code == 404
    assert nowhere.json()["error"]["message"].endswith(": see /"), nowhere.text


async def test_a_chain_by_name_is_the_chain_by_id(chain: LocalChain, served: Served) -> None:
    """A chain's network name and its chain id reach the same chain: a submission taken by
    one is found by the other, at the path the relay answers with, by name."""
    async with served.interface() as http:
        price = await quote(http, served.chain_id, authority=fresh_client("anyone").address)
        client = fresh_client("by name")
        await chain.mint(USDC, client.address, price)
        call, auth = await signed_call(chain, client, served.payee, price)
        body = submission(call, auth, served.payee, price, await served.head() + 10)
        taken = await http.post("/anvil/v1/submissions", json=body)
        again = await http.post(f"/{served.chain_id}/v1/submissions", json=body)
        by_id = await http.get(taken.headers["Location"].replace("/anvil/", f"/{served.chain_id}/"))

    assert taken.status_code == 202, taken.text
    assert taken.headers["Location"].startswith("/anvil/v1/submissions/0x")
    assert again.status_code == 200, again.text
    assert again.json()["id"] == taken.json()["id"]
    assert by_id.json()["status"] == "pending", by_id.text
