/**
 * The relay as a Cloudflare Worker: the Worker routes each request to its chain's Durable
 * Object, one a chain, which runs that chain's relay (DESIGN.md, Cloudflare Workers).
 *
 * Bindings:
 * - RELAY: the ChainRelay namespace.
 * - CONFIG: the configuration, JSON ({"chains": {...}}, as the Python relay's); a bundler's
 *   identity_key names the binding holding that key.
 * - FLOAT_KEY: the float's private key, as hex: a secret.
 * - TEST_HOOKS: "1" only in local tests: /__test/{chainId}/... configure the object and drive
 *   its batches and blocks; its alarms run from /start until /stop.
 */

import type { Builder } from "./builders.ts";
import { Chain } from "./chain.ts";
import type { DurableObjectNamespace, DurableObjectState } from "./cloudflare.ts";
import { chainConfig, ConfigError } from "./config.ts";
import { type Address, bytes, type Hex } from "./evm.ts";
import { type Forks, type GasSchedule, NETWORK_FORKS, SCHEDULES } from "./forks.ts";
import { handle, json, OPENRPC } from "./interface.ts";
import { Prices } from "./prices.ts";
import { measureErc20, Relay, type Store } from "./relay.ts";
import type { Erc20 } from "./runner.ts";
import { signer } from "./transactions.ts";

export interface Env {
  readonly RELAY: DurableObjectNamespace;
  readonly CONFIG: string;
  readonly FLOAT_KEY: string;
  readonly TEST_HOOKS?: string;
  readonly [binding: string]: unknown;
}

/** A chain id as its canonical decimal (no leading zeros, a safe integer): one object a chain. */
const ROUTE = /^\/(__test\/)?([1-9]\d{0,14})(\/.*)?$/;
/** The shape of what the object stores: a deploy that changes it raises this, and either reads
 * both shapes or migrates what's stored. */
const SCHEMA = "1";
/** How soon an alarm runs again when the next slot can't be told: a failed read, say. */
const RETRY_MS = 1_000;
/** How long after a slot's start its block is looked for. */
const SLOT_OFFSET_MS = 1_000;

export default {
  fetch(request: Request, env: Env): Promise<Response> | Response {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/openrpc.json") {
      return json(OPENRPC);
    }
    const route = ROUTE.exec(url.pathname);
    const testing = env.TEST_HOOKS === "1";
    if (route === null || (route[1] !== undefined && !testing)) {
      return json({ error: { code: -32601, message: "not found" } }, 404);
    }
    const chainId = route[2] as string;
    // a chain the configuration names, so no request makes an object for any other; tests
    // configure theirs through the hooks
    if (!testing && !configured(env, chainId)) {
      return json({ error: { code: -32601, message: `chain ${chainId} isn't served here` } }, 404);
    }
    const stub = env.RELAY.get(env.RELAY.idFromName(chainId));
    return stub.fetch(request);
  },
};

/** What a test configures in place of CONFIG, FLOAT_KEY, the bindings bundlers' identity
 * keys name, and the network's forks. */
interface TestSetup {
  readonly config: unknown;
  readonly forks: readonly (readonly [number, string])[];
  readonly floatKey: Hex;
  readonly identities?: Readonly<Record<string, Hex>>;
}

function configured(env: Env, chainId: string): boolean {
  try {
    const chains = (JSON.parse(env.CONFIG) as { chains?: Record<string, unknown> }).chains;
    return chains !== undefined && Object.hasOwn(chains, chainId);
  } catch {
    return false;
  }
}

/** One chain's relay: its state in SQLite, its batches and blocks driven by alarms. */
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
      return await this.testHook(chainId, rest, request);
    }
    let relay: Relay;
    try {
      relay = await this.open(chainId);
    } catch (e) {
      if (!(e instanceof ConfigError)) throw e;
      return json({ error: { code: -32601, message: e.message } }, 404);
    }
    if (this.env.TEST_HOOKS !== "1" && (await this.state.storage.getAlarm()) === null) {
      await this.state.storage.setAlarm(Date.now());
    }
    return await handle(relay, request, rest);
  }

  /** Each slot's block, and each batch as it falls due; then the next alarm. */
  async alarm(): Promise<void> {
    let next: number | null = Date.now() + RETRY_MS;
    try {
      const relay = await this.open(Number(this.store.get("chainId")));
      const latest = await relay.chain.latest();
      if (String(latest.number) !== this.store.get("head")) {
        await relay.onBlock(latest.number);
        this.store.put("head", String(latest.number));
      }
      const config = chainConfig(this.chainEntry(relay.chain.chainId));
      const batchAt = Number(this.store.get("batchAt") ?? 0);
      if (Date.now() >= batchAt) {
        // due again a batch later whether this one succeeds or not: a failing batch isn't
        // retried every alarm
        this.store.put("batchAt", String(Date.now() + config.batchSeconds * 1000));
        await relay.closeBatch();
      }
      const slot = (latest.timestamp + config.slotSeconds) * 1000 + SLOT_OFFSET_MS;
      next = Math.max(Math.min(slot, Number(this.store.get("batchAt"))), Date.now() + 100);
    } catch (e) {
      if (e instanceof ConfigError) {
        next = null; // its chain isn't configured: nothing to run
      }
      console.error("alarm:", e);
    } finally {
      if (next === null) {
        await this.state.storage.deleteAlarm();
      } else {
        await this.state.storage.setAlarm(next);
      }
    }
  }

  private async testHook(chainId: number, hook: string, request: Request): Promise<Response> {
    if (hook === "/configure") {
      await this.state.storage.deleteAlarm();
      this.state.storage.sql.exec("DELETE FROM kv WHERE key != 'schema'");
      this.store.put("test", await request.text());
      this.relay = null;
      await this.open(chainId);
      return json({ ok: true });
    }
    const relay = await this.open(chainId);
    if (hook === "/start") {
      await this.state.storage.setAlarm(Date.now());
      return json({ ok: true });
    }
    if (hook === "/stop") {
      await this.state.storage.deleteAlarm();
      return json({ ok: true });
    }
    if (hook === "/close-batch") {
      await relay.closeBatch();
      return json({ ok: true });
    }
    if (hook === "/on-block") {
      const { head } = (await request.json()) as { head: number };
      await relay.onBlock(head);
      return json({ ok: true });
    }
    return json({ error: { code: -32601, message: "no such hook" } }, 404);
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
    if (entry === undefined) {
      throw new ConfigError(`chain ${chainId} isn't configured`);
    }
    return entry;
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
    const float = signer(bytes(setup === null ? this.env.FLOAT_KEY as Hex : setup.floatKey));
    const chain = new Chain(chainId, config.rpc, forks, config.slotSeconds);
    await chain.checkChain();
    const gasSchedule = await chain.schedule();
    const tokens = new Map<Address, Erc20 | null>();
    for (const token of config.tokens) {
      if (token.price.length === 0) {
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
    const builders: Builder[] = config.bundlers.map((b) => ({
      url: b.url,
      mode: b.mode,
      identity: b.identityKey === null ? null : signer(
        bytes(setup?.identities?.[b.identityKey] ?? String(this.env[b.identityKey]) as Hex),
      ),
    }));
    this.relay = new Relay(chain, config, float, tokens, prices, builders, this.store);
    return this.relay;
  }
}

function schedule(name: string): GasSchedule {
  const found = SCHEDULES[name];
  if (found === undefined) {
    throw new ConfigError(`no gas schedule ${name}`);
  }
  return found;
}
