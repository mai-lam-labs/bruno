/**
 * Sends a token from an EOA holding no ETH through a Bruno relay, as an EIP-7702 smart account
 * (viem's toSimple7702SmartAccount, EntryPoint v0.8), with viem's bundler client. The relay pays
 * the gas, and the user operation pays the relay in the same token, in its last call. The EOA's
 * first operation brings its authorization, delegating it to Simple7702Account; later ones need
 * none.
 *
 *   npm install viem
 *   node userop-viem-7702-token.ts
 *
 * Set BRUNO_URL to a chain's endpoint (such as https://bruno.mailam.app/sepolia), RPC_URL to a
 * node on that chain, PRIVATE_KEY to the EOA's key, TOKEN to a token the relay takes (listed by
 * its /v1/config), RECIPIENT, and AMOUNT in the token's smallest unit.
 */

import {
  BaseError,
  createPublicClient,
  createWalletClient,
  defineChain,
  encodeFunctionData,
  erc20Abi,
  type Hex,
  http,
} from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { createBundlerClient, toSimple7702SmartAccount } from "viem/account-abstraction";

function env(name: string): string {
  const value = process.env[name];
  if (value === undefined || value === "") throw new Error(`${name} isn't set`);
  return value;
}

const relay = env("BRUNO_URL");
const rpc = env("RPC_URL");
const token = env("TOKEN") as Hex;
const recipient = env("RECIPIENT") as Hex;
const amount = BigInt(env("AMOUNT"));

const chain = defineChain({
  id: await createPublicClient({ transport: http(rpc) }).getChainId(),
  name: "chain",
  nativeCurrency: { name: "Ether", symbol: "ETH", decimals: 18 },
  rpcUrls: { default: { http: [rpc] } },
});
const client = createPublicClient({ chain, transport: http(rpc) });

const owner = privateKeyToAccount(env("PRIVATE_KEY") as Hex);
const account = await toSimple7702SmartAccount({ client, owner });

const held = await client.readContract({
  address: token,
  abi: erc20Abi,
  functionName: "balanceOf",
  args: [account.address],
});
if (held <= amount) {
  console.error(`${account.address} holds ${held}: send it more than ${amount}, then run again`);
  process.exit(1);
}

// the EOA's authorization of Simple7702Account, until it's delegated
const authorization = await account.isDeployed() ? undefined : await createWalletClient({
  account: owner,
  chain,
  transport: http(rpc),
}).signAuthorization({ contractAddress: account.authorization.address });

// the relay is the bundler
const bundler = createBundlerClient({ account, client, chain, transport: http(relay) });
const { payee } = await (await fetch(`${relay}/v1/config`)).json() as { payee: Hex };

const transfer = (to: Hex, value: bigint) => ({
  to: token,
  data: encodeFunctionData({ abi: erc20Abi, functionName: "transfer", args: [to, value] }),
});

/** What the relay said is required, if `e` is its underpaid error. */
function required(e: unknown): bigint | null {
  if (!(e instanceof BaseError)) return null;
  const found = e.walk((c) => {
    const data = (c as { data?: { required?: unknown } }).data;
    return typeof data?.required === "string";
  }) as { data: { required: string } } | null;
  return found === null ? null : BigInt(found.data.required);
}

// The first try pays one unit, so the relay answers that it's underpaid, with its price; the
// next pays that. The price can rise in between, so it tries a few times.
let paying = 1n;
for (let attempt = 0; attempt < 4; attempt++) {
  try {
    const hash = await bundler.sendUserOperation({
      authorization,
      calls: [transfer(recipient, amount), transfer(payee, paying)],
      // the relay pays the gas: the operation's last call pays the relay
      maxFeePerGas: 0n,
      maxPriorityFeePerGas: 0n,
    });
    const receipt = await bundler.waitForUserOperationReceipt({ hash });
    console.log(JSON.stringify({
      hash,
      account: account.address,
      success: receipt.success,
      transactionHash: receipt.receipt.transactionHash,
      paid: paying.toString(),
    }));
    process.exit(receipt.success ? 0 : 1);
  } catch (e) {
    const asked = required(e);
    if (asked === null) throw e;
    paying = asked;
  }
}
throw new Error("the relay's price kept rising");
