/**
 * Sends a token from an EOA through a Bruno relay, as Alchemy's EIP-7702 smart account (Modular
 * Account v2 in 7702 mode, EntryPoint v0.7), with viem's bundler client set up as for any
 * bundler, the relay's URL its only setting and no Alchemy API key: the user operation pays for
 * its gas in ETH, at the fees viem gives it. The EOA's first operation brings its authorization,
 * delegating it to Alchemy's SemiModularAccount7702; later ones need none.
 *
 *   npm install viem @alchemy/smart-accounts
 *   node userop-alchemy-7702.ts
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
import { createBundlerClient } from "viem/account-abstraction";
import { toModularAccountV2 } from "@alchemy/smart-accounts";

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
const account = await toModularAccountV2({ client, owner, mode: "7702" });

const held = await client.getBalance({ address: account.address });
if (held === 0n) {
  console.error(`${account.address} holds no ETH: send it some, then run again`);
  process.exit(1);
}

// the EOA's authorization of SemiModularAccount7702, until it's delegated: viem only stubs it,
// to estimate the operation
const delegate = account.authorization?.address;
if (delegate === undefined) throw new Error("not an EIP-7702 account");
const authorization = await account.isDeployed() ? undefined : await createWalletClient({
  account: owner,
  chain,
  transport: http(rpc),
}).signAuthorization({ contractAddress: delegate });

// the relay is the bundler
const bundler = createBundlerClient({ account, client, chain, transport: http(relay) });
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
