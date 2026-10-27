"""A local configuration merged over the default (make's dist/config.json), as deployed."""

from pathlib import Path

import msgspec

from bruno.common import Json
from bruno.config import Config, merged

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
