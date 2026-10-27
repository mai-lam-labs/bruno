/**
 * An AgentKit agent as an npm user runs one, against a relay and node it's given (the local
 * chain's, from bruno/tests/test_agentkit.py): AgentKit's own registry and viem wallet provider,
 * the Bruno actions given the chain and its USDC. Its wallet is a key, or with SMART set to "1"
 * or "1.1" a Coinbase Smart Wallet of that version the key owns (viem's toCoinbaseSmartAccount),
 * deployed or not: undeployed, its first payment deploys it. It sends USDC holding no ETH, paying Bruno, and prints what the action
 * said, as JSON.
 */

import { AgentKit, ViemWalletProvider } from "@coinbase/agentkit";
import { brunoActionProvider } from "@mailam/bruno-agentkit";
import { createPublicClient, createWalletClient, defineChain, type Hex, http } from "viem";
import { type LocalAccount, privateKeyToAccount, toAccount } from "viem/accounts";
import { toCoinbaseSmartAccount } from "viem/account-abstraction";

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
const key = privateKeyToAccount(env("KEY") as Hex);

/** The wallet: the key, or the Coinbase Smart Wallet it owns, as an account AgentKit's viem
 * provider takes (one that signs typed data as the smart account does: ERC-6492-wrapped while
 * it's undeployed). */
async function wallet(): Promise<LocalAccount> {
  const version = process.env.SMART;
  if (version === undefined || version === "") return key;
  if (version !== "1" && version !== "1.1") throw new Error(`SMART: "1" or "1.1", not ${version}`);
  const client = createPublicClient({ chain, transport: http(rpc) });
  // the version named: each its own factory (1's 0x0BA5ED0c…, 1.1's 0xBA5ED110…), so its own
  // account for the same owner
  const smart = await toCoinbaseSmartAccount({ client, owners: [key], version });
  return toAccount({
    address: smart.address,
    signMessage: (parameters) => smart.signMessage(parameters),
    signTypedData: (parameters) => smart.signTypedData(parameters),
    signTransaction: () => {
      throw new Error("a smart account signs no transactions");
    },
  });
}

const walletClient = createWalletClient({ account: await wallet(), chain, transport: http(rpc) });
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
