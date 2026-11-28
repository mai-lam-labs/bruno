/**
 * The relay as a Cloudflare Worker: the Worker routes each request to its chain's Durable
 * Object, one a chain, which runs that chain's relay (docs/development/design/worker.md).
 *
 * The configuration is configuration.js's text, uploaded beside the bundle.
 *
 * Bindings:
 * - RELAY: the ChainRelay namespace.
 * - PAYEE: an address, if given: the payee of every chain that doesn't name its own, in place
 *   of the configuration's top-level payee.
 * - FLOAT_KEY: the float's private key, as 0x-prefixed hex: a secret. It and each identity
 *   key are checked as a chain opens: one missing or malformed is a configuration error,
 *   naming the binding.
 * - PAYEE_KEY: the payee's private key, as FLOAT_KEY, if given: a secret. It runs the treasury
 *   of each chain that configures one and whose payee it is.
 * - Each bundler's identity key: the binding its identity_key names, a secret.
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

import CONFIG from "./configuration.js";
import { derive } from "./accounts.ts";
import type { Builder } from "./builders.ts";
import { Chain } from "./chain.ts";
import type { DurableObjectNamespace, DurableObjectState } from "./cloudflare.ts";
import { chainConfig, ConfigError } from "./config.ts";
import { type Address, bytes, checksum, type Hex, hexQuantity } from "./evm.ts";
import {
  accountsEntry,
  chainIdOf,
  type ChainShown,
  index,
  openapi,
  page,
  server,
  treasuryEntry,
} from "./front.ts";
import {
  errorResponse,
  facilitate,
  facilitatorRoute,
  handle,
  json,
  methodNotAllowed,
  notFound,
  unsettled,
} from "./app.ts";
import { errorObject, NotServedError } from "./operations.ts";
import { openrpc } from "./rpc.ts";
import { INVALID_NETWORK, INVALID_PAYLOAD, x402Url } from "./x402.ts";
import { Prices } from "./prices.ts";
import { payeeTokens } from "./batches.ts";
import { forwarderOf } from "./forwarder.ts";
import { MAX_RETRY, Relay, RETRY } from "./relay.ts";
import type { Store } from "./store.ts";
import { Fraction } from "./fraction.ts";
import { type Signer, signer } from "./transactions.ts";
import { statusFromJson, survey } from "./maintenance.ts";
import { storedSnapshot, Treasury, TreasuryError } from "./treasury.ts";
import { Millis, millisOf, Seconds } from "./units.ts";

export interface Env {
  readonly RELAY: DurableObjectNamespace;
  readonly FLOAT_KEY: string;
  readonly PAYEE?: string;
  readonly PAYEE_KEY?: string;
  readonly TEST_HOOKS?: string;
  readonly [binding: string]: unknown;
}

/** A chain in the path: its id as canonical decimal (no leading zeros, a safe integer), or its
 * network's name. Either reaches the one object a chain, by its id. */
const ROUTE = /^\/(__test\/)?([1-9]\d{0,14}|[a-z][a-z0-9-]*)(\/.*)?$/;
/** Where the hosted x402 facilitator is: a payout address, its terms and an endpoint follow. */
const FACILITATOR = "/x402/facilitator";
/** The facilitator's path within a chain's object, the Worker's own: its chain is the
 * payment's. */
const FACILITATOR_OWN = "/__facilitator";
/** The Worker's own paths, each taking GET alone. */
const ROOT = new Set(["/", "/openrpc.json", "/openapi.json", "/.well-known/x402"]);
/** Any origin may call the relay: nothing in it is bound to a cookie or an origin. */
const CORS: Readonly<Record<string, string>> = {
  "access-control-allow-origin": "*",
  "access-control-expose-headers":
    "Location, PAYMENT-REQUIRED, PAYMENT-RESPONSE, EXTENSION-RESPONSES",
};
/** The shape of what the object stores: a deploy that changes it raises this, and either reads
 * both shapes or migrates what's stored. */
const SCHEMA = "1";
/** Where an object keeps whether its alarm is only for forgetting, and where 0.5.4 kept it. */
const WAITING_TO_FORGET = "waitingToForget";
const WAITING_TO_FORGET_BEFORE_060 = "idle";

/** A relay's own clock, as it stands: busy with work for the next block (a submission pending,
 * or a funding in flight); only waiting to forget finished submissions; its alarm set at all
 * (bruno/relay.py's ClockState). */
interface ClockState {
  readonly busy: boolean;
  readonly waitingToForget: boolean;
  readonly set: boolean;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const response = request.method === "OPTIONS" ? preflight() : await route(request, env);
    const answered = new Response(response.body, response);
    for (const [name, value] of Object.entries(CORS)) answered.headers.set(name, value);
    return answered;
  },

  /** The hourly cron: each chain with a treasury runs it, if its interval is up. */
  async scheduled(_controller: unknown, env: Env): Promise<void> {
    const chains = [...served(env).keys()].filter(hasTreasury);
    const runs = await Promise.allSettled(chains.map(async (chainId) => {
      const stub = env.RELAY.get(env.RELAY.idFromName(String(chainId)));
      const ran = await stub.fetch(
        new Request(`https://internal/${chainId}/__treasury`, { method: "POST" }),
      );
      if (!ran.ok) console.error(`chain ${chainId}: treasury:`, await ran.text());
    }));
    // a chain's object that threw: its run failed, and nothing else would say so
    for (const [i, run] of runs.entries()) {
      if (run.status === "rejected") console.error(`chain ${chains[i]}: treasury:`, run.reason);
    }
  },
};

/** The least min_fee of `chains` (those taking x402 payments), as written: x402's price, at
 * least; null if none has one. */
function leastFee(chains: readonly ChainShown[]): string | null {
  return chains.map((c) => c.x402MinFee).filter((f): f is string => f !== null).reduce<
    string | null
  >(
    (least, f) =>
      least === null || Fraction.decimal(f).compare(Fraction.decimal(least)) < 0 ? f : least,
    null,
  );
}

/** Whether the configuration gives chain `chainId` a treasury. */
function hasTreasury(chainId: number): boolean {
  try {
    const chains = (JSON.parse(CONFIG) as { chains?: Record<string, unknown> }).chains ?? {};
    const entry = chains[String(chainId)];
    return typeof entry === "object" && entry !== null && "treasury" in entry;
  } catch {
    return false;
  }
}

/** What the root shows of each chain: its payee and the payee's forwarder, and from its object
 * its float, accounts and treasury's last snapshot, none of it read from the chain. */
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
      const front = (await answer.json()) as Omit<ChainShown, "payee" | "forwarder">;
      shown.set(chainId, { payee, forwarder: forwarderOf(payee).address, ...front });
    }),
  );
  return shown;
}

async function route(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);
  if (request.method !== "GET" && ROOT.has(url.pathname)) return methodNotAllowed("GET");
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
    const x402 = [...(await shownOf(env, url.origin, served(env))).values()]
      .filter((c) => c.x402Tokens.length > 0);
    return json(openapi(url.origin, x402.length > 0, leastFee(x402)));
  }
  if (request.method === "GET" && url.pathname === "/.well-known/x402") {
    const shown = await shownOf(env, url.origin, served(env));
    const resources = [...shown].filter(([, c]) => c.x402Tokens.length > 0)
      .map(([chainId]) => chainId).sort((a, b) => a - b).map((c) => x402Url(url.origin, c));
    return json({ version: 1, resources });
  }
  if (url.pathname === FACILITATOR || url.pathname.startsWith(`${FACILITATOR}/`)) {
    return await facilitator(request, env, url);
  }
  const route = ROUTE.exec(url.pathname);
  const testing = env.TEST_HOOKS === "1";
  const chainId = route === null ? null : chainIdOf(route[2] as string);
  const own = (route?.[3] ?? "").startsWith("/__");
  if (route === null || (route[1] !== undefined && !testing) || own) {
    return notFound();
  }
  // a chain the configuration names, so no request makes an object for any other; tests
  // configure theirs through the hooks
  if (chainId === null || (!testing && !served(env).has(chainId))) {
    return errorResponse(new NotServedError(route[2] as string));
  }
  // on to the chain's object by its id, the path's chain its id: a name and an id are the same
  url.pathname = `/${route[1] ?? ""}${chainId}${route[3] ?? ""}`;
  const stub = env.RELAY.get(env.RELAY.idFromName(String(chainId)));
  return await stub.fetch(new Request(url, request));
}

/**
 * The hosted x402 facilitator, which has no chain in its path: a payout address's page and
 * /supported are each chain's part, joined; a payment sent to /verify or /settle goes to the
 * object of the chain it names, at a path of the Worker's own.
 */
async function facilitator(request: Request, env: Env, url: URL): Promise<Response> {
  const rest = url.pathname.slice(FACILITATOR.length);
  let endpoint: string;
  try {
    endpoint = facilitatorRoute(rest).endpoint;
  } catch (e) {
    return errorResponse(e);
  }
  const to = (chainId: number): Promise<Response> => {
    const stub = env.RELAY.get(env.RELAY.idFromName(String(chainId)));
    const inner = new URL(url);
    inner.pathname = `/${chainId}/__facilitator${rest}`;
    return stub.fetch(new Request(inner, request));
  };
  if (request.method === "GET") {
    if (endpoint !== "" && endpoint !== "supported") return notFound();
    const parts = await Promise.all([...served(env).keys()].sort((a, b) => a - b).map(to));
    const joined: Record<string, unknown> = {};
    for (const part of parts) {
      if (!part.ok) return part;
      for (const [key, value] of Object.entries(await part.json() as Record<string, unknown>)) {
        const had = joined[key];
        // a list is each chain's entries; a map, each chain's own key; anything else the same
        if (Array.isArray(value) && Array.isArray(had)) {
          joined[key] = [...new Set([...had, ...value].map((each) => JSON.stringify(each)))]
            .map((each) => JSON.parse(each));
        } else if (typeof value === "object" && value !== null && !Array.isArray(value)) {
          joined[key] = { ...(had as object | undefined), ...value };
        } else {
          joined[key] = value;
        }
      }
    }
    return json(joined);
  }
  if (request.method !== "POST") return methodNotAllowed("GET, POST");
  const verifying = endpoint === "verify";
  if (!verifying && endpoint !== "settle") return notFound();
  const text = await request.clone().text();
  let named: unknown;
  try {
    named = (JSON.parse(text) as { paymentRequirements?: { network?: unknown } })
      ?.paymentRequirements?.network;
  } catch {
    named = undefined;
  }
  if (typeof named !== "string") return unsettled(verifying, INVALID_PAYLOAD, "", 400);
  const eip155 = /^eip155:([1-9]\d{0,14})$/.exec(named);
  const chainId = eip155 === null ? null : Number(eip155[1]);
  if (chainId === null || !served(env).has(chainId)) {
    return unsettled(verifying, INVALID_NETWORK, named, 200);
  }
  return await to(chainId);
}

/** A browser's preflight: GET and POST, with a JSON body, from anywhere. */
function preflight(): Response {
  return new Response(null, {
    status: 204,
    headers: {
      "access-control-allow-methods": "GET, POST",
      "access-control-allow-headers": "content-type, payment-signature",
      "access-control-max-age": "86400",
    },
  });
}

/** What a test configures in place of the configuration, FLOAT_KEY and the bindings bundlers' identity
 * keys name. */
interface TestSetup {
  readonly config: unknown;
  readonly floatKey: Hex;
  readonly payeeKey?: Hex;
  readonly identities?: Readonly<Record<string, Hex>>;
}

/** The chains the configuration names, each with its payee: a chain without a valid payee
 * isn't served (it couldn't open), and none is if the configuration doesn't parse. */
function served(env: Env): Map<number, Address> {
  const payees = new Map<number, Address>();
  try {
    const chains = (JSON.parse(CONFIG) as { chains?: Record<string, unknown> }).chains ?? {};
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

/** A chain's payee: its own, else PAYEE, else the configuration's top-level payee; null if
 * none. */
function payeeOf(env: Env, entry: object): string | null {
  const own = (entry as { payee?: unknown }).payee;
  if (typeof own === "string") return own;
  if (typeof env.PAYEE === "string" && env.PAYEE !== "") return env.PAYEE;
  try {
    const top = (JSON.parse(CONFIG) as { payee?: unknown }).payee;
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
      relay = this.open(chainId);
      if (rest === "/__treasury") {
        return json({ ran: await this.treasuryIfDue(relay) });
      }
      if (rest === FACILITATOR_OWN || rest.startsWith(`${FACILITATOR_OWN}/`)) {
        return await facilitate(relay, request, rest.slice(FACILITATOR_OWN.length));
      }
    } catch (e) {
      if (!(e instanceof ConfigError)) throw e;
      return misconfigured(e, chain as string);
    }
    return await handle(relay, request, rest);
  }

  /** A submission taken arms the alarms, from now: its batch closes at once. An alarm only
   * waiting to forget finished submissions is replaced, as the Python relay replaces its idle
   * clock: left, it would hold the submission until then. Armed as it's taken, not once its
   * request is answered: an x402 request is held until the alarms have landed it. */
  private async arm(): Promise<void> {
    if (
      this.ticking() &&
      (this.waitingToForget() || (await this.state.storage.getAlarm()) === null)
    ) {
      this.store.delete(WAITING_TO_FORGET);
      this.store.delete(WAITING_TO_FORGET_BEFORE_060);
      await this.state.storage.setAlarm(Millis.unix());
    }
  }

  /**
   * While there's work, each slot's block and each batch as it falls due, then the next alarm.
   * With none, the finished submissions due to be forgotten, then an alarm only for the next of
   * those: no reads of the chain, and no alarm at all once there's nothing left.
   */
  async alarm(): Promise<void> {
    let next: Millis | null = null;
    let relay: Relay | null = null;
    try {
      relay = this.open(Number(this.store.get("chainId")));
      relay.queue.prune();
      if (relay.busy) {
        next = await relay.step();
      }
      this.store.delete("failures");
    } catch (e) {
      if (!(e instanceof ConfigError)) {
        const failures = Number(this.store.get("failures") ?? 0);
        this.store.put("failures", String(failures + 1));
        next = Millis(
          Millis.unix() + millisOf(Seconds(Math.min(RETRY * 2 ** failures, MAX_RETRY))),
        );
      } // else its chain isn't configured: nothing to run
      console.error("alarm:", e);
    } finally {
      // decided with nothing awaited since checking: a submission taken meanwhile is either seen
      // here, or arms the alarm itself after this
      const idle = this.ticking() && relay !== null && !relay.busy;
      if (!this.ticking()) {
        next = null;
      } else if (relay !== null && !relay.busy) {
        next = relay.queue.forgetAt();
      }
      // whether the alarm is only for forgetting: a submission taken replaces it
      if (idle && next !== null) {
        this.store.put(WAITING_TO_FORGET, "1");
      } else {
        this.store.delete(WAITING_TO_FORGET);
        this.store.delete(WAITING_TO_FORGET_BEFORE_060);
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
  private front(chainId: number): Omit<ChainShown, "payee" | "forwarder"> {
    const last = storedSnapshot(this.store);
    const snapshot = last === null ? null : treasuryEntry(last);
    const stored = this.store.get("accounts");
    const parsed = stored === undefined ? null : statusFromJson(stored);
    const status = parsed === null ? null : accountsEntry(parsed);
    // what's wrong, as the relay last measured: stored, so read without waking the relay
    const problems = [
      ...JSON.parse(this.store.get("problems") ?? "[]") as string[],
      ...JSON.parse(this.store.get("internalErrors") ?? "[]") as string[],
    ];
    try {
      const config = chainConfig(this.chainEntry(chainId));
      const setup = this.testSetup();
      const floatAccount = keyBinding(
        setup === null ? this.env.FLOAT_KEY : setup.floatKey,
        "FLOAT_KEY",
      );
      const accounts = Array.from(
        { length: config.accounts },
        (_, i) => derive(floatAccount, i).address,
      );
      const x402Tokens = config.tokens.filter((t) => t.eip3009 !== null).map((t) => t.address);
      const x402MinFee = x402Tokens.length === 0 || config.minFee === null
        ? null
        : config.minFee.text;
      return {
        floatAccount: floatAccount.address,
        accounts,
        status,
        snapshot,
        x402Tokens,
        x402MinFee,
        problems,
      };
    } catch (e) {
      if (!(e instanceof ConfigError)) throw e;
      return {
        floatAccount: null,
        accounts: [],
        status,
        snapshot,
        x402Tokens: [],
        x402MinFee: null,
        problems,
      };
    }
  }

  /** The treasury's run, if it has one and its interval is up: whether it ran. Due again an
   * interval after one succeeds, unless it approved tokens: then at the next cron, to swap them.
   * One that fails is tried at the next cron. */
  private async treasuryIfDue(relay: Relay): Promise<boolean> {
    const settings = relay.config.treasury;
    if (this.treasury === null || settings === null) return false;
    if (Millis.unix() < Number(this.store.get("treasuryDue") ?? 0)) {
      // not due: the accounts read again while awake, so the root's are an hour old at most
      const latest = await relay.measuring.latest();
      const measured = await relay.measuring.measurements();
      relay.measuring.recordAccounts(
        await survey(
          relay.chain,
          relay.floatAccount,
          relay.config.accounts,
          latest.number,
          relay.payee,
          relay.forwarder,
          payeeTokens(measured),
        ),
      );
      return false;
    }
    if (!(await this.treasury.run()).approving) {
      const interval = millisOf(Seconds(settings.intervalSeconds));
      this.store.put("treasuryDue", String(Millis.unix() + interval));
    }
    return true;
  }

  private async testHook(chainId: number, hook: string, request: Request): Promise<Response> {
    if (hook === "/configure") {
      await this.state.storage.deleteAlarm();
      this.store.delete("ticking");
      this.state.storage.sql.exec("DELETE FROM kv WHERE key != 'schema'");
      this.store.put("test", await request.text());
      this.relay = null;
      this.treasury = null;
      this.open(chainId);
      return json({ ok: true });
    }
    const relay = this.open(chainId);
    if (hook === "/start") {
      this.store.put("ticking", "1");
      await this.state.storage.setAlarm(Millis.unix());
      return json({ ok: true });
    }
    if (hook === "/stop") {
      this.store.delete("ticking");
      this.store.delete(WAITING_TO_FORGET);
      this.store.delete(WAITING_TO_FORGET_BEFORE_060);
      await this.state.storage.deleteAlarm();
      return json({ ok: true });
    }
    if (hook === "/treasury") {
      if (this.treasury === null) {
        return json({ error: { code: -32601, message: "no treasury" } }, 404);
      }
      try {
        return json({ outcome: (await this.treasury.run()).outcome });
      } catch (e) {
        return json({ error: errorObject(e) }, 500); // what a failed run fails with
      }
    }
    if (hook === "/clock") {
      return json(await this.clock(relay));
    }
    if (hook === "/close-batch") {
      await relay.batches.closeBatch(await relay.measuring.latest());
      return json({ ok: true });
    }
    if (hook === "/latest") { // the chain's latest block, read now
      const latest = await relay.chain.latest();
      return json({
        number: latest.number,
        timestamp: latest.timestamp,
        nextBaseFee: hexQuantity(latest.nextBaseFee),
        tips: latest.tips.map(hexQuantity),
      });
    }
    if (hook === "/on-block") {
      await relay.batches.onBlock(await relay.measuring.latest()); // the latest block, as an alarm would
      return json({ ok: true, fundingInFlight: relay.batches.fundingInFlight });
    }
    return json({ error: { code: -32601, message: "no such hook" } }, 404);
  }

  /** Whether the alarm is only for forgetting finished submissions: a submission taken replaces
   * it. Stored under its 0.5.4 name too while an object of then may still hold it. */
  private waitingToForget(): boolean {
    return this.store.get(WAITING_TO_FORGET) === "1" ||
      this.store.get(WAITING_TO_FORGET_BEFORE_060) === "1";
  }

  /** Its own clock, as it stands (bruno/relay.py's Relay.clock). */
  private async clock(relay: Relay): Promise<ClockState> {
    return {
      busy: relay.busy,
      waitingToForget: this.waitingToForget(),
      set: (await this.state.storage.getAlarm()) !== null,
    };
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
    const chains = (JSON.parse(CONFIG) as { chains?: Record<string, unknown> }).chains;
    const entry = chains?.[String(chainId)];
    if (typeof entry !== "object" || entry === null) {
      throw new ConfigError("not-configured", `chains.${chainId}`, "not configured");
    }
    const payee = payeeOf(this.env, entry);
    if (payee === null) {
      throw new ConfigError(
        "no-payee",
        "payee",
        `chain ${chainId}: none (its own, the configuration's, or PAYEE)`,
      );
    }
    return { ...entry, payee };
  }

  /** The chain's relay, made once: it reads nothing until it's needed. */
  private open(chainId: number): Relay {
    return this.relay ?? this.load(chainId);
  }

  private load(chainId: number): Relay {
    // nothing read: the relay measures the chain when it's first needed
    const config = chainConfig(this.chainEntry(chainId));
    this.store.put("chainId", String(chainId));
    const setup = this.testSetup();
    const floatAccount = keyBinding(
      setup === null ? this.env.FLOAT_KEY : setup.floatKey,
      "FLOAT_KEY",
    );
    const builders: Builder[] = config.bundlers.map((b) => ({
      url: b.url,
      mode: b.mode,
      // without an identity key of its own, the float signs
      identity: b.identityKey === null
        ? floatAccount
        : keyBinding(setup?.identities?.[b.identityKey] ?? this.env[b.identityKey], b.identityKey),
    }));
    const chain = new Chain(chainId, config.nodes, Seconds(config.slotSeconds), config.blockDelay);
    const prices = new Prices(
      chain,
      config.weth,
      new Map(config.tokens.map((t) => [t.address, t.price])),
      Seconds(config.priceTtlSeconds),
    );
    this.relay = new Relay(chain, config, floatAccount, prices, builders, this.store);
    this.relay.onTaken = () => this.arm();
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

/** A chain whose configuration is the operator's mistake: 500, Internal error, saying what's
 * wrong and where (`{reason, field}`), never retried by a client; a chain not configured at all
 * isn't served, so not found, `chain` as the path names it. */
function misconfigured(e: ConfigError, chain: string): Response {
  if (e.reason === "not-configured") {
    return errorResponse(new NotServedError(chain));
  }
  const data = { reason: e.reason, field: e.field };
  return json({ error: { code: -32603, message: e.message, data } }, 500);
}

/** The signer for the private key bound as `name`: a ConfigError naming the binding, never
 * showing its value, if it holds no key. */
function keyBinding(value: unknown, name: string): Signer {
  if (typeof value !== "string" || !/^0x[0-9a-fA-F]{64}$/.test(value)) {
    throw new ConfigError(
      "unbound-key",
      name,
      "no private key bound (32 bytes, as 0x-prefixed hex)",
    );
  }
  try {
    return signer(bytes(value as Hex));
  } catch {
    throw new ConfigError("invalid-key", name, "not a secp256k1 private key");
  }
}
