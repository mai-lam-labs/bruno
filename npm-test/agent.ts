/**
 * An AgentKit agent as an npm user runs one, against a relay and node it's given (the local
 * chain's, from bruno/tests/test_agentkit.py): AgentKit's own registry and viem wallet provider,
 * the Bruno actions given the chain and its USDC. It sends USDC holding no ETH, paying Bruno,
 * and prints what the action said, as JSON.
 */

import { AgentKit, ViemWalletProvider } from "@coinbase/agentkit";
import { brunoActionProvider } from "@mailam/bruno-agentkit";
import { createWalletClient, defineChain, type Hex, http } from "viem";
import { privateKeyToAccount } from "viem/accounts";

function env(name: string): string {
  const value = process.env[name];
  if (value === undefined || value === "") throw new Error(`${name} isn't set`);
  return value;
}

const relay = env("BRUNO_URL");
const rpc = env("RPC_URL");
const chainId = Number(env("CHAIN_ID"));
const usdc = env("USDC") as Hex;

// AgentKit's analytics (its wallet providers report themselves, the wallet's address among it):
// answered here, the test's wallets not sent to Coinbase
const nodeFetch = globalThis.fetch;
globalThis.fetch = (input, init) =>
  String(input).startsWith("https://cca-lite.coinbase.com/")
    ? Promise.resolve(new Response("{}"))
    : nodeFetch(input, init);

const chain = defineChain({
  id: chainId,
  name: "local",
  nativeCurrency: { name: "Ether", symbol: "ETH", decimals: 18 },
  rpcUrls: { default: { http: [rpc] } },
});
const walletClient = createWalletClient({
  account: privateKeyToAccount(env("KEY") as Hex),
  chain,
  transport: http(rpc),
});
const agentkit = await AgentKit.from({
  walletProvider: new ViemWalletProvider(walletClient, { rpcUrl: rpc }),
  actionProviders: [
    brunoActionProvider({
      url: relay,
      chains: { [String(chainId)]: [{ symbol: "USDC", address: usdc, decimals: 6 }] },
    }),
  ],
});
const action = agentkit.getActions().find((a) => a.name.endsWith("send_usdc_gasless"));
if (action === undefined) throw new Error("no send_usdc_gasless among AgentKit's actions");
const said = await action.invoke(
  action.schema.parse({
    recipient: env("RECIPIENT"),
    amount: env("AMOUNT"),
    maxFee: env("MAX_FEE"),
  }),
);
console.log(JSON.stringify({ said }));
if (said.includes("failed")) process.exit(1);
