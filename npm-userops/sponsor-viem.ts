/**
 * Sends a token from an account holding no ETH, as the account's own transaction, sponsored by a
 * Bruno relay. The relay sends the account the ETH its transactions need, sends them as signed,
 * and is paid in the same token by one more transfer the account signs, all in one bundle.
 *
 *   npm install @mailam/bruno viem
 *   node sponsor-viem.ts
 *
 * Set BRUNO_URL to the relay (https://bruno.mailam.app), CHAIN to the chain's name or id
 * (sepolia), PRIVATE_KEY to the account's key, TOKEN to a token the relay takes, by alias (USDC)
 * or address, RECIPIENT, AMOUNT in the token's smallest unit, and MAX, the most the account pays
 * the relay, in the token's smallest unit.
 */

import { Bruno, type Hex, viemTransactionSigner } from "@mailam/bruno";
import { encodeFunctionData, erc20Abi } from "viem";
import { privateKeyToAccount } from "viem/accounts";

function env(name: string): string {
  const value = process.env[name];
  if (value === undefined || value === "") throw new Error(`${name} isn't set`);
  return value;
}

const bruno = new Bruno({ url: env("BRUNO_URL"), chain: env("CHAIN") });
// a local account: it signs a transaction without sending it
const account = privateKeyToAccount(env("PRIVATE_KEY") as Hex);
const token = bruno.token(env("TOKEN"));

// each call is one transaction of the account's own, in order: here, one transfer
const calls = [{
  to: token.address,
  data: encodeFunctionData({
    abi: erc20Abi,
    functionName: "transfer",
    args: [env("RECIPIENT") as Hex, BigInt(env("AMOUNT"))],
  }),
}];

// quoted, checked to be these calls and a payment of the price, signed, and submitted; refused
// with nothing signed if the price is over MAX
const { id, paid } = await bruno.sponsor(calls, viemTransactionSigner(account), {
  token: token.address,
  max: { baseUnits: BigInt(env("MAX")) },
});

// included: the relay's first transaction, the account's two, and the relay's last
const status = await bruno.wait(id, "confirming");
console.log(JSON.stringify({
  account: account.address,
  status: status.status,
  reason: status.reason,
  transactions: status.transactionHashes,
  paid: paid.baseUnits.toString(),
}));
if (status.status !== "included") process.exit(1);
