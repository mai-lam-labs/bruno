/**
 * Sends a token from a ZeroDev Kernel account through a Bruno relay, with viem's bundler client,
 * set up as for any bundler, the relay's URL its only setting: the user operation pays for its gas
 * in ETH, at the fees viem gives it, the relay's estimate charging what makes up its price. The
 * account can be undeployed: the operation deploys it, paying from the ETH it holds.
 *
 *   npm install viem @zerodev/sdk @zerodev/ecdsa-validator tslib
 *   (tslib because @zerodev/ecdsa-validator loads it without depending on it: zerodevapp/sdk#281)
 *   node userop-viem-eth.ts
 *
 * Set BRUNO_URL to a chain's endpoint (such as https://bruno.mailam.app/sepolia), RPC_URL to a
 * node on that chain, PRIVATE_KEY to the account owner's key, TOKEN to the token to send,
 * RECIPIENT, and AMOUNT in the token's smallest unit.
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
import { createBundlerClient } from "viem/account-abstraction";
import { signerToEcdsaValidator } from "@zerodev/ecdsa-validator";
import { createKernelAccount } from "@zerodev/sdk";
import { getEntryPoint, KERNEL_V3_2 } from "@zerodev/sdk/constants";

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

const entryPoint = getEntryPoint("0.7");
const account = await createKernelAccount(client, {
  entryPoint,
  kernelVersion: KERNEL_V3_2,
  plugins: {
    sudo: await signerToEcdsaValidator(client, {
      signer: privateKeyToAccount(env("PRIVATE_KEY") as Hex),
      entryPoint,
      kernelVersion: KERNEL_V3_2,
    }),
  },
});

const held = await client.getBalance({ address: account.address });
if (held === 0n) {
  console.error(`${account.address} holds no ETH: send it some, then run again`);
  process.exit(1);
}

// the relay is the bundler
const bundler = createBundlerClient({ account, client, chain, transport: http(relay) });
const hash = await bundler.sendUserOperation({
  calls: [{
    to: token,
    data: encodeFunctionData({
      abi: erc20Abi,
      functionName: "transfer",
      args: [recipient, amount],
    }),
  }],
});
const receipt = await bundler.waitForUserOperationReceipt({ hash });
console.log(JSON.stringify({
  hash,
  account: account.address,
  success: receipt.success,
  transactionHash: receipt.receipt.transactionHash,
  paid: receipt.actualGasCost.toString(),
}));
process.exit(receipt.success ? 0 : 1);
