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
import { NETWORK_NAMES } from "./forks.ts";
import { qr, svg } from "./qr.ts";
import frontDocument from "./front.json" with { type: "json" };
import openapiDocument from "./openapi.json" with { type: "json" };
import openrpcDocument from "./openrpc.json" with { type: "json" };
import type { Snapshot } from "./treasury.ts";

const CHAIN_ID = /^[1-9]\d{0,14}$/;
const IDS = new Map(Object.entries(NETWORK_NAMES).map(([id, name]) => [name, Number(id)]));
const INFO = openrpcDocument.info;

/** What an account holds of a token (ETH as ERC-7528's address), and its decimals. */
export interface HoldingEntry {
  readonly token: Address;
  readonly amount: string;
  readonly decimals: number;
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
  readonly treasury?: TreasuryEntry;
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
    payee: s.payee.map((h) => ({ token: h.token, amount: q(h.amount), decimals: h.decimals })),
    target: q(s.target),
    reserve: q(s.reserve),
    covers: q(s.covers),
    outcome: s.outcome,
    ...(s.spendADay === null ? {} : { spendADay: q(s.spendADay) }),
  };
}

/** The root's JSON: what the relay is, its documents, and each chain served, with its
 * payee and its treasury's last snapshot, if it has one. */
export function index(
  origin: string,
  payees: ReadonlyMap<number, Address>,
  treasuries: ReadonlyMap<number, TreasuryEntry> = new Map(),
): Index {
  const chains = [...payees.keys()].sort((a, b) => a - b).map((chainId): ChainEntry => {
    const name = NETWORK_NAMES[chainId];
    return {
      chainId,
      jsonRpc: `${origin}/${chainPath(chainId)}`,
      rest: `${origin}/${chainPath(chainId)}/v1`,
      payee: payees.get(chainId) as Address,
      ...(name === undefined ? {} : { name }),
      ...(treasuries.has(chainId) ? { treasury: treasuries.get(chainId) as TreasuryEntry } : {}),
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
  const values: Record<string, string> = {
    treasury: treasuryRows === ""
      ? ""
      : frontDocument.treasury.replace(/\$(\w+)/g, (_, name: string) =>
        name === "rows" ? treasuryRows : ""),
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

/** A chain's treasury on the page: what the float and its accounts hold, how many transactions
 * that covers, the float's target, the payee's holdings, and the last run. */
function treasuryRow(chain: ChainEntry): string {
  const t = chain.treasury as TreasuryEntry;
  const held = BigInt(t.floatBalance) + t.accounts.reduce((a, b) => a + BigInt(b), 0n);
  const payee = t.payee.map((h) =>
    `${amount(BigInt(h.amount), h.decimals)} ` +
    (h.token === NATIVE_ASSET ? "ETH" : `${h.token.slice(0, 6)}…${h.token.slice(-4)}`)
  ).join(", ");
  return `<tr><td>${escape(chain.name ?? String(chain.chainId))}</td>` +
    `<td>${amount(held, 18)} ETH</td><td>${BigInt(t.covers)}</td>` +
    `<td>${amount(BigInt(t.target), 18)} ETH</td><td>${escape(payee)}</td>` +
    `<td>${BigInt(t.block)}: ${escape(t.outcome)}</td></tr>`;
}

/** `units` of a token with `decimals` as a decimal, cut (not rounded) to 6 places, its trailing
 * zeros dropped. */
function amount(units: bigint, decimals: number): string {
  const scale = 10n ** BigInt(decimals);
  const fraction = (units % scale).toString().padStart(decimals, "0").slice(0, 6)
    .replace(/0+$/, "");
  return fraction === "" ? String(units / scale) : `${units / scale}.${fraction}`;
}

/** As Python's html.escape: &, <, >, " and '. */
function escape(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#x27;");
}
