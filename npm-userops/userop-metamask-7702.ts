/**
 * Sends a token from an EOA through a Bruno relay, as MetaMask's EIP-7702 smart account (the
 * Smart Accounts Kit's Stateless7702, EntryPoint v0.7), with viem's bundler client set up as for
 * any bundler, the relay's URL its only setting: the user operation pays for its gas in ETH, at
 * the fees viem gives it. The EOA's first operation brings its authorization, delegating it to
 * MetaMask's EIP7702StatelessDeleGator; later ones need none.
 *
 *   npm install viem @metamask/smart-accounts-kit
 *   DO_NOT_TRACK=1 node userop-metamask-7702.ts
 *
 * Set BRUNO_URL to a chain's endpoint (such as https://bruno.mailam.app/sepolia), RPC_URL to a
 * node on that chain, PRIVATE_KEY to the EOA's key, TOKEN to the token to send, RECIPIENT, and
 * AMOUNT in the token's smallest unit. The kit reports its use to MetaMask unless DO_NOT_TRACK
 * (or CI) is set.
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
import {
  getSmartAccountsEnvironment,
  Implementation,
  toMetaMaskSmartAccount,
} from "@metamask/smart-accounts-kit";

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

// MetaMask's contracts, by Ethereum's chain id: at the same addresses on Sepolia (the kit
// refuses a chain it doesn't list)
const environment = getSmartAccountsEnvironment(1);
const delegate = environment.implementations.EIP7702StatelessDeleGatorImpl as Hex;
const metaMask = await toMetaMaskSmartAccount({
  client,
  implementation: Implementation.Stateless7702,
  address: owner.address,
  signer: { account: owner },
  environment,
});
// an EIP-7702 account has no factory: until the EOA is delegated, the kit refuses to name one,
// and its first operation brings the authorization instead
const account = { ...metaMask, getFactoryArgs: async () => ({}) };

const held = await client.getBalance({ address: account.address });
if (held === 0n) {
  console.error(`${account.address} holds no ETH: send it some, then run again`);
  process.exit(1);
}

// the EOA's authorization of EIP7702StatelessDeleGator, until it's delegated
const code = await client.getCode({ address: owner.address });
const delegated = code?.toLowerCase() === `0xef0100${delegate.slice(2)}`.toLowerCase();
const authorization = delegated ? undefined : await createWalletClient({
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
