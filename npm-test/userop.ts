/**
 * A user operation sent through a Bruno relay as viem's own bundler client sends one, against
 * the relay and node it's given (the local chain's, from bruno/tests/test_viem_bundler.py): the
 * relay's chain endpoint its bundler transport, the client's node its chain; a Kernel the key
 * owns its account (kernel.ts), undeployed until its op deploys it; the op's fees zero, its last
 * call paying the relay's payee in USDC. Paying a unit first, it signs again at what the
 * relay says is required, until it's taken; then waits for its receipt, and prints it, as JSON.
 */

import {
  BaseError,
  createPublicClient,
  defineChain,
  encodeFunctionData,
  erc20Abi,
  type Hex,
  http,
} from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { createBundlerClient } from "viem/account-abstraction";
import { kernelAccount } from "./kernel.ts";

function env(name: string): string {
  const value = process.env[name];
  if (value === undefined || value === "") throw new Error(`${name} isn't set`);
  return value;
}

const relay = env("BRUNO_URL");
const rpc = env("RPC_URL");
const chainId = Number(env("CHAIN_ID"));
const usdc = env("USDC") as Hex;
const recipient = env("RECIPIENT") as Hex;
const sent = BigInt(env("AMOUNT"));
// how often it signs again at the price the relay asks, at most
const SIGNINGS = 4;

const chain = defineChain({
  id: chainId,
  name: "local",
  nativeCurrency: { name: "Ether", symbol: "ETH", decimals: 18 },
  rpcUrls: { default: { http: [rpc] } },
});
const client = createPublicClient({ chain, transport: http(rpc) });
const account = await kernelAccount(rpc, chain, privateKeyToAccount(env("KEY") as Hex));
const bundler = createBundlerClient({
  account,
  client,
  chain,
  transport: http(`${relay}/${chainId}`),
  // the relay pays the gas: the op's fees are zero, and its own calls pay the relay
  userOperation: { estimateFeesPerGas: async () => ({ maxFeePerGas: 0n, maxPriorityFeePerGas: 0n }) },
});
const terms = await (await fetch(`${relay}/${chainId}/v1/config`)).json() as { payee: Hex };

/** What the relay said is required, if `e` is its underpaid answer. */
function required(e: unknown): bigint | null {
  if (!(e instanceof BaseError)) return null;
  const found = e.walk((c) => {
    const data = (c as { data?: { required?: unknown } }).data;
    return typeof data?.required === "string";
  }) as { data: { required: string } } | null;
  return found === null ? null : BigInt(found.data.required);
}

const transfer = (to: Hex, amount: bigint) => ({
  to: usdc,
  data: encodeFunctionData({ abi: erc20Abi, functionName: "transfer", args: [to, amount] }),
});
let paying = 1n;
for (let signing = 0; signing < SIGNINGS; signing++) {
  try {
    const hash = await bundler.sendUserOperation({
      calls: [transfer(recipient, sent), transfer(terms.payee, paying)],
    });
    const receipt = await bundler.waitForUserOperationReceipt({ hash, pollingInterval: 500 });
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
throw new Error(`not taken in ${SIGNINGS} signings`);
