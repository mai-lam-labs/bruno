/**
 * The Worker deployed through Cloudflare's API, with an account's API token (make
 * worker-deploy, worker-secret, worker-domain; DEPLOYMENT.md, Cloudflare). Run with network
 * access to api.cloudflare.com only, and read access to exactly the files it's given.
 *
 *   deploy --credentials=DIR --name=NAME --bundle=FILE --config=FILE --compat=DATE [--payee=0x…]
 *   secret --credentials=DIR --name=NAME --key=FILE [--binding=FLOAT_KEY|PAYEE_KEY]
 *   domain --credentials=DIR --name=NAME --domain=HOST
 *
 * DIR holds account_id and api_token. `deploy` creates the Worker, or replaces its code and
 * bindings, keeping its secrets, and sets its hourly cron (the treasuries); the configuration
 * is checked as the Worker reads it first.
 */

import { chainConfig } from "./src/config.ts";
import { bytes, checksum, type Hex } from "./src/evm.ts";
import { signer } from "./src/transactions.ts";

const API = "https://api.cloudflare.com/client/v4";
/** The migration that creates the relay's SQLite-backed Durable Object class. */
const MIGRATION = "v1";
const CLASS = "ChainRelay";
/** The cron that runs each chain's treasury when its interval is up. */
const CRON = "0 * * * *";
/** The secrets `secret` sets: the float's key, and the payee's (the treasuries'). */
const KEY_BINDINGS = ["FLOAT_KEY", "PAYEE_KEY"];

interface Cloudflare {
  readonly account: string;
  readonly call: (method: string, path: string, body?: BodyInit, type?: string) => Promise<unknown>;
}

async function cloudflare(dir: string): Promise<Cloudflare> {
  const account = (await Deno.readTextFile(`${dir}/account_id`)).trim();
  const token = (await Deno.readTextFile(`${dir}/api_token`)).trim();
  return {
    account,
    call: async (method, path, body, type) => {
      const headers: Record<string, string> = { authorization: `Bearer ${token}` };
      if (type !== undefined) headers["content-type"] = type;
      const init: RequestInit = { method, headers };
      if (body !== undefined) init.body = body;
      const response = await fetch(`${API}${path}`, init);
      const answer = (await response.json()) as {
        success: boolean;
        errors?: { code: number; message: string }[];
        result: unknown;
      };
      if (!answer.success) {
        const errors = (answer.errors ?? []).map((e) => `${e.code}: ${e.message}`).join("; ");
        throw new Error(`${method} ${path.replace(account, "<account>")}: ${errors}`);
      }
      return answer.result;
    },
  };
}

function options(args: readonly string[]): Map<string, string> {
  return new Map(args.map((arg) => {
    const [key, ...value] = arg.replace(/^--/, "").split("=");
    return [key as string, value.join("=")];
  }));
}

function required(o: Map<string, string>, name: string): string {
  const value = o.get(name);
  if (value === undefined || value === "") throw new Error(`--${name} is required`);
  return value;
}

/** The Worker, created or replaced: its code, CONFIG, PAYEE and Durable Object; secrets kept. */
async function deploy(cf: Cloudflare, o: Map<string, string>): Promise<void> {
  const name = required(o, "name");
  const config = await Deno.readTextFile(required(o, "config"));
  const chains = (JSON.parse(config) as { chains?: Record<string, unknown> }).chains ?? {};
  for (const [chainId, entry] of Object.entries(chains)) {
    const payee = (entry as { payee?: unknown }).payee ?? o.get("payee") ??
      (JSON.parse(config) as { payee?: unknown }).payee;
    chainConfig({ ...(entry as object), payee }); // as the Worker will read it: throws if not
    console.log(`chain ${chainId}: checked`);
  }
  const payee = o.get("payee");
  const scripts = (await cf.call("GET", `/accounts/${cf.account}/workers/scripts`)) as {
    id: string;
    migration_tag?: string;
  }[];
  const tag = scripts.find((s) => s.id === name)?.migration_tag;
  if (tag !== undefined && tag !== MIGRATION) {
    throw new Error(`${name}'s migration tag is ${tag}, not ${MIGRATION}`);
  }
  const metadata = {
    main_module: "worker.js",
    compatibility_date: required(o, "compat"),
    bindings: [
      { type: "durable_object_namespace", name: "RELAY", class_name: CLASS },
      { type: "plain_text", name: "CONFIG", text: config },
      ...(payee === undefined || payee === ""
        ? []
        : [{ type: "plain_text", name: "PAYEE", text: checksum(payee) }]),
    ],
    keep_bindings: ["secret_text"],
    // the class's storage made once, the first time
    ...(tag === MIGRATION
      ? {}
      : { migrations: { new_tag: MIGRATION, new_sqlite_classes: [CLASS] } }),
  };
  const form = new FormData();
  form.append("metadata", new Blob([JSON.stringify(metadata)], { type: "application/json" }));
  form.append(
    "worker.js",
    new Blob([await Deno.readFile(required(o, "bundle"))], {
      type: "application/javascript+module",
    }),
    "worker.js",
  );
  await cf.call("PUT", `/accounts/${cf.account}/workers/scripts/${name}`, form);
  console.log(`${name}: deployed${tag === undefined ? ", its Durable Object class created" : ""}`);
  await cf.call(
    "PUT",
    `/accounts/${cf.account}/workers/scripts/${name}/schedules`,
    JSON.stringify([{ cron: CRON }]),
    "application/json",
  );
  console.log(`${name}: cron ${CRON}`);
}

/** A key's secret (FLOAT_KEY unless --binding names PAYEE_KEY) set from a key file (hex, 0x or
 * not), the key never shown: its address is. */
async function secret(cf: Cloudflare, o: Map<string, string>): Promise<void> {
  const name = required(o, "name");
  const binding = o.get("binding") ?? "FLOAT_KEY";
  if (!KEY_BINDINGS.includes(binding)) {
    throw new Error(`--binding is one of ${KEY_BINDINGS.join(", ")}, not ${binding}`);
  }
  const hex = (await Deno.readTextFile(required(o, "key"))).trim().replace(/^0x/, "");
  if (!/^[0-9a-fA-F]{64}$/.test(hex)) throw new Error("the key file holds no private key");
  const key = `0x${hex}` as Hex;
  await cf.call(
    "PUT",
    `/accounts/${cf.account}/workers/scripts/${name}/secrets`,
    JSON.stringify({ name: binding, text: key, type: "secret_text" }),
    "application/json",
  );
  console.log(`${name}: ${binding} set, for ${signer(bytes(key)).address}`);
}

/** The hostname routed to the Worker, in whichever of the account's zones holds it. */
async function domain(cf: Cloudflare, o: Map<string, string>): Promise<void> {
  const name = required(o, "name");
  const hostname = required(o, "domain");
  const labels = hostname.split(".");
  for (let i = 0; i < labels.length - 1; i++) {
    const zoneName = labels.slice(i).join(".");
    const zones = (await cf.call(
      "GET",
      `/zones?name=${encodeURIComponent(zoneName)}&account.id=${cf.account}`,
    )) as { id: string; name: string }[];
    const zone = zones[0];
    if (zone === undefined) continue;
    await cf.call(
      "PUT",
      `/accounts/${cf.account}/workers/domains`,
      JSON.stringify({ hostname, service: name, zone_id: zone.id, zone_name: zone.name }),
      "application/json",
    );
    console.log(`https://${hostname} → ${name} (zone ${zone.name})`);
    return;
  }
  throw new Error(`no zone of the account holds ${hostname}`);
}

const [command, ...args] = Deno.args;
const o = options(args);
const commands: Record<string, (cf: Cloudflare, o: Map<string, string>) => Promise<void>> = {
  deploy,
  secret,
  domain,
};
const run = commands[command ?? ""];
if (run === undefined) {
  console.error("deploy.ts deploy|secret|domain --credentials=DIR --name=NAME …");
  Deno.exit(2);
}
await run(await cloudflare(required(o, "credentials")), o);
