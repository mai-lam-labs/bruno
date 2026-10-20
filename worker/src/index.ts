/**
 * The relay as a Cloudflare Worker: the Worker routes each request to its chain's Durable
 * Object, one a chain, which runs that chain's relay (DESIGN.md, Cloudflare Workers).
 *
 * Bindings:
 * - RELAY: the ChainRelay namespace.
 * - CONFIG: the configuration, JSON ({"chains": {...}}, as the Python relay's); a bundler's
 *   identity_key names the binding holding that key.
 * - PAYEE: an address, if given: the payee of every chain that doesn't name its own, in place
 *   of CONFIG's top-level payee.
 * - FLOAT_KEY: the float's private key, as 0x-prefixed hex: a secret. It and each identity
 *   key are checked as a chain opens: one missing or malformed is a configuration error,
 *   naming the binding.
 * - TEST_HOOKS: "1" only in local tests: /__test/{chainId}/... configure the object and drive
 *   its batches and blocks; its alarms run from /start until /stop.
 *
 * An object's alarms run only while it has work: a submission taken arms them, and they stop
 * once nothing is pending and no funding is in flight, waking again only to forget what's
 * finished. Idle, an object costs nothing.
 */

import type { Builder } from "./builders.ts";
import { Chain } from "./chain.ts";
import type { DurableObjectNamespace, DurableObjectState } from "./cloudflare.ts";
import { chainConfig, ConfigError } from "./config.ts";
import { type Address, bytes, checksum, type Hex, NATIVE_ASSET } from "./evm.ts";
import { type Forks, type GasSchedule, NETWORK_FORKS, SCHEDULES } from "./forks.ts";
import { chainIdOf, index, openapi, openrpc, page, server } from "./front.ts";
import { handle, json } from "./interface.ts";
import { Prices } from "./prices.ts";
import { Relay, type Store } from "./relay.ts";
import { measureErc20 } from "./tokens.ts";
import type { Erc20 } from "./runner.ts";
import { type Signer, signer } from "./transactions.ts";

export interface Env {
  readonly RELAY: DurableObjectNamespace;
  readonly CONFIG: string;
  readonly FLOAT_KEY: string;
  readonly PAYEE?: string;
  readonly TEST_HOOKS?: string;
  readonly [binding: string]: unknown;
}

/** A chain in the path: its id as canonical decimal (no leading zeros, a safe integer), or its
 * network's name. Either reaches the one object a chain, by its id. */
const ROUTE = /^\/(__test\/)?([1-9]\d{0,14}|[a-z][a-z0-9-]*)(\/.*)?$/;
/** Any origin may call the relay: nothing in it is bound to a cookie or an origin. */
const CORS: Readonly<Record<string, string>> = {
  "access-control-allow-origin": "*",
  "access-control-expose-headers": "Location",
};
/** The shape of what the object stores: a deploy that changes it raises this, and either reads
 * both shapes or migrates what's stored. */
const SCHEMA = "1";
/** How soon an alarm runs again after one fails (the nodes not answering, say): twice as long
 * after each failure in a row, up to MAX_RETRY_MS. */
const RETRY_MS = 1_000;
const MAX_RETRY_MS = 300_000;
/** How long after a slot's start its block is looked for. */
const SLOT_OFFSET_MS = 1_000;

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const response = request.method === "OPTIONS" ? preflight() : await route(request, env);
    const answered = new Response(response.body, response);
    for (const [name, value] of Object.entries(CORS)) answered.headers.set(name, value);
    return answered;
  },
};

async function route(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  if (request.method === "GET" && url.pathname === "/") {
    const root = index(url.origin, served(env));
    if ((request.headers.get("accept") ?? "").includes("text/html")) {
      return new Response(page(url.origin, root), {
        headers: { "content-type": "text/html; charset=utf-8" },
      });
    }
    return json(root);
  }
  if (request.method === "GET" && url.pathname === "/openrpc.json") {
    return json(
      openrpc(
        [...served(env).keys()].sort((a, b) => a - b).map((chainId) => server(url.origin, chainId)),
      ),
    );
  }
  if (request.method === "GET" && url.pathname === "/openapi.json") {
    return json(openapi(url.origin));
  }
  const route = ROUTE.exec(url.pathname);
  const testing = env.TEST_HOOKS === "1";
  const chainId = route === null ? null : chainIdOf(route[2] as string);
  if (route === null || chainId === null || (route[1] !== undefined && !testing)) {
    return json({ error: { code: -32601, message: "not found: see /" } }, 404);
  }
  // a chain the configuration names, so no request makes an object for any other; tests
  // configure theirs through the hooks
  if (!testing && !served(env).has(chainId)) {
    const message = `chain ${route[2]} isn't served here: see /`;
    return json({ error: { code: -32601, message } }, 404);
  }
  // on to the chain's object by its id, the path's chain its id: a name and an id are the same
  url.pathname = `/${route[1] ?? ""}${chainId}${route[3] ?? ""}`;
  const stub = env.RELAY.get(env.RELAY.idFromName(String(chainId)));
  return await stub.fetch(new Request(url, request));
}

/** A browser's preflight: GET and POST, with a JSON body, from anywhere. */
function preflight(): Response {
  return new Response(null, {
    status: 204,
    headers: {
      "access-control-allow-methods": "GET, POST",
      "access-control-allow-headers": "content-type",
      "access-control-max-age": "86400",
    },
  });
}

/** What a test configures in place of CONFIG, FLOAT_KEY, the bindings bundlers' identity
 * keys name, and the network's forks. */
interface TestSetup {
  readonly config: unknown;
  readonly forks: readonly (readonly [number, string])[];
  readonly floatKey: Hex;
  readonly identities?: Readonly<Record<string, Hex>>;
}

/** The chains the configuration names, each with its payee: a chain without a valid payee
 * isn't served (it couldn't open), and none is if the configuration doesn't parse. */
function served(env: Env): Map<number, Address> {
  const payees = new Map<number, Address>();
  try {
    const chains = (JSON.parse(env.CONFIG) as { chains?: Record<string, unknown> }).chains ?? {};
    for (const [key, entry] of Object.entries(chains)) {
      const chainId = chainIdOf(key);
      const payee = typeof entry === "object" && entry !== null ? payeeOf(env, entry) : null;
      if (chainId !== null && payee !== null) {
        payees.set(chainId, checksum(payee));
      }
    }
  } catch {
    return new Map();
  }
  return payees;
}

/** A chain's payee: its own, else PAYEE, else CONFIG's top-level payee; null if none. */
function payeeOf(env: Env, entry: object): string | null {
  const own = (entry as { payee?: unknown }).payee;
  if (typeof own === "string") return own;
  if (typeof env.PAYEE === "string" && env.PAYEE !== "") return env.PAYEE;
  try {
    const top = (JSON.parse(env.CONFIG) as { payee?: unknown }).payee;
    return typeof top === "string" ? top : null;
  } catch {
    return null;
  }
}

/** One chain's relay: its state in SQLite, its batches and blocks driven by alarms while it
 * has work. */
export class ChainRelay {
  private relay: Relay | null = null;
  /** The relay being opened: concurrent requests, and an alarm, wait for the one. */
  private opening: Promise<Relay> | null = null;
  private readonly store: Store;

  constructor(private readonly state: DurableObjectState, private readonly env: Env) {
    const sql = state.storage.sql;
    sql.exec("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)");
    const stored = sql.exec("SELECT value FROM kv WHERE key = 'schema'").toArray()[0];
    if (stored === undefined) {
      sql.exec("INSERT INTO kv (key, value) VALUES ('schema', ?)", SCHEMA);
    } else if (String(stored.value) !== SCHEMA) {
      throw new Error(`stored state is schema ${stored.value}, this code reads ${SCHEMA}`);
    }
    this.store = {
      get: (key) => {
        const row = sql.exec("SELECT value FROM kv WHERE key = ?", key).toArray()[0];
        return row === undefined ? undefined : String(row.value);
      },
      delete: (key) => {
        sql.exec("DELETE FROM kv WHERE key = ?", key);
      },
      put: (key, value) => {
        sql.exec(
          "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
          key,
          value,
        );
      },
      list: (prefix) =>
        sql.exec("SELECT value FROM kv WHERE key LIKE ? ORDER BY rowid", `${prefix}%`)
          .toArray().map((row) => String(row.value)),
    };
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    const [, test, chain, rest = ""] = ROUTE.exec(url.pathname) ?? [];
    const chainId = Number(chain);
    if (test !== undefined) {
      // checked again here: an object answers hooks only where the Worker's binding allows
      if (this.env.TEST_HOOKS !== "1") {
        return json({ error: { code: -32601, message: "not found" } }, 404);
      }
    }
    let relay: Relay;
    try {
      if (test !== undefined) {
        return await this.testHook(chainId, rest, request);
      }
      relay = await this.open(chainId);
    } catch (e) {
      if (!(e instanceof ConfigError)) throw e;
      return json({ error: { code: -32601, message: e.message } }, 404);
    }
    const response = await handle(relay, request, rest);
    // a submission taken arms the alarms, from now: its batch closes at once
    if (this.ticking() && relay.busy && (await this.state.storage.getAlarm()) === null) {
      await this.state.storage.setAlarm(Date.now());
    }
    return response;
  }

  /**
   * While there's work, each slot's block and each batch as it falls due, then the next alarm.
   * With none, the finished submissions due to be forgotten, then an alarm only for the next of
   * those: no reads of the chain, and no alarm at all once there's nothing left.
   */
  async alarm(): Promise<void> {
    let next: number | null = null;
    let relay: Relay | null = null;
    try {
      relay = await this.open(Number(this.store.get("chainId")));
      relay.prune();
      if (relay.busy) {
        next = await this.step(relay);
      }
      this.store.delete("failures");
    } catch (e) {
      if (!(e instanceof ConfigError)) {
        const failures = Number(this.store.get("failures") ?? 0);
        this.store.put("failures", String(failures + 1));
        next = Date.now() + Math.min(RETRY_MS * 2 ** failures, MAX_RETRY_MS);
      } // else its chain isn't configured: nothing to run
      console.error("alarm:", e);
    } finally {
      // decided with nothing awaited since checking: a submission taken meanwhile is either seen
      // here, or arms the alarm itself after this
      if (!this.ticking()) {
        next = null;
      } else if (relay !== null && !relay.busy) {
        next = relay.forgetAt();
      }
      if (next === null) {
        await this.state.storage.deleteAlarm();
      } else {
        await this.state.storage.setAlarm(next);
      }
    }
  }

  /** The block, if there's a new one, and the batch, if it's due, both as of the one read of
   * the latest block: when the next alarm is. */
  private async step(relay: Relay): Promise<number> {
    const latest = await relay.chain.latest();
    if (String(latest.number) !== this.store.get("head")) {
      await relay.onBlock(latest);
      this.store.put("head", String(latest.number));
    }
    const config = chainConfig(this.chainEntry(relay.chain.chainId));
    const batchAt = Number(this.store.get("batchAt") ?? 0);
    if (Date.now() >= batchAt) {
      // due again a batch later whether this one succeeds or not: a failing batch isn't
      // retried every alarm
      this.store.put("batchAt", String(Date.now() + config.batchSeconds * 1000));
      await relay.closeBatch(latest);
    }
    const slot = (latest.timestamp + config.slotSeconds) * 1000 + SLOT_OFFSET_MS;
    return Math.max(Math.min(slot, Number(this.store.get("batchAt"))), Date.now() + 100);
  }

  private async testHook(chainId: number, hook: string, request: Request): Promise<Response> {
    if (hook === "/configure") {
      await this.state.storage.deleteAlarm();
      this.store.delete("ticking");
      this.state.storage.sql.exec("DELETE FROM kv WHERE key != 'schema'");
      this.store.put("test", await request.text());
      this.relay = null;
      await this.open(chainId);
      return json({ ok: true });
    }
    const relay = await this.open(chainId);
    if (hook === "/start") {
      this.store.put("ticking", "1");
      await this.state.storage.setAlarm(Date.now());
      return json({ ok: true });
    }
    if (hook === "/stop") {
      this.store.delete("ticking");
      await this.state.storage.deleteAlarm();
      return json({ ok: true });
    }
    if (hook === "/alarm") {
      return json({ alarm: await this.state.storage.getAlarm() });
    }
    if (hook === "/close-batch") {
      await relay.closeBatch(await relay.chain.latest());
      return json({ ok: true });
    }
    if (hook === "/on-block") {
      await relay.onBlock(await relay.chain.latest()); // the latest block, as an alarm would
      return json({ ok: true });
    }
    return json({ error: { code: -32601, message: "no such hook" } }, 404);
  }

  /** Whether alarms run: always, unless hooks are on, where a test runs them from /start. */
  private ticking(): boolean {
    return this.env.TEST_HOOKS !== "1" || this.store.get("ticking") === "1";
  }

  /** What a test configured, where hooks are on; never otherwise. */
  private testSetup(): TestSetup | null {
    const test = this.env.TEST_HOOKS === "1" ? this.store.get("test") : undefined;
    return test === undefined ? null : (JSON.parse(test) as TestSetup);
  }

  private chainEntry(chainId: number): unknown {
    const setup = this.testSetup();
    if (setup !== null) {
      return setup.config;
    }
    const chains = (JSON.parse(this.env.CONFIG) as { chains?: Record<string, unknown> }).chains;
    const entry = chains?.[String(chainId)];
    if (typeof entry !== "object" || entry === null) {
      throw new ConfigError(`chain ${chainId} isn't configured`);
    }
    const payee = payeeOf(this.env, entry);
    if (payee === null) {
      throw new ConfigError(`chain ${chainId}: no payee (its own, CONFIG's, or PAYEE)`);
    }
    return { ...entry, payee };
  }

  /** The chain's relay: its nodes checked, its tokens' balanceOf gas measured, once. */
  private async open(chainId: number): Promise<Relay> {
    if (this.relay !== null) {
      return this.relay;
    }
    this.opening ??= this.load(chainId).finally(() => {
      this.opening = null;
    });
    return await this.opening;
  }

  private async load(chainId: number): Promise<Relay> {
    const config = chainConfig(this.chainEntry(chainId));
    this.store.put("chainId", String(chainId));
    const setup = this.testSetup();
    const forks: Forks = setup === null
      ? NETWORK_FORKS[chainId] ?? []
      : setup.forks.map(([at, name]) => [at, schedule(name)] as const);
    const float = keyBinding(setup === null ? this.env.FLOAT_KEY : setup.floatKey, "FLOAT_KEY");
    const builders: Builder[] = config.bundlers.map((b) => ({
      url: b.url,
      mode: b.mode,
      // without an identity key of its own, the float signs
      identity: b.identityKey === null
        ? float
        : keyBinding(setup?.identities?.[b.identityKey] ?? this.env[b.identityKey], b.identityKey),
    }));
    const chain = new Chain(chainId, config.rpc, forks, config.slotSeconds);
    await chain.checkChain();
    const gasSchedule = await chain.schedule();
    const tokens = new Map<Address, Erc20 | null>();
    for (const token of config.tokens) {
      if (token.address === NATIVE_ASSET) {
        tokens.set(token.address, null); // ETH: read with BALANCE
      } else {
        tokens.set(
          token.address,
          await measureErc20(chain, token.address, config.payee, gasSchedule),
        );
      }
    }
    const prices = new Prices(
      chain,
      config.weth,
      new Map(config.tokens.map((t) => [t.address, t.price])),
      config.priceTtlSeconds,
    );
    this.relay = new Relay(chain, config, float, tokens, prices, builders, this.store);
    return this.relay;
  }
}

/** The signer for the private key bound as `name`: a ConfigError naming the binding, never
 * showing its value, if it holds no key. */
function keyBinding(value: unknown, name: string): Signer {
  if (typeof value !== "string" || !/^0x[0-9a-fA-F]{64}$/.test(value)) {
    throw new ConfigError(`${name}: no private key bound (32 bytes, as 0x-prefixed hex)`);
  }
  try {
    return signer(bytes(value as Hex));
  } catch {
    throw new ConfigError(`${name}: not a secp256k1 private key`);
  }
}

function schedule(name: string): GasSchedule {
  const found = SCHEDULES[name];
  if (found === undefined) {
    throw new ConfigError(`no gas schedule ${name}`);
  }
  return found;
}
