/**
 * What a visitor to the relay's root finds: the chains it serves and where each is, the
 * documents describing the interface, and a page saying the same for a browser, with a QR code
 * of each payee's address for donations (the Python
 * reference's bruno/server/front.py, whose page and links make vectors writes to front.json).
 *
 * A chain's path is its network's name where it has one ("sepolia"), else its chain id; either
 * routes to the chain.
 */

import { type Address, NATIVE_ASSET } from "./evm.ts";
import { NETWORK_EXPLORERS, NETWORK_NAMES } from "./forks.ts";
import { qr, svg } from "./qr.ts";
import frontDocument from "./front.json" with { type: "json" };
import openapiDocument from "./openapi.json" with { type: "json" };
import openrpcDocument from "./openrpc.json" with { type: "json" };
import type { AccountsStatus } from "./maintenance.ts";
import type { Snapshot } from "./treasury.ts";

const CHAIN_ID = /^[1-9]\d{0,14}$/;
const IDS = new Map(Object.entries(NETWORK_NAMES).map(([id, name]) => [name, Number(id)]));
const INFO = openrpcDocument.info;

/** What an account holds of a token (ETH as ERC-7528's address), and its decimals. */
export interface HoldingEntry {
  readonly token: Address;
  readonly amount: string;
  readonly decimals: number;
  readonly symbol: string;
}

/** The chain's money as the treasury's last run found it, and what it did: quantities as hex. */
export interface TreasuryEntry {
  readonly at: string;
  readonly block: string;
  readonly floatBalance: string;
  readonly accounts: readonly string[];
  readonly payee: readonly HoldingEntry[];
  readonly target: string;
  readonly reserve: string;
  readonly covers: string;
  readonly outcome: string;
  readonly spendADay?: string;
}

export interface ChainEntry {
  readonly chainId: number;
  readonly jsonRpc: string;
  readonly rest: string;
  readonly payee: Address;
  readonly name?: string;
  readonly floatAccount?: Address;
  readonly concurrencyAccounts?: readonly Address[];
  readonly explorer?: string;
  readonly accountsStatus?: AccountsEntry;
  readonly treasury?: TreasuryEntry;
}

/** The float and each concurrency account's balance and nonce, as last read, at `block`:
 * quantities as hex. */
export interface AccountsEntry {
  readonly block: string;
  readonly floatBalance: string;
  readonly floatNonce: string;
  readonly balances: readonly string[];
  readonly nonces: readonly string[];
}

/** The accounts' status as the root shows it. */
export function accountsEntry(s: AccountsStatus): AccountsEntry {
  const q = (v: bigint | number) => `0x${v.toString(16)}`;
  return {
    block: q(s.block),
    floatBalance: q(s.floatBalance),
    floatNonce: q(s.floatNonce),
    balances: s.accounts.map((a) => q(a.balance)),
    nonces: s.accounts.map((a) => q(a.nonce)),
  };
}

/** What the root shows of a chain: its payee, its float and concurrency accounts (none
 * without the float's key) and their balances and nonces as last read, and its treasury's
 * last snapshot, if it has one. */
export interface ChainShown {
  readonly payee: Address;
  readonly floatAccount: Address | null;
  readonly accounts: readonly Address[];
  readonly status: AccountsEntry | null;
  readonly treasury: TreasuryEntry | null;
}

export interface Index {
  readonly title: string;
  readonly description: string;
  readonly version: string;
  readonly spec: string;
  readonly repository: string;
  readonly openrpc: string;
  readonly openapi: string;
  readonly chains: readonly ChainEntry[];
}

/** The chain's path segment: its network's name, else its id. */
export function chainPath(chainId: number): string {
  return NETWORK_NAMES[chainId] ?? String(chainId);
}

/** The chain a path segment names, by id or network name: null if it names none. */
export function chainIdOf(segment: string): number | null {
  if (CHAIN_ID.test(segment)) {
    return Number(segment);
  }
  return IDS.get(segment) ?? null;
}

/** The chain's JSON-RPC endpoint, as an OpenRPC server. */
export function server(origin: string, chainId: number): { name: string; url: string } {
  return { name: chainPath(chainId), url: `${origin}/${chainPath(chainId)}` };
}

/** The OpenRPC document, its `servers` the endpoints it describes. */
export function openrpc(servers: readonly { name: string; url: string }[]): unknown {
  return { ...openrpcDocument, servers };
}

/** The OpenAPI document, served from `origin`. */
export function openapi(origin: string): unknown {
  return { ...openapiDocument, servers: [{ url: origin }] };
}

/** A treasury's snapshot as the root shows it. */
export function treasuryEntry(s: Snapshot): TreasuryEntry {
  const q = (v: bigint | number) => `0x${v.toString(16)}`;
  return {
    at: q(s.at),
    block: q(s.block),
    floatBalance: q(s.floatBalance),
    accounts: s.accounts.map(q),
    payee: s.payee.map((h) => ({
      token: h.token,
      amount: q(h.amount),
      decimals: h.decimals,
      symbol: h.symbol,
    })),
    target: q(s.target),
    reserve: q(s.reserve),
    covers: q(s.covers),
    outcome: s.outcome,
    ...(s.spendADay === null ? {} : { spendADay: q(s.spendADay) }),
  };
}

/** The root's JSON: what the relay is, its documents, and each chain served, with its
 * payee, float and accounts, and its treasury's last snapshot, if it has one. */
export function index(origin: string, shown: ReadonlyMap<number, ChainShown>): Index {
  const chains = [...shown.keys()].sort((a, b) => a - b).map((chainId): ChainEntry => {
    const name = NETWORK_NAMES[chainId];
    const explorer = NETWORK_EXPLORERS[chainId];
    const chain = shown.get(chainId) as ChainShown;
    return {
      chainId,
      jsonRpc: `${origin}/${chainPath(chainId)}`,
      rest: `${origin}/${chainPath(chainId)}/v1`,
      payee: chain.payee,
      ...(name === undefined ? {} : { name }),
      ...(chain.floatAccount === null ? {} : { floatAccount: chain.floatAccount }),
      ...(chain.accounts.length === 0 ? {} : { concurrencyAccounts: chain.accounts }),
      ...(explorer === undefined ? {} : { explorer }),
      ...(chain.status === null ? {} : { accountsStatus: chain.status }),
      ...(chain.treasury === null ? {} : { treasury: chain.treasury }),
    };
  });
  return {
    title: INFO.title,
    description: INFO.description,
    version: INFO.version,
    spec: frontDocument.spec,
    repository: frontDocument.repository,
    openrpc: `${origin}/openrpc.json`,
    openapi: `${origin}/openapi.json`,
    chains,
  };
}

/** The root's page, for a browser: `root` as HTML. */
export function page(origin: string, root: Index): string {
  const rows = root.chains.map((c) =>
    `<tr><td>${escape(c.name ?? "")}</td><td>${c.chainId}</td>` +
    `<td><code>${escape(c.jsonRpc)}</code></td><td><code>${escape(c.rest)}</code></td></tr>`
  ).join("\n");
  const example = root.chains[0]?.jsonRpc ?? `${origin}/{chain}`;
  const networks = new Map<string, string[]>();
  for (const c of root.chains) {
    networks.set(c.payee, [...(networks.get(c.payee) ?? []), c.name ?? String(c.chainId)]);
  }
  const figures = [...networks].map(([payee, names]) =>
    `<figure>${svg(qr(payee))}<figcaption><code>${escape(payee)}</code>: ` +
    `${escape(names.join(", "))}</figcaption></figure>`
  ).join("\n");
  const treasuryRows = root.chains.filter((c) => c.treasury !== undefined).map(treasuryRow)
    .join("\n");
  const accountRows = root.chains.filter((c) => c.floatAccount !== undefined).map(accountsOf)
    .join("\n");
  const values: Record<string, string> = {
    status: statusSections(accountRows, treasuryRows),
    title: escape(root.title),
    description: escape(root.description),
    version: escape(root.version),
    chains: rows,
    figures,
    example: escape(example),
    spec: escape(root.spec),
    openrpc: escape(root.openrpc),
    openapi: escape(root.openapi),
    repository: escape(root.repository),
  };
  return frontDocument.template.replace(/\$(\w+)/g, (_, name: string) => values[name] ?? "");
}

/** Below the rest, after a rule: the accounts' rows as a section, then the treasuries', each if
 * there are any; nothing if neither. */
function statusSections(accounts: string, treasuries: string): string {
  const section = (template: string, rows: string) =>
    rows === ""
      ? ""
      : template.replace(/\$(\w+)/g, (_, name: string) => name === "rows" ? rows : "");
  const shown = [
    section(frontDocument.accounts, accounts),
    section(frontDocument.treasury, treasuries),
  ].filter((s) => s !== "").join("\n");
  return shown === "" ? "" : `<hr>\n${shown}`;
}

/** A chain's float and concurrency accounts on the page: each address, shortened and linked to
 * the chain's explorer if it has one, and its balance and nonce as last read, if they have
 * been. */
function accountsOf(chain: ChainEntry): string {
  const status = chain.accountsStatus;
  const network = escape(chain.name ?? String(chain.chainId));
  const accounts: [string, Address][] = [
    ["float", chain.floatAccount as Address],
    ...(chain.concurrencyAccounts ?? []).map((a, i): [string, Address] => [String(i), a]),
  ];
  return accounts.map(([role, address], n) => {
    let shown = `<code>${escape(short(address))}</code>`;
    if (chain.explorer !== undefined) {
      shown = `<a href="${escape(chain.explorer)}/address/${escape(address)}">${shown}</a>`;
    }
    let numbers = ["", "", ""];
    if (status !== undefined) {
      numbers = [
        amount(BigInt([status.floatBalance, ...status.balances][n] as string), 18),
        String(BigInt([status.floatNonce, ...status.nonces][n] as string)),
        String(BigInt(status.block)),
      ];
    }
    const cells = numbers.map(numeric).join("");
    return `<tr><td>${network}</td><td>${role}</td><td>${shown}</td>${cells}</tr>`;
  }).join("\n");
}

/** A chain's treasury on the page: what the float and its accounts held, how many transactions
 * that covered, and the float's target, at the last run's block; beneath, a row for each token
 * the payee held, then what the run did. */
function treasuryRow(chain: ChainEntry): string {
  const t = chain.treasury as TreasuryEntry;
  const held = BigInt(t.floatBalance) + t.accounts.reduce((a, b) => a + BigInt(b), 0n);
  const rows = [
    `<tr><td>${escape(chain.name ?? String(chain.chainId))}</td>` +
    `${numeric(amount(held, 18))}${numeric(String(BigInt(t.covers)))}` +
    `${numeric(amount(BigInt(t.target), 18))}${numeric(String(BigInt(t.block)))}</tr>`,
  ];
  t.payee.forEach((h, n) => {
    let token = escape(h.symbol);
    if (h.token !== NATIVE_ASSET && chain.explorer !== undefined) {
      token = `<a href="${escape(chain.explorer)}/address/${escape(h.token)}">${token}</a>`;
    }
    const label = n === 0 ? "payee" : "";
    rows.push(
      `<tr><td>${label}</td>${numeric(amount(BigInt(h.amount), h.decimals))}` +
        `<td colspan="3">${token}</td></tr>`,
    );
  });
  rows.push(`<tr><td>last run</td><td colspan="4">${escape(t.outcome)}</td></tr>`);
  return rows.join("\n");
}

/** An address shortened to its first and last few: 0x1234…abcd. */
function short(address: string): string {
  return `${address.slice(0, 6)}…${address.slice(-4)}`;
}

/** A number's cell: right-aligned, monospaced; empty if there's none. */
function numeric(text: string): string {
  return text === "" ? `<td class="n"></td>` : `<td class="n"><code>${text}</code></td>`;
}

/** `units` of a token with `decimals` as a decimal of exactly 6 places, cut (not rounded) and
 * zero-padded, so a column of them lines up. */
function amount(units: bigint, decimals: number): string {
  const scale = 10n ** BigInt(decimals);
  const fraction = (units % scale).toString().padStart(decimals, "0").slice(0, 6).padEnd(6, "0");
  return `${units / scale}.${fraction}`;
}

/** As Python's html.escape: &, <, >, " and '. */
function escape(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#x27;");
}
