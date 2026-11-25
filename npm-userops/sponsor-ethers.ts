/**
 * Sends a token from an account holding no ETH, as the account's own transaction, sponsored by a
 * Bruno relay, with an ethers Wallet signing. The relay sends the account the ETH its
 * transactions need, sends them as signed, and is paid in the same token by one more transfer
 * the account signs, all in one bundle.
 *
 *   npm install @mailam/bruno ethers
 *   node sponsor-ethers.ts
 *
 * Set BRUNO_URL to the relay (https://bruno.mailam.app), CHAIN to the chain's name or id
 * (sepolia), PRIVATE_KEY to the account's key, TOKEN to a token the relay takes, by alias (USDC)
 * or address, RECIPIENT, AMOUNT in the token's smallest unit, and MAX, the most the account pays
 * the relay, in the token's smallest unit.
 */

import { Bruno, type Hex, type TransactionSigner } from "@mailam/bruno";
import { Interface, Wallet } from "ethers";

function env(name: string): string {
  const value = process.env[name];
  if (value === undefined || value === "") throw new Error(`${name} isn't set`);
  return value;
}

const bruno = new Bruno({ url: env("BRUNO_URL"), chain: env("CHAIN") });
// a Wallet with no provider: it signs a transaction and hands it back, and can't send one
const wallet = new Wallet(env("PRIVATE_KEY"));

// all the client asks of a signer: its address, and a transaction signed without being sent
const signer: TransactionSigner = {
  address: wallet.address as Hex,
  signTransaction: async (transaction) =>
    await wallet.signTransaction({
      type: 2,
      chainId: transaction.chainId,
      nonce: transaction.nonce,
      to: transaction.to ?? null,
      data: transaction.data,
      value: transaction.value,
      gasLimit: transaction.gas,
      maxFeePerGas: transaction.maxFeePerGas,
      maxPriorityFeePerGas: transaction.maxPriorityFeePerGas,
    }) as Hex,
};

const token = bruno.token(env("TOKEN"));
const erc20 = new Interface(["function transfer(address to, uint256 amount)"]);

// each call is one transaction of the account's own, in order: here, one transfer
const calls = [{
  to: token.address,
  data: erc20.encodeFunctionData("transfer", [env("RECIPIENT"), BigInt(env("AMOUNT"))]) as Hex,
}];

// quoted, checked to be these calls and a payment of the price, signed, and submitted; refused
// with nothing signed if the price is over MAX
const { id, paid } = await bruno.sponsor(calls, signer, {
  token: token.address,
  max: { baseUnits: BigInt(env("MAX")) },
});

// included: the relay's first transaction, the account's two, and the relay's last
const status = await bruno.wait(id, "confirming");
console.log(JSON.stringify({
  account: wallet.address,
  status: status.status,
  reason: status.reason,
  transactions: status.transactionHashes,
  paid: paid.baseUnits.toString(),
}));
if (status.status !== "included") process.exit(1);
