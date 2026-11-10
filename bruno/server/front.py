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

from ..common import whole
from ..forks import NETWORK_EXPLORERS, NETWORK_NAMES
from ..maintenance import AccountsStatus
from ..treasury import Snapshot
from .operations import DESCRIPTION, DOCUMENTATION, REPOSITORY, SOURCE, TITLE, VERSION
from .wire import Address, Quantity, Wire, quantity

# a chain in a path: its id as canonical decimal (a safe integer), or a network's name
CHAIN_PATTERN = whole("[1-9][0-9]{0,14}|[a-z][a-z0-9-]*")
_CHAIN_ID = re.compile("[1-9][0-9]{0,14}")
_IDS = {name: chain_id for chain_id, name in NETWORK_NAMES.items()}
# the page, its $names filled by page(); the Worker's is this one (make vectors)
PAGE = Template(files("bruno.server").joinpath("front.html").read_text())
# the page's accounts section, when a chain names its float: its $rows each account's
ACCOUNTS = Template(
    "<h2>Accounts</h2>\n<table>\n<tr><th>Network</th><th>Account</th><th>Address</th>"
    '<th class="n">Balance</th><th>Asset</th><th class="n">Nonce</th>'
    '<th class="n">At block</th></tr>\n'
    "$rows\n</table>"
)
# the page's treasury section, when a chain has one: its $rows each chain's
TREASURY = Template(
    '<h2>Treasury</h2>\n<table>\n<tr><th>Network</th><th class="n">Covers</th>'
    '<th class="n">Target (ETH)</th><th class="n">Reserve (ETH)</th>'
    '<th class="n">At block</th></tr>\n'
    "$rows\n</table>"
)


class HoldingEntry(Wire, frozen=True):
    """What an account holds of a token (ETH as ERC-7528's address), and its decimals and
    symbol."""

    token: Address
    amount: Quantity
    decimals: int
    symbol: str


class TreasuryEntry(Wire, frozen=True):
    """The treasury's last run, at `block` (`at`, Unix seconds): the float's target and the
    payee's reserve, how many transactions of the largest gas the float and accounts covered,
    the spend a day measured (none on the first run), and what the run did. Balances are the
    accounts' (AccountsEntry), as last read."""

    at: Quantity
    block: Quantity
    target: Quantity
    reserve: Quantity
    covers: Quantity
    outcome: str
    spend_a_day: Quantity | None = None

    @classmethod
    def of(cls, snapshot: Snapshot) -> TreasuryEntry:
        return cls(
            at=quantity(snapshot.at),
            block=quantity(snapshot.block),
            target=quantity(snapshot.target),
            reserve=quantity(snapshot.reserve),
            covers=quantity(snapshot.covers),
            outcome=snapshot.outcome,
            spend_a_day=None if snapshot.spend_a_day is None else quantity(snapshot.spend_a_day),
        )


class AccountsEntry(Wire, frozen=True):
    """The float, each concurrency account and the payee, as last read, at `block`: each's
    balance and nonce, and what the payee holds of each token."""

    block: Quantity
    float_balance: Quantity
    float_nonce: Quantity
    balances: tuple[Quantity, ...]
    nonces: tuple[Quantity, ...]
    payee_balance: Quantity
    payee_nonce: Quantity
    payee_tokens: tuple[HoldingEntry, ...]

    @classmethod
    def of(cls, status: AccountsStatus) -> AccountsEntry:
        return cls(
            block=quantity(status.block),
            float_balance=quantity(status.float_balance),
            float_nonce=quantity(status.float_nonce),
            balances=tuple(quantity(a.balance) for a in status.accounts),
            nonces=tuple(quantity(a.nonce) for a in status.accounts),
            payee_balance=quantity(status.payee_balance),
            payee_nonce=quantity(status.payee_nonce),
            payee_tokens=tuple(
                HoldingEntry(h.token, quantity(h.amount), h.decimals, h.symbol)
                for h in status.payee_tokens
            ),
        )


class ChainEntry(Wire, frozen=True):
    """A chain served: its id, its network's name if it has one, its interface's URLs, the
    payee, to which donations go too, the float and its concurrency accounts and their
    balances and nonces as last read, its block explorer if it has one, its treasury's last
    run, if it has one, its x402 endpoint and the tokens it takes there, if any, and what's
    wrong with it as last measured: none if all's well."""

    chain_id: int
    json_rpc: str
    rest: str
    payee: Address
    name: str | None = None
    float_account: Address | None = None
    concurrency_accounts: tuple[Address, ...] = ()
    explorer: str | None = None
    accounts_status: AccountsEntry | None = None
    treasury: TreasuryEntry | None = None
    x402: str | None = None
    x402_tokens: tuple[Address, ...] = ()
    problems: tuple[str, ...] = ()


class IndexResult(Wire, frozen=True):
    """The relay's root: what it is, where its documents and its source (this version's, and the
    repository) are, and the chains it serves."""

    title: str
    description: str
    version: str
    documentation: str
    repository: str
    source: str
    openrpc: str
    openapi: str
    chains: tuple[ChainEntry, ...]


@dataclass(frozen=True, slots=True)
class ChainShown:
    """What the root shows of a chain: its payee, its float and concurrency accounts (none
    without the float's key) and their balances and nonces as last read, its treasury's
    last snapshot, if it has one, the tokens it takes over x402, and what's wrong with it as
    last measured."""

    payee: ChecksumAddress
    float_account: ChecksumAddress | None = None
    accounts: tuple[ChecksumAddress, ...] = ()
    status: AccountsStatus | None = None
    snapshot: Snapshot | None = None
    x402_tokens: tuple[ChecksumAddress, ...] = ()
    problems: tuple[str, ...] = ()


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
            x402=f"{origin}/{chain_path(chain_id)}/v1/x402" if shown.x402_tokens else None,
            x402_tokens=shown.x402_tokens,
            problems=shown.problems,
        )
        for chain_id, shown in sorted(chains.items())
    )
    return IndexResult(
        title=TITLE,
        description=DESCRIPTION,
        version=VERSION,
        documentation=DOCUMENTATION,
        repository=REPOSITORY,
        source=SOURCE,
        openrpc=f"{origin}/openrpc.json",
        openapi=f"{origin}/openapi.json",
        chains=entries,
    )


def page(origin: str, root: IndexResult) -> str:
    """The root's page, for a browser: `root` as HTML."""
    rows = "\n".join(
        f"<tr><td>{escape(c.name or '')}</td><td>{c.chain_id}</td>"
        f"<td><code>{escape(c.json_rpc)}</code></td><td><code>{escape(c.rest)}</code></td>"
        f"<td>{'' if c.x402 is None else f'<code>{escape(c.x402)}</code>'}</td>"
        f"<td>{escape('; '.join(c.problems)) if c.problems else 'ok'}</td></tr>"
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
        documentation=escape(root.documentation),
        openrpc=escape(root.openrpc),
        openapi=escape(root.openapi),
        repository=escape(root.repository),
        source=escape(root.source),
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
    """A chain's float, concurrency accounts and payee on the page: each address, shortened
    and linked to the chain's explorer if it has one, and its ETH and nonce; beneath the
    payee, what it holds of each token. All as last read, at the one block, if they have
    been."""
    if chain.float_account is None:
        return ""
    status = chain.accounts_status
    network = escape(chain.name or str(chain.chain_id))
    accounts = [
        ("float", chain.float_account),
        *((str(i), a) for i, a in enumerate(chain.concurrency_accounts)),
        ("payee", chain.payee),
    ]
    rows: list[str] = []
    for n, (role, address) in enumerate(accounts):
        numbers = ["", "", ""]
        if status is not None:
            balances = [status.float_balance, *status.balances, status.payee_balance]
            nonces = [status.float_nonce, *status.nonces, status.payee_nonce]
            numbers = [
                amount(int(balances[n], 16), 18),
                str(int(nonces[n], 16)),
                str(int(status.block, 16)),
            ]
        rows.append(
            f"<tr><td>{network}</td><td>{role}</td><td>{linked(chain, address)}</td>"
            f"{numeric(numbers[0])}<td>ETH</td>{numeric(numbers[1])}{numeric(numbers[2])}</tr>"
        )
    if status is not None:
        for h in status.payee_tokens:
            symbol = escape(h.symbol)
            if chain.explorer is not None:
                symbol = (
                    f'<a href="{escape(chain.explorer)}/address/{escape(h.token)}">{symbol}</a>'
                )
            rows.append(
                f"<tr><td></td><td></td><td></td>{numeric(amount(int(h.amount, 16), h.decimals))}"
                f"<td>{symbol}</td>{numeric('')}{numeric('')}</tr>"
            )
    return "\n".join(rows)


def linked(chain: ChainEntry, address: str) -> str:
    """An address shortened, linked to the chain's explorer if it has one."""
    shown = f"<code>{escape(short(address))}</code>"
    if chain.explorer is None:
        return shown
    return f'<a href="{escape(chain.explorer)}/address/{escape(address)}">{shown}</a>'


def treasury_row(chain: ChainEntry) -> str:
    """A chain's treasury on the page: its last run's own figures, at that run's block (how
    many transactions the float and its accounts covered, the float's target, the payee's
    reserve); beneath, what the run did. Balances are the accounts', as last read."""
    t = chain.treasury
    if t is None:
        return ""
    return "\n".join(
        [
            f"<tr><td>{escape(chain.name or str(chain.chain_id))}</td>"
            f"{numeric(str(int(t.covers, 16)))}{numeric(amount(int(t.target, 16), 18))}"
            f"{numeric(amount(int(t.reserve, 16), 18))}{numeric(str(int(t.block, 16)))}</tr>",
            f'<tr><td>last run</td><td colspan="4">{escape(t.outcome)}</td></tr>',
        ]
    )


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
