/**
 * The packages as npm, pnpm and Bun install them from their packed tarballs (`make npm-test`):
 * AgentKit's own registry and viem wallet provider, given the Bruno actions; a stand-in relay
 * and node; one USDC transfer sent and Bruno paid, each signature checked. Run as it is by Node
 * and Bun, and type-checked by tsc against the packages' generated declarations.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { AgentKit, ViemWalletProvider } from "@coinbase/agentkit";
import { Bruno, NoPayment, tokenOf } from "@mailam/bruno";
import { brunoActionProvider } from "@mailam/bruno-agentkit";
import {
  createWalletClient,
  decodeFunctionData,
  type Hex,
  http,
  keccak256,
  parseAbi,
  recoverTypedDataAddress,
  toHex,
} from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { sepolia } from "viem/chains";

/** Whether `T` is `any`: what a declaration that failed to resolve becomes, unreported under
 * skipLibCheck. */
type IsAny<T> = 0 extends 1 & T ? true : false;
const typed: [
  IsAny<ReturnType<typeof brunoActionProvider>>,
  IsAny<Parameters<ReturnType<typeof brunoActionProvider>["getActions"]>[0]>,
  IsAny<ReturnType<ReturnType<typeof brunoActionProvider>["getActions"]>[number]>,
  IsAny<Bruno>,
  IsAny<Bruno["x402"]>,
  IsAny<ReturnType<typeof tokenOf>>,
] = [false, false, false, false, false, false];
assert.deepEqual(typed, [false, false, false, false, false, false]);

const account = privateKeyToAccount(keccak256(toHex("the npm tests' wallet")));
const USDC = tokenOf(sepolia.id, "USDC").address;
const PAYEE: Hex = "0xfBC0cF0d2C09C57222C871B1cbF9b153bfFA0049";
const RECIPIENT: Hex = "0x000000000000000000000000000000000000bEEF";
const TRANSACTION: Hex = `0x${"cd".repeat(32)}`;
const REQUIREMENT = {
  scheme: "exact",
  network: `eip155:${sepolia.id}`,
  amount: "250000",
  asset: USDC,
  payTo: PAYEE,
  maxTimeoutSeconds: 120,
  extra: { name: "USDC", version: "2" },
};
const DOMAIN = { name: "USDC", version: "2", chainId: sepolia.id, verifyingContract: USDC };
const TYPES = {
  TransferWithAuthorization: [
    { name: "from", type: "address" },
    { name: "to", type: "address" },
    { name: "value", type: "uint256" },
    { name: "validAfter", type: "uint256" },
    { name: "validBefore", type: "uint256" },
    { name: "nonce", type: "bytes32" },
  ],
} as const;

interface Sent {
  readonly url: string;
  readonly body: unknown;
  readonly payment: string | null;
}

/** A relay: 402 until paid, then settled; every request kept. */
function relay(sent: Sent[]): typeof fetch {
  return (input, init) => {
    const payment = new Headers(init?.headers).get("PAYMENT-SIGNATURE");
    sent.push({ url: String(input), body: JSON.parse(String(init?.body)), payment });
    if (payment === null) {
      const required = btoa(JSON.stringify({ x402Version: 2, accepts: [REQUIREMENT] }));
      return Promise.resolve(
        new Response("{}", { status: 402, headers: { "PAYMENT-REQUIRED": required } }),
      );
    }
    const settled = { success: true, transaction: TRANSACTION, network: REQUIREMENT.network };
    return Promise.resolve(
      new Response("{}", {
        status: 200,
        headers: { "PAYMENT-RESPONSE": btoa(JSON.stringify(settled)) },
      }),
    );
  };
}

/** A node answering what the actions ask of one: a call's gas, and the chain. */
async function node(): Promise<{ url: string; close: () => void }> {
  const server = createServer((request, response) => {
    let text = "";
    request.on("data", (chunk) => (text += chunk));
    request.on("end", () => {
      const { id, method } = JSON.parse(text) as { id: number; method: string };
      const results: Record<string, string> = {
        eth_estimateGas: "0xea60",
        eth_chainId: toHex(sepolia.id),
      };
      const result = results[method];
      response.setHeader("content-type", "application/json");
      response.end(
        JSON.stringify(
          result === undefined
            ? { jsonrpc: "2.0", id, error: { code: -32601, message: `no ${method} here` } }
            : { jsonrpc: "2.0", id, result },
        ),
      );
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as AddressInfo;
  return { url: `http://127.0.0.1:${port}`, close: () => server.close() };
}

/** A package's package.json as installed. */
function manifest(name: string): { dependencies?: Record<string, string> } {
  return JSON.parse(readFileSync(`node_modules/${name}/package.json`, "utf8"));
}

// the packages' own dependencies: none for the client; AgentKit and zod for the provider
assert.deepEqual(Object.keys(manifest("@mailam/bruno").dependencies ?? {}), []);
assert.deepEqual(Object.keys(manifest("@mailam/bruno-agentkit").dependencies ?? {}).sort(), [
  "@coinbase/agentkit",
  "zod",
]);

// the client alone: its errors are Errors, carrying their fields
const none = new NoPayment(["USDC: not offered"]);
assert.ok(none instanceof Error);
assert.deepEqual(none.reasons, ["USDC: not offered"]);
assert.equal(new Bruno({ chain: "sepolia" }).chainId, sepolia.id);

const rpc = await node();

// what's fetched beyond the node and the relay given: AgentKit's analytics, answered here (left
// to fail, AgentKit leaves its rejection unhandled). Its wallet providers report themselves on
// creation, the wallet's address among it; its per-action events are its decorator's, which
// the actions don't use
const ANALYTICS = "https://cca-lite.coinbase.com/amp";
const reported: string[] = [];
const nodeFetch = globalThis.fetch;
globalThis.fetch = (input, init) => {
  const url = String(input);
  if (url.startsWith(rpc.url)) return nodeFetch(input, init);
  if (url === ANALYTICS) {
    const { e } = JSON.parse(String(init?.body)) as { e: string };
    for (const event of JSON.parse(e) as { event_type: string }[]) reported.push(event.event_type);
    return Promise.resolve(new Response("{}"));
  }
  return Promise.reject(new Error(`fetched ${url}`));
};

try {
  const walletClient = createWalletClient({ account, chain: sepolia, transport: http(rpc.url) });
  const walletProvider = new ViemWalletProvider(walletClient, { rpcUrl: rpc.url });
  const sent: Sent[] = [];
  const agentkit = await AgentKit.from({
    walletProvider,
    actionProviders: [brunoActionProvider({ url: "https://relay.test", fetch: relay(sent) })],
  });
  const actions = agentkit.getActions();
  assert.deepEqual(actions.map((a) => a.name).sort(), [
    "BrunoActionProvider_get_gasless_price",
    "BrunoActionProvider_send_call_gasless",
    "BrunoActionProvider_send_usdc_gasless",
  ]);
  const action = (name: string) => actions.find((a) => a.name.endsWith(name))!;

  // as an agent framework calls it: the arguments through the action's schema
  const sendUsdc = action("send_usdc_gasless");
  const said = await sendUsdc.invoke(sendUsdc.schema.parse({ recipient: RECIPIENT, amount: "2.5" }));
  assert.match(said, new RegExp(`${TRANSACTION}.*paying Bruno 0\\.25 USDC`));
  assert.ok(sent.every((s) => s.url.startsWith("https://relay.test/sepolia/v1/x402")));

  // the paid call: USDC's transferWithAuthorization of 2.5 USDC to the recipient, signed by the
  // wallet
  const paid = sent.find((s) => s.payment !== null)!;
  const { call } = paid.body as { call: { to: Hex; data: Hex } };
  assert.equal(call.to, USDC);
  const { args } = decodeFunctionData({
    abi: parseAbi([
      "function transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,uint8,bytes32,bytes32)",
    ]),
    data: call.data,
  });
  const [from, to, value, validAfter, validBefore, nonce, v, r, s] = args;
  assert.deepEqual([from, to, value], [account.address, RECIPIENT, 2_500_000n]);
  const signer = await recoverTypedDataAddress({
    domain: DOMAIN,
    types: TYPES,
    primaryType: "TransferWithAuthorization",
    message: { from, to, value, validAfter, validBefore, nonce },
    signature: `${r}${s.slice(2)}${v.toString(16)}` as Hex,
  });
  assert.equal(signer, account.address);

  // Bruno's payment: another transfer, from the wallet to the payee, of the price
  const payment = JSON.parse(atob(paid.payment!));
  const a = payment.payload.authorization;
  assert.deepEqual([a.from, a.to, a.value], [account.address, PAYEE, "250000"]);
  const payer = await recoverTypedDataAddress({
    domain: DOMAIN,
    types: TYPES,
    primaryType: "TransferWithAuthorization",
    message: {
      ...a,
      value: BigInt(a.value),
      validAfter: BigInt(a.validAfter),
      validBefore: BigInt(a.validBefore),
    },
    signature: payment.payload.signature,
  });
  assert.equal(payer, account.address);

  // a price over maxFee: refused, nothing signed for Bruno
  const before = sent.length;
  const refused = await sendUsdc.invoke(
    sendUsdc.schema.parse({ recipient: RECIPIENT, amount: "1", maxFee: "0.1" }),
  );
  assert.match(refused, /failed/);
  assert.equal(sent.slice(before).filter((s) => s.payment !== null).length, 0);

  const price = action("get_gasless_price");
  assert.match(await price.invoke(price.schema.parse({ to: RECIPIENT, data: "0x" })), /0\.25 USDC/);
} finally {
  rpc.close();
}
assert.deepEqual(reported, ["agent_initialization"]);
console.log("ok: the packed packages under", process.versions.bun ? `Bun ${process.versions.bun}` : `Node ${process.version}`);
