/**
 * What a visitor to the relay's root finds: the chains it serves and where each is, the
 * documents describing the interface, and a page saying the same for a browser, with a QR code
 * a chain to donate to its payee (the Python
 * reference's bruno/server/front.py, whose page and links make vectors writes to front.json).
 *
 * A chain's path is its network's name where it has one ("sepolia"), else its chain id; either
 * routes to the chain.
 */

import type { Address } from "./evm.ts";
import { NETWORK_NAMES } from "./forks.ts";
import { qr, svg } from "./qr.ts";
import frontDocument from "./front.json" with { type: "json" };
import openapiDocument from "./openapi.json" with { type: "json" };
import openrpcDocument from "./openrpc.json" with { type: "json" };

const CHAIN_ID = /^[1-9]\d{0,14}$/;
const IDS = new Map(Object.entries(NETWORK_NAMES).map(([id, name]) => [name, Number(id)]));
const INFO = openrpcDocument.info;

export interface ChainEntry {
  readonly chainId: number;
  readonly jsonRpc: string;
  readonly rest: string;
  readonly payee: Address;
  readonly name?: string;
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

/** The root's JSON: what the relay is, its documents, and each chain served, with its
 * payee. */
export function index(origin: string, payees: ReadonlyMap<number, Address>): Index {
  const chains = [...payees.keys()].sort((a, b) => a - b).map((chainId): ChainEntry => {
    const name = NETWORK_NAMES[chainId];
    return {
      chainId,
      jsonRpc: `${origin}/${chainPath(chainId)}`,
      rest: `${origin}/${chainPath(chainId)}/v1`,
      payee: payees.get(chainId) as Address,
      ...(name === undefined ? {} : { name }),
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
  const paths = root.chains.map((c) => escape(chainPath(c.chainId)));
  const options = root.chains.map((c, i) =>
    `<option value="${paths[i]}">${escape(c.name ?? String(c.chainId))}</option>`
  ).join("\n");
  const figures = root.chains.map((c, i) =>
    `<figure id="donate-${paths[i]}">${svg(qr(donation(c.chainId, c.payee)))}` +
    `<figcaption><code>${escape(c.payee)}</code></figcaption></figure>`
  ).join("\n");
  const rules = paths.map((p) =>
    `#donate:has(option[value="${p}"]:checked) #donate-${p} { display: block; }`
  ).join("\n");
  const values: Record<string, string> = {
    title: escape(root.title),
    description: escape(root.description),
    version: escape(root.version),
    chains: rows,
    options,
    figures,
    rules,
    example: escape(example),
    spec: escape(root.spec),
    openrpc: escape(root.openrpc),
    openapi: escape(root.openapi),
    repository: escape(root.repository),
  };
  return frontDocument.template.replace(/\$(\w+)/g, (_, name: string) => values[name] ?? "");
}

/** An EIP-681 URI for paying `payee` on the chain: a wallet scanning it knows both. */
export function donation(chainId: number, payee: string): string {
  return `ethereum:${payee}@${chainId}`;
}

/** As Python's html.escape: &, <, >, " and '. */
function escape(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#x27;");
}
