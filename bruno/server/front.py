"""What a visitor to the relay's root finds: the chains it serves and where each is, the
documents describing the interface, and a page saying the same for a browser, with a QR code
of each payee's address for donations.

A chain's path is its network's name where it has one ("sepolia"), else its chain id; either
routes to the chain."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html import escape
from importlib.resources import files
from string import Template

import segno
from eth_typing import ChecksumAddress

from ..evm import NATIVE_ASSET
from ..forks import NETWORK_EXPLORERS, NETWORK_NAMES
from ..maintenance import AccountsStatus
from ..treasury import Snapshot
from .operations import DESCRIPTION, REPOSITORY, SPEC, TITLE, VERSION
from .wire import AccountsEntry, ChainEntry, IndexResult, TreasuryEntry

# a chain in a path: its id as canonical decimal (a safe integer), or a network's name
CHAIN_PATTERN = "^([1-9][0-9]{0,14}|[a-z][a-z0-9-]*)$"
_CHAIN_ID = re.compile("[1-9][0-9]{0,14}")
_IDS = {name: chain_id for chain_id, name in NETWORK_NAMES.items()}
# the page, its $names filled by page(); the Worker's is this one (make vectors)
PAGE = Template(files("bruno.server").joinpath("front.html").read_text())
# the page's accounts section, when a chain names its float: its $rows each account's
ACCOUNTS = Template(
    "<h2>Accounts</h2>\n<table>\n<tr><th>Network</th><th>Account</th><th>Address</th>"
    '<th class="n">ETH</th><th class="n">Nonce</th><th class="n">At block</th></tr>\n'
    "$rows\n</table>"
)
# the page's treasury section, when a chain has one: its $rows each chain's
TREASURY = Template(
    '<h2>Treasury</h2>\n<table>\n<tr><th>Network</th><th class="n">Float (ETH)</th>'
    '<th class="n">Covers</th><th class="n">Target (ETH)</th><th class="n">At block</th></tr>\n'
    "$rows\n</table>"
)


@dataclass(frozen=True, slots=True)
class ChainShown:
    """What the root shows of a chain: its payee, its float and concurrency accounts (none
    without the float's key) and their balances and nonces as last read, and its treasury's
    last snapshot, if it has one."""

    payee: ChecksumAddress
    float_account: ChecksumAddress | None = None
    accounts: tuple[ChecksumAddress, ...] = ()
    status: AccountsStatus | None = None
    snapshot: Snapshot | None = None


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


def index(origin: str, chains: Mapping[int, ChainShown]) -> IndexResult:
    """The root's JSON: what the relay is, its documents, and each chain served, with its
    payee, float and accounts, and its treasury's last snapshot, if it has one."""
    entries = tuple(
        ChainEntry(
            chain_id=chain_id,
            json_rpc=f"{origin}/{chain_path(chain_id)}",
            rest=f"{origin}/{chain_path(chain_id)}/v1",
            payee=shown.payee,
            name=NETWORK_NAMES.get(chain_id),
            float_account=shown.float_account,
            concurrency_accounts=shown.accounts,
            explorer=NETWORK_EXPLORERS.get(chain_id),
            accounts_status=None if shown.status is None else AccountsEntry.of(shown.status),
            treasury=None if shown.snapshot is None else TreasuryEntry.of(shown.snapshot),
        )
        for chain_id, shown in sorted(chains.items())
    )
    return IndexResult(
        title=TITLE,
        description=DESCRIPTION,
        version=VERSION,
        spec=SPEC,
        repository=REPOSITORY,
        openrpc=f"{origin}/openrpc.json",
        openapi=f"{origin}/openapi.json",
        chains=entries,
    )


def page(origin: str, root: IndexResult) -> str:
    """The root's page, for a browser: `root` as HTML."""
    rows = "\n".join(
        f"<tr><td>{escape(c.name or '')}</td><td>{c.chain_id}</td>"
        f"<td><code>{escape(c.json_rpc)}</code></td><td><code>{escape(c.rest)}</code></td></tr>"
        for c in root.chains
    )
    example = root.chains[0].json_rpc if root.chains else f"{origin}/{{chain}}"
    networks: dict[str, list[str]] = {}
    for c in root.chains:
        networks.setdefault(c.payee, []).append(c.name or str(c.chain_id))
    figures = "\n".join(
        f"<figure>{svg(qr(payee))}<figcaption><code>{escape(payee)}</code>: "
        f"{escape(', '.join(names))}</figcaption></figure>"
        for payee, names in networks.items()
    )
    treasuries = "\n".join(treasury_row(c) for c in root.chains if c.treasury is not None)
    accounts = "\n".join(account_rows(c) for c in root.chains if c.float_account is not None)
    return PAGE.substitute(
        title=escape(root.title),
        description=escape(root.description),
        version=escape(root.version),
        chains=rows,
        figures=figures,
        status=status_sections(accounts, treasuries),
        example=escape(example),
        spec=escape(root.spec),
        openrpc=escape(root.openrpc),
        openapi=escape(root.openapi),
        repository=escape(root.repository),
    )


def status_sections(accounts: str, treasuries: str) -> str:
    """Below the rest, after a rule: the accounts' rows as a section, then the treasuries',
    each if there are any; nothing if neither."""
    sections = [
        ACCOUNTS.substitute(rows=accounts) if accounts else "",
        TREASURY.substitute(rows=treasuries) if treasuries else "",
    ]
    shown = "\n".join(s for s in sections if s)
    return f"<hr>\n{shown}" if shown else ""


def account_rows(chain: ChainEntry) -> str:
    """A chain's float and concurrency accounts on the page: each address, shortened and
    linked to the chain's explorer if it has one, and its balance and nonce as last read, if
    they have been."""
    if chain.float_account is None:
        return ""
    status = chain.accounts_status
    network = escape(chain.name or str(chain.chain_id))
    accounts = [
        ("float", chain.float_account),
        *((str(i), a) for i, a in enumerate(chain.concurrency_accounts)),
    ]
    rows: list[str] = []
    for n, (role, address) in enumerate(accounts):
        shown = f"<code>{escape(short(address))}</code>"
        if chain.explorer is not None:
            shown = f'<a href="{escape(chain.explorer)}/address/{escape(address)}">{shown}</a>'
        numbers = ["", "", ""]
        if status is not None:
            balances = [status.float_balance, *status.balances]
            nonces = [status.float_nonce, *status.nonces]
            numbers = [
                amount(int(balances[n], 16), 18),
                str(int(nonces[n], 16)),
                str(int(status.block, 16)),
            ]
        cells = "".join(numeric(x) for x in numbers)
        rows.append(f"<tr><td>{network}</td><td>{role}</td><td>{shown}</td>{cells}</tr>")
    return "\n".join(rows)


def treasury_row(chain: ChainEntry) -> str:
    """A chain's treasury on the page: what the float and its accounts held, how many
    transactions that covered, and the float's target, at the last run's block; beneath, a row
    for each token the payee held, then what the run did."""
    t = chain.treasury
    if t is None:
        return ""
    held = int(t.float_balance, 16) + sum(int(a, 16) for a in t.accounts)
    rows = [
        f"<tr><td>{escape(chain.name or str(chain.chain_id))}</td>"
        f"{numeric(amount(held, 18))}{numeric(str(int(t.covers, 16)))}"
        f"{numeric(amount(int(t.target, 16), 18))}{numeric(str(int(t.block, 16)))}</tr>"
    ]
    for n, h in enumerate(t.payee):
        if h.token == NATIVE_ASSET:
            shown = "ETH"
        else:
            shown = f"<code>{escape(short(h.token))}</code>"
            if chain.explorer is not None:
                shown = f'<a href="{escape(chain.explorer)}/address/{escape(h.token)}">{shown}</a>'
        label = "payee" if n == 0 else ""
        rows.append(
            f"<tr><td>{label}</td>{numeric(amount(int(h.amount, 16), h.decimals))}"
            f'<td colspan="3">{shown}</td></tr>'
        )
    rows.append(f'<tr><td>last run</td><td colspan="4">{escape(t.outcome)}</td></tr>')
    return "\n".join(rows)


def short(address: str) -> str:
    """An address shortened to its first and last few: 0x1234…abcd."""
    return f"{address[:6]}…{address[-4:]}"


def numeric(text: str) -> str:
    """A number's cell: right-aligned, monospaced; empty if there's none."""
    return f'<td class="n"><code>{text}</code></td>' if text else '<td class="n"></td>'


def amount(units: int, decimals: int) -> str:
    """`units` of a token with `decimals` as a decimal of exactly 6 places, cut (not rounded)
    and zero-padded, so a column of them lines up."""
    whole, part = divmod(units, 10**decimals)
    fraction = str(part).rjust(decimals, "0")[:6].ljust(6, "0")
    return f"{whole}.{fraction}"


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
