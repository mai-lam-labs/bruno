/**
 * Sends a token from an EOA through a Bruno relay, as an EIP-7702 smart account
 * (permissionless.js's to7702SimpleSmartAccount, EntryPoint v0.8), with its smart account client
 * set up as for any bundler, the relay's URL its only setting: the user operation pays for its
 * gas in ETH, at the fees viem gives it. The EOA's first operation brings its authorization,
 * delegating it to Simple7702Account; later ones need none.
 *
 *   npm install viem permissionless
 *   node userop-permissionless-7702.ts
 *
 * Set BRUNO_URL to a chain's endpoint (such as https://bruno.mailam.app/sepolia), RPC_URL to a
 * node on that chain, PRIVATE_KEY to the EOA's key, TOKEN to the token to send, RECIPIENT, and
 * AMOUNT in the token's smallest unit.
 */

import {
  createPublicClient,
  createWalletClient,
  defineChain,
  encodeFunctionData,
  erc20Abi,
  type Hex,
  http,
} from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { to7702SimpleSmartAccount } from "permissionless/accounts";
import { createSmartAccountClient } from "permissionless";

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
const account = await to7702SimpleSmartAccount({ client, owner });

const held = await client.getBalance({ address: account.address });
if (held === 0n) {
  console.error(`${account.address} holds no ETH: send it some, then run again`);
  process.exit(1);
}

// the EOA's authorization of Simple7702Account, until it's delegated
const authorization = await account.isDeployed() ? undefined : await createWalletClient({
  account: owner,
  chain,
  transport: http(rpc),
}).signAuthorization({ contractAddress: account.authorization.address });

// the relay is the bundler
const bundler = createSmartAccountClient({
  account,
  client,
  chain,
  bundlerTransport: http(relay),
});
const hash = await bundler.sendUserOperation({
  authorization,
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
