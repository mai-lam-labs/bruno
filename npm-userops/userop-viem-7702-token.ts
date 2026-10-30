/**
 * Sends a token from an EOA holding no ETH through a Bruno relay, as an EIP-7702 smart account
 * (viem's toSimple7702SmartAccount, EntryPoint v0.8), with viem's bundler client. The relay pays
 * the gas, and the user operation pays the relay in the same token, in its last call, the amount
 * the relay quotes for it before it's signed. The EOA's first operation brings its authorization,
 * delegating it to Simple7702Account; later ones need none.
 *
 *   npm install viem
 *   node userop-viem-7702-token.ts
 *
 * Set BRUNO_URL to a chain's endpoint (such as https://bruno.mailam.app/sepolia), RPC_URL to a
 * node on that chain, PRIVATE_KEY to the EOA's key, TOKEN to a token the relay takes (listed by
 * its /v1/config), RECIPIENT, and AMOUNT in the token's smallest unit.
 */

import {
  type Address,
  createClient,
  createPublicClient,
  createWalletClient,
  defineChain,
  encodeFunctionData,
  erc20Abi,
  type Hex,
  http,
  rpcSchema,
} from "viem";
import { privateKeyToAccount } from "viem/accounts";
import {
  createBundlerClient,
  formatUserOperationRequest,
  type RpcUserOperation,
  toSimple7702SmartAccount,
} from "viem/account-abstraction";

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

// the relay's price for an operation paying by its calls, asked before it's signed
const quoting = createClient({
  transport: http(relay),
  rpcSchema: rpcSchema<[{
    Method: "postpaid_quoteUserOperation";
    Parameters: [RpcUserOperation, Address];
    ReturnType: { validUntilBlock: Hex; quotes: { token: Address; amount: Hex }[] };
  }]>(),
});

const transfer = (to: Hex, value: bigint) => ({
  to: token,
  data: encodeFunctionData({ abi: erc20Abi, functionName: "transfer", args: [to, value] }),
});
// its last call pays the relay
const calls = (paying: bigint) => [transfer(recipient, amount), transfer(payee, paying)];
// the relay pays the gas: the operation's fees are zero
const fees = { maxFeePerGas: 0n, maxPriorityFeePerGas: 0n };

// the operation paying a placeholder of one unit, prepared (not signed), and quoted
const draft = await bundler.prepareUserOperation({ authorization, calls: calls(1n), ...fees });
const { quotes } = await quoting.request({
  method: "postpaid_quoteUserOperation",
  params: [formatUserOperationRequest(draft), account.entryPoint.address],
});
const quote = quotes.find((q) => q.token.toLowerCase() === token.toLowerCase());
if (quote === undefined) throw new Error(`the relay doesn't take ${token}`);
const paying = BigInt(quote.amount);

// sent paying what's quoted, signed once
const hash = await bundler.sendUserOperation({ authorization, calls: calls(paying), ...fees });
const receipt = await bundler.waitForUserOperationReceipt({ hash });
console.log(JSON.stringify({
  hash,
  account: account.address,
  success: receipt.success,
  transactionHash: receipt.receipt.transactionHash,
  paid: paying.toString(),
}));
process.exit(receipt.success ? 0 : 1);
