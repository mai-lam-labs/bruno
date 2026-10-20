"""What a visitor to the relay's root finds: the chains it serves and where each is, the
documents describing the interface, and a page saying the same for a browser, with a QR code
a chain to donate to its payee.

A chain's path is its network's name where it has one ("sepolia"), else its chain id; either
routes to the chain."""

import re
from collections.abc import Mapping, Sequence
from html import escape
from importlib.resources import files
from string import Template

import segno
from eth_typing import ChecksumAddress

from ..forks import NETWORK_NAMES
from .operations import DESCRIPTION, REPOSITORY, SPEC, TITLE, VERSION
from .wire import ChainEntry, IndexResult

# a chain in a path: its id as canonical decimal (a safe integer), or a network's name
CHAIN_PATTERN = "^([1-9][0-9]{0,14}|[a-z][a-z0-9-]*)$"
_CHAIN_ID = re.compile("[1-9][0-9]{0,14}")
_IDS = {name: chain_id for chain_id, name in NETWORK_NAMES.items()}
# the page, its $names filled by page(); the Worker's is this one (make vectors)
PAGE = Template(files("bruno.server").joinpath("front.html").read_text())


def chain_path(chain_id: int) -> str:
    """The chain's path segment: its network's name, else its id."""
    return NETWORK_NAMES.get(chain_id, str(chain_id))


def chain_id_of(segment: str) -> int | None:
    """The chain a path segment names, by id or network name: None if it names none."""
    if _CHAIN_ID.fullmatch(segment):
        return int(segment)
    return _IDS.get(segment)


def server(origin: str, chain_id: int) -> dict[str, str]:
    """The chain's JSON-RPC endpoint, as an OpenRPC server."""
    return {"name": chain_path(chain_id), "url": f"{origin}/{chain_path(chain_id)}"}


def index(origin: str, payees: Mapping[int, ChecksumAddress]) -> IndexResult:
    """The root's JSON: what the relay is, its documents, and each chain served, with its
    payee."""
    chains = tuple(
        ChainEntry(
            chain_id=chain_id,
            json_rpc=f"{origin}/{chain_path(chain_id)}",
            rest=f"{origin}/{chain_path(chain_id)}/v1",
            payee=payees[chain_id],
            name=NETWORK_NAMES.get(chain_id),
        )
        for chain_id in sorted(payees)
    )
    return IndexResult(
        title=TITLE,
        description=DESCRIPTION,
        version=VERSION,
        spec=SPEC,
        repository=REPOSITORY,
        openrpc=f"{origin}/openrpc.json",
        openapi=f"{origin}/openapi.json",
        chains=chains,
    )


def page(origin: str, root: IndexResult) -> str:
    """The root's page, for a browser: `root` as HTML."""
    rows = "\n".join(
        f"<tr><td>{escape(c.name or '')}</td><td>{c.chain_id}</td>"
        f"<td><code>{escape(c.json_rpc)}</code></td><td><code>{escape(c.rest)}</code></td></tr>"
        for c in root.chains
    )
    example = root.chains[0].json_rpc if root.chains else f"{origin}/{{chain}}"
    paths = [escape(chain_path(c.chain_id)) for c in root.chains]
    options = "\n".join(
        f'<option value="{p}">{escape(c.name or str(c.chain_id))}</option>'
        for p, c in zip(paths, root.chains, strict=True)
    )
    figures = "\n".join(
        f'<figure id="donate-{p}">{svg(qr(donation(c.chain_id, c.payee)))}'
        f"<figcaption><code>{escape(c.payee)}</code></figcaption></figure>"
        for p, c in zip(paths, root.chains, strict=True)
    )
    rules = "\n".join(
        f'#donate:has(option[value="{p}"]:checked) #donate-{p} {{ display: block; }}' for p in paths
    )
    return PAGE.substitute(
        title=escape(root.title),
        description=escape(root.description),
        version=escape(root.version),
        chains=rows,
        options=options,
        figures=figures,
        rules=rules,
        example=escape(example),
        spec=escape(root.spec),
        openrpc=escape(root.openrpc),
        openapi=escape(root.openapi),
        repository=escape(root.repository),
    )


def donation(chain_id: int, payee: str) -> str:
    """An EIP-681 URI for paying `payee` on the chain: a wallet scanning it knows both."""
    return f"ethereum:{payee}@{chain_id}"


def qr(text: str) -> tuple[bytes, ...]:
    """`text` as a QR code's modules, row by row, 1 dark: byte mode, error correction level
    M, the smallest version that holds it. The Worker's encoder reproduces it (make vectors)."""
    code = segno.make_qr(text, error="m", mode="byte", boost_error=False)
    return tuple(bytes(row) for row in code.matrix)


def svg(rows: Sequence[bytes]) -> str:
    """A QR code as SVG: its dark modules one path, each row's runs a rectangle, inside the
    four-module light border a reader needs."""
    size = len(rows) + 8
    runs: list[str] = []
    for y, row in enumerate(rows):
        x = 0
        while x < len(row):
            start = x
            while x < len(row) and row[x]:
                x += 1
            if x > start:
                runs.append(f"M{start + 4} {y + 4}h{x - start}v1h-{x - start}z")
            x += 1
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
        f'width="{size * 5}" height="{size * 5}" shape-rendering="crispEdges">'
        f'<rect width="{size}" height="{size}" fill="#fff"/>'
        f'<path fill="#000" d="{"".join(runs)}"/></svg>'
    )
