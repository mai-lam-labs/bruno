/**
 * An AgentKit agent as an npm user runs one, against a relay and node it's given (the local
 * chain's, from bruno/tests/test_agentkit.py): AgentKit's own registry and viem wallet provider,
 * the Bruno actions given the chain and its USDC. Its wallet is a key; or, with SMART set, a
 * smart account the key owns, deployed or not (undeployed, its first payment deploys it): "1" or
 * "1.1" a Coinbase Smart Wallet of that version (viem's toCoinbaseSmartAccount), "kernel"
 * ZeroDev's Kernel v3.2 as AgentKit's ZeroDevWalletProvider makes it (@zerodev/sdk, the same
 * options; that provider takes its chain from AgentKit's table of public networks, so isn't run
 * on a local chain itself). Or, with WALLET_PROVIDER "zerodev" and NETWORK_ID one of AgentKit's
 * networks (the live Sepolia test), AgentKit's own ZeroDevWalletProvider, the key its signer:
 * its project ID only names ZeroDev's bundler, which signing never calls. It sends USDC
 * holding no ETH, paying Bruno, and prints what the action said, as JSON.
 */

import {
  AgentKit,
  type EvmWalletProvider,
  ViemWalletProvider,
  ZeroDevWalletProvider,
} from "@coinbase/agentkit";
import { brunoActionProvider } from "@mailam/bruno-agentkit";
import { createPublicClient, createWalletClient, defineChain, type Hex, http } from "viem";
import { type LocalAccount, privateKeyToAccount, toAccount } from "viem/accounts";
import { toCoinbaseSmartAccount } from "viem/account-abstraction";
import { createKernelAccount } from "@zerodev/sdk";
import { getEntryPoint, KERNEL_V3_2 } from "@zerodev/sdk/constants";
import { signerToEcdsaValidator } from "@zerodev/ecdsa-validator";
import { installIntentExecutor, INTENT_V0_3 } from "@zerodev/intent";

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

/** The Kernel account the key owns, made as AgentKit's ZeroDevWalletProvider.configureWithWallet
 * makes it: Kernel v3.2, ZeroDev's ECDSA validator its sudo, EntryPoint v0.7, the intent
 * executor installed. */
async function kernelAccount() {
  const client = createPublicClient({ chain, transport: http(rpc) });
  const entryPoint = getEntryPoint("0.7");
  const sudo = await signerToEcdsaValidator(client, {
    signer: key,
    entryPoint,
    kernelVersion: KERNEL_V3_2,
  });
  return await createKernelAccount(client, {
    plugins: { sudo },
    entryPoint,
    kernelVersion: KERNEL_V3_2,
    initConfig: [installIntentExecutor(INTENT_V0_3)],
  });
}

/** The wallet: the key, or the smart account it owns, as an account AgentKit's viem provider
 * takes (one that signs typed data as the smart account does: ERC-6492-wrapped while it's
 * undeployed). */
async function wallet(): Promise<LocalAccount> {
  const version = process.env.SMART;
  if (version === undefined || version === "") return key;
  if (version !== "1" && version !== "1.1" && version !== "kernel") {
    throw new Error(`SMART: "1", "1.1" or "kernel", not ${version}`);
  }
  // Coinbase's version named: each its own factory (1's 0x0BA5ED0c…, 1.1's 0xBA5ED110…), so its
  // own account for the same owner
  const smart = version === "kernel" ? await kernelAccount() : await toCoinbaseSmartAccount({
    client: createPublicClient({ chain, transport: http(rpc) }),
    owners: [key],
    version,
  });
  return toAccount({
    address: smart.address,
    signMessage: (parameters) => smart.signMessage(parameters),
    signTypedData: (parameters) => smart.signTypedData(parameters),
    signTransaction: () => {
      throw new Error("a smart account signs no transactions");
    },
  });
}

// a ZeroDev project ID: it only names ZeroDev's bundler, for the provider's own transactions
const NO_PROJECT = "bruno-signs-only";

/** The wallet provider: AgentKit's ZeroDevWalletProvider on a network AgentKit knows, if asked;
 * else its viem wallet provider, with the wallet above. */
async function walletProvider(): Promise<EvmWalletProvider> {
  if (process.env.WALLET_PROVIDER === "zerodev") {
    return await ZeroDevWalletProvider.configureWithWallet({
      signer: key,
      projectId: NO_PROJECT,
      networkId: env("NETWORK_ID"),
      rpcUrl: rpc,
    });
  }
  const walletClient = createWalletClient({ account: await wallet(), chain, transport: http(rpc) });
  return new ViemWalletProvider(walletClient, { rpcUrl: rpc });
}

const provider = await walletProvider();
console.error(`wallet: ${provider.getAddress()}`);
const agentkit = await AgentKit.from({
  walletProvider: provider,
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
