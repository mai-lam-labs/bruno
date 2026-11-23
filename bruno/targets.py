"""The contracts a call may be sent to directly, from a concurrency account, without the runner:
targets.json, the Worker's too. Each is sender-agnostic, so a concurrency account calling it
gains nothing there, in its own name, that a later call could use: a whole contract, every
function of it; or a contract's listed functions only (`functions`, by signature). Some are on
every chain, the rest on the chain they're listed under. A call that isn't to one needs a
runner, for isolation: plan.proof_of decides."""

from dataclasses import dataclass
from importlib.resources import files

import msgspec
from eth_typing import ChecksumAddress
from eth_utils import to_checksum_address

from .abi import selector
from .evm import EncodedCall


class _Entry(msgspec.Struct, frozen=True):
    name: str
    address: str
    functions: list[str] | None = None


class _Document(msgspec.Struct, frozen=True):
    everywhere: list[_Entry]
    chains: dict[str, list[_Entry]]


@dataclass(frozen=True, slots=True)
class Target:
    """A contract a call may be sent to directly: its `name`, and the selectors of the functions
    it may be called with, or None for any."""

    name: str
    selectors: frozenset[bytes] | None

    def takes(self, data: bytes) -> bool:
        """Whether a call with `data` may be sent to it directly."""
        return self.selectors is None or data[:4] in self.selectors


def _targets(entries: list[_Entry]) -> dict[ChecksumAddress, Target]:
    return {
        to_checksum_address(e.address): Target(
            e.name,
            None if e.functions is None else frozenset(selector(f) for f in e.functions),
        )
        for e in entries
    }


_DOCUMENT = msgspec.json.decode(
    files("bruno").joinpath("targets.json").read_bytes(), type=_Document
)
_EVERYWHERE = _targets(_DOCUMENT.everywhere)
_ON_CHAIN = {int(chain): _targets(entries) for chain, entries in _DOCUMENT.chains.items()}


def direct(chain_id: int, call: EncodedCall) -> bool:
    """Whether `call` may be sent directly on chain `chain_id`: to a target, with one of its
    functions, sending no ETH."""
    if call.value:
        return False
    target = _EVERYWHERE.get(call.to) or _ON_CHAIN.get(chain_id, {}).get(call.to)
    return target is not None and target.takes(call.data)
