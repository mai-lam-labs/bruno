/**
 * Sends a token from an EOA through a Bruno relay, as an EIP-7702 Kernel account (ZeroDev's
 * Kernel v3.3, EntryPoint v0.7), with ZeroDev's Kernel account client, the relay its bundler
 * transport and no ZeroDev project ID: the user operation pays for its gas in ETH, at the gas
 * price the relay answers. The EOA's first operation brings its authorization, delegating it to
 * Kernel, which the client signs; later ones need none.
 *
 *   npm install viem @zerodev/sdk
 *   node userop-zerodev-7702.ts
 *
 * Set BRUNO_URL to a chain's endpoint (such as https://bruno.mailam.app/sepolia), RPC_URL to a
 * node on that chain, PRIVATE_KEY to the EOA's key, TOKEN to the token to send, RECIPIENT, and
 * AMOUNT in the token's smallest unit.
 */

import {
  createPublicClient,
  defineChain,
  encodeFunctionData,
  erc20Abi,
  type Hex,
  http,
} from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { createKernelAccount, createKernelAccountClient } from "@zerodev/sdk";
import { getEntryPoint, KERNEL_V3_3 } from "@zerodev/sdk/constants";

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

const account = await createKernelAccount(client, {
  eip7702Account: privateKeyToAccount(env("PRIVATE_KEY") as Hex),
  entryPoint: getEntryPoint("0.7"),
  kernelVersion: KERNEL_V3_3,
});

const held = await client.getBalance({ address: account.address });
if (held === 0n) {
  console.error(`${account.address} holds no ETH: send it some, then run again`);
  process.exit(1);
}

// the relay is the bundler
const kernel = createKernelAccountClient({
  account,
  chain,
  client,
  bundlerTransport: http(relay),
});
const hash = await kernel.sendUserOperation({
  calls: [{
    to: token,
    data: encodeFunctionData({
      abi: erc20Abi,
      functionName: "transfer",
      args: [recipient, amount],
    }),
  }],
});
const receipt = await kernel.waitForUserOperationReceipt({ hash });
console.log(JSON.stringify({
  hash,
  account: account.address,
  success: receipt.success,
  transactionHash: receipt.receipt.transactionHash,
  paid: receipt.actualGasCost.toString(),
}));
process.exit(receipt.success ? 0 : 1);
