"""A local configuration merged over the default (make's dist/config.json), as deployed."""

from pathlib import Path

import msgspec

from bruno.common import Json
from bruno.config import Config, merged
from bruno.jsonrpc import Node

DEFAULT = Path(__file__).parents[2] / "config.default.json"


def config(document: Json) -> Config:
    """`document` read as the relay reads its file: chain ids from the JSON's string keys."""
    return msgspec.json.decode(msgspec.json.encode(document), type=Config)


def test_a_local_configuration_overrides_only_what_it_names() -> None:
    """A chain's nodes replaced whole, as listed; the rest of that chain, and every other
    chain, as the default has them; nothing to merge, the default unchanged."""
    default: Json = msgspec.json.decode(DEFAULT.read_bytes())
    nodes: list[Json] = [
        "https://eth-mainnet.g.alchemy.com/v2/KEY",
        "https://ethereum-rpc.publicnode.com",
    ]
    local: Json = {"chains": {"1": {"rpc": nodes}}}
    expected = config(default)

    result = config(merged(default, local))

    assert result.chains[1].rpc == tuple(nodes)
    assert result.chains[1].tokens == expected.chains[1].tokens
    assert result.chains[11_155_111] == expected.chains[11_155_111]
    assert config(merged(default, {})) == expected


def test_a_local_configurations_extra_nodes_join_the_defaults() -> None:
    """A chain's rpc_extra in a local configuration leaves the default's rpc as it is: the
    chain is read by both, the default's first."""
    default: Json = msgspec.json.decode(DEFAULT.read_bytes())
    keyed = "https://eth-mainnet.g.alchemy.com/v2/KEY"
    expected = config(default)

    result = config(merged(default, {"chains": {"1": {"rpc_extra": [keyed]}}}))

    assert result.chains[1].rpc == expected.chains[1].rpc
    assert result.chains[1].nodes == (*expected.chains[1].nodes, Node(keyed))
    assert result.chains[11_155_111].nodes == expected.chains[11_155_111].nodes
