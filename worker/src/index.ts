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
 * - PAYEE_KEY: the payee's private key, as FLOAT_KEY, if given: a secret. It runs the treasury
 *   of each chain that configures one and whose payee it is.
 * - TEST_HOOKS: "1" only in local tests: /__test/{chainId}/... configure the object and drive
 *   its batches and blocks; its alarms run from /start until /stop.
 *
 * A cron trigger, hourly, calls each chain with a treasury: its object runs the treasury when
 * its interval is up. The root reads each chain's last snapshot from its object, never the
 * chain. Paths under /{chain}/__ are the Worker's own: refused from outside.
 *
 * An object's alarms run only while it has work: a submission taken arms them, and they stop
 * once nothing is pending and no funding is in flight, waking again only to forget what's
 * finished. Idle, an object costs nothing.
 */

import { derive } from "./accounts.ts";
import type { Builder } from "./builders.ts";
import { Chain, ChainError, resultOf } from "./chain.ts";
import type { DurableObjectNamespace, DurableObjectState } from "./cloudflare.ts";
import { chainConfig, ConfigError } from "./config.ts";
import { type Address, bytes, checksum, type Hex, NATIVE_ASSET } from "./evm.ts";
import { type Forks, type GasSchedule, NETWORK_FORKS, scheduleAt, SCHEDULES } from "./forks.ts";
import {
  accountsEntry,
  chainIdOf,
  type ChainShown,
  index,
  openapi,
  openrpc,
  page,
  server,
  treasuryEntry,
} from "./front.ts";
import { handle, json } from "./interface.ts";
import { Prices } from "./prices.ts";
import { Relay, type Store } from "./relay.ts";
import { decimalsCall, erc20, measuring, symbolCall, symbolOf } from "./tokens.ts";
import { Fraction } from "./fraction.ts";
import type { Erc20 } from "./runner.ts";
import { type Signer, signer } from "./transactions.ts";
import { statusFromJson, survey } from "./maintenance.ts";
import { storedSnapshot, Treasury, TreasuryError } from "./treasury.ts";

export interface Env {
  readonly RELAY: DurableObjectNamespace;
  readonly CONFIG: string;
  readonly FLOAT_KEY: string;
  readonly PAYEE?: string;
  readonly PAYEE_KEY?: string;
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

  /** The hourly cron: each chain with a treasury runs it, if its interval is up. */
  async scheduled(_controller: unknown, env: Env): Promise<void> {
    const chains = [...served(env).keys()].filter((chainId) => hasTreasury(env, chainId));
    await Promise.allSettled(chains.map(async (chainId) => {
      const stub = env.RELAY.get(env.RELAY.idFromName(String(chainId)));
      const ran = await stub.fetch(
        new Request(`https://internal/${chainId}/__treasury`, { method: "POST" }),
      );
      if (!ran.ok) console.error(`chain ${chainId}: treasury:`, await ran.text());
    }));
  },
};

/** Whether CONFIG gives chain `chainId` a treasury. */
function hasTreasury(env: Env, chainId: number): boolean {
  try {
    const chains = (JSON.parse(env.CONFIG) as { chains?: Record<string, unknown> }).chains ?? {};
    const entry = chains[String(chainId)];
    return typeof entry === "object" && entry !== null && "treasury" in entry;
  } catch {
    return false;
  }
}

/** What the root shows of each chain: its payee, and from its object its float, accounts and
 * treasury's last snapshot, none of it read from the chain. */
async function shownOf(
  env: Env,
  origin: string,
  payees: ReadonlyMap<number, Address>,
): Promise<Map<number, ChainShown>> {
  const shown = new Map<number, ChainShown>();
  await Promise.all(
    [...payees].map(async ([chainId, payee]) => {
      const stub = env.RELAY.get(env.RELAY.idFromName(String(chainId)));
      const answer = await stub.fetch(new Request(`${origin}/${chainId}/__front`));
      const front = (await answer.json()) as Omit<ChainShown, "payee">;
      shown.set(chainId, { payee, ...front });
    }),
  );
  return shown;
}

async function route(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  if (request.method === "GET" && url.pathname === "/") {
    const root = index(url.origin, await shownOf(env, url.origin, served(env)));
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
  const own = (route?.[3] ?? "").startsWith("/__");
  if (route === null || chainId === null || (route[1] !== undefined && !testing) || own) {
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
  readonly payeeKey?: Hex;
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
  private treasury: Treasury | null = null;
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
    if (test === undefined && rest === "/__front") {
      return json(this.front(chainId));
    }
    let relay: Relay;
    try {
      if (test !== undefined) {
        return await this.testHook(chainId, rest, request);
      }
      relay = await this.open(chainId);
      if (rest === "/__treasury") {
        return json({ ran: await this.treasuryIfDue(relay) });
      }
    } catch (e) {
      if (!(e instanceof ConfigError)) throw e;
      return json({ error: { code: -32601, message: e.message } }, 404);
    }
    const response = await handle(relay, request, rest);
    // a submission taken arms the alarms, from now: its batch closes at once. An alarm only
    // waiting to forget finished submissions is replaced, as the Python relay replaces its idle
    // clock: left, it would hold the submission until then
    if (
      this.ticking() && relay.busy &&
      (this.store.get("idle") === "1" || (await this.state.storage.getAlarm()) === null)
    ) {
      this.store.delete("idle");
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
      const idle = this.ticking() && relay !== null && !relay.busy;
      if (!this.ticking()) {
        next = null;
      } else if (relay !== null && !relay.busy) {
        next = relay.forgetAt();
      }
      // whether the alarm is only for forgetting: a submission taken replaces it
      if (idle && next !== null) {
        this.store.put("idle", "1");
      } else {
        this.store.delete("idle");
      }
      if (next === null) {
        await this.state.storage.deleteAlarm();
      } else {
        await this.state.storage.setAlarm(next);
      }
    }
  }

  /** What the root shows of the chain, from what's configured and stored: its float and
   * concurrency accounts (none without the float's key, or a configuration), and its
   * treasury's last snapshot. Nothing is read from the chain. */
  private front(chainId: number): Omit<ChainShown, "payee"> {
    const snapshot = storedSnapshot(this.store);
    const treasury = snapshot === null ? null : treasuryEntry(snapshot);
    const stored = this.store.get("accounts");
    const status = stored === undefined ? null : accountsEntry(statusFromJson(stored));
    try {
      const config = chainConfig(this.chainEntry(chainId));
      const setup = this.testSetup();
      const float = keyBinding(setup === null ? this.env.FLOAT_KEY : setup.floatKey, "FLOAT_KEY");
      const accounts = Array.from({ length: config.accounts }, (_, i) => derive(float, i).address);
      return { floatAccount: float.address, accounts, status, treasury };
    } catch (e) {
      if (!(e instanceof ConfigError)) throw e;
      return { floatAccount: null, accounts: [], status, treasury };
    }
  }

  /** The treasury's run, if it has one and its interval is up: whether it ran. Due again an
   * interval after one succeeds, unless it approved tokens: then at the next cron, to swap them.
   * One that fails is tried at the next cron. */
  private async treasuryIfDue(relay: Relay): Promise<boolean> {
    const settings = relay.config.treasury;
    if (this.treasury === null || settings === null) return false;
    if (Date.now() < Number(this.store.get("treasuryDue") ?? 0)) {
      // not due: the accounts read again while awake, so the root's are an hour old at most
      const latest = await relay.chain.latest();
      relay.recordAccounts(
        await survey(relay.chain, relay.float, relay.config.accounts, latest.number),
      );
      return false;
    }
    if (!(await this.treasury.run()).approving) {
      this.store.put("treasuryDue", String(Date.now() + settings.intervalSeconds * 1000));
    }
    return true;
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
      this.treasury = null;
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
      this.store.delete("idle");
      await this.state.storage.deleteAlarm();
      return json({ ok: true });
    }
    if (hook === "/treasury") {
      if (this.treasury === null) {
        return json({ error: { code: -32601, message: "no treasury" } }, 404);
      }
      return json({ outcome: (await this.treasury.run()).outcome });
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
    // the nodes checked, each token's balanceOf gas measured and its decimals read: one batch
    const erc20s = config.tokens.map((t) => t.address).filter((t) => t !== NATIVE_ASSET);
    const answers = (await chain.batch([
      { method: "eth_chainId", params: [] },
      { method: "eth_getBlockByNumber", params: ["latest", false] },
      ...erc20s.map((t) => measuring(t, config.payee)),
      ...erc20s.map(decimalsCall),
      ...erc20s.map(symbolCall),
    ])).map(resultOf);
    const nodes = Number(BigInt(answers[0] as string));
    if (nodes !== chainId) throw new ChainError(`the nodes are chain ${nodes}, not ${chainId}`);
    const timestamp = Number(BigInt((answers[1] as { timestamp: string }).timestamp));
    const gasSchedule = scheduleAt(forks, timestamp + config.slotSeconds);
    const tokens = new Map<Address, Erc20 | null>();
    const decimals = new Map<Address, number>([[NATIVE_ASSET, 18]]);
    const symbols = new Map<Address, string>([[NATIVE_ASSET, "ETH"]]);
    for (const token of config.tokens) {
      const n = erc20s.indexOf(token.address);
      if (n === -1) {
        tokens.set(token.address, null); // ETH: read with BALANCE
        continue;
      }
      const estimate = Number(BigInt(answers[2 + n] as string));
      tokens.set(token.address, erc20(token.address, config.payee, estimate, gasSchedule));
      decimals.set(token.address, Number(BigInt(answers[2 + erc20s.length + n] as string)));
      symbols.set(token.address, symbolOf(bytes(answers[2 + 2 * erc20s.length + n] as Hex)));
    }
    const prices = new Prices(
      chain,
      config.weth,
      new Map(config.tokens.map((t) => [t.address, t.price])),
      config.priceTtlSeconds,
    );
    let minFeeUnits = 0n;
    if (config.minFee !== null) {
      if (!tokens.has(config.minFee.token)) {
        throw new ConfigError(`min_fee: ${config.minFee.token} isn't one of the chain's tokens`);
      }
      const units = new Fraction(10n ** BigInt(decimals.get(config.minFee.token) as number));
      minFeeUnits = config.minFee.amount.mul(units).ceil();
    }
    this.relay = new Relay(
      chain,
      config,
      float,
      tokens,
      prices,
      builders,
      this.store,
      minFeeUnits,
      decimals,
      symbols,
    );
    const payeeKey = setup === null ? this.env.PAYEE_KEY : setup.payeeKey;
    if (config.treasury !== null && payeeKey !== undefined && payeeKey !== "") {
      const payee = keyBinding(payeeKey, "PAYEE_KEY");
      try {
        // only where the key is this chain's payee's
        if (payee.address === config.payee) {
          this.treasury = new Treasury(this.relay, payee, config.treasury, this.store);
        }
      } catch (e) {
        if (!(e instanceof TreasuryError)) throw e;
        console.error(`chain ${chainId}: no treasury:`, e.message);
      }
    }
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
