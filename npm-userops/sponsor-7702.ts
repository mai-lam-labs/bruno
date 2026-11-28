/**
 * Turns a plain key holding a token and no ETH into a smart account and makes its first calls,
 * in one sponsored bundle. The relay's first transaction carries the key's EIP-7702
 * authorization and makes one call for it, into the account's new code, whose batch sends the
 * token and pays the relay. The account sends no transaction of its own, and is sent no ETH. The
 * delegate is Uniswap's Calibur, and the batch is Calibur's signed batch: another delegate needs
 * its own.
 *
 *   npm install @mailam/bruno viem
 *   node sponsor-7702.ts
 *
 * Set BRUNO_URL to the relay (https://bruno.mailam.app), CHAIN to the chain's name or id
 * (sepolia), RPC_URL to a node of that chain (the account's nonce is read from it), PRIVATE_KEY to
 * the account's key, TOKEN to a token the relay takes, by alias (USDC) or address, RECIPIENT,
 * AMOUNT in the token's smallest unit, and MAX, the most the account pays the relay, in the
 * token's smallest unit.
 */

import { type Authorization, Bruno, type Hex } from "@mailam/bruno";
import {
  createPublicClient,
  encodeAbiParameters,
  encodeFunctionData,
  erc20Abi,
  http,
  pad,
  parseAbi,
  toHex,
  zeroAddress,
  zeroHash,
} from "viem";
import { privateKeyToAccount } from "viem/accounts";

// Calibur, at the address it has on every chain it's deployed to
const CALIBUR = "0x000000005c84F8Fd50b21CAC312528A64437030e";

function env(name: string): string {
  const value = process.env[name];
  if (value === undefined || value === "") throw new Error(`${name} isn't set`);
  return value;
}

const bruno = new Bruno({ url: env("BRUNO_URL"), chain: env("CHAIN") });
const node = createPublicClient({ transport: http(env("RPC_URL")) });
const account = privateKeyToAccount(env("PRIVATE_KEY") as Hex);
const token = bruno.token(env("TOKEN"));
const most = BigInt(env("MAX"));

// the node is the relay's chain's: a nonce read from another would sign a useless authorization
if (await node.getChainId() !== bruno.chainId) {
  throw new Error(`RPC_URL isn't a node of chain ${bruno.chainId}`);
}

// the delegation: signed at the account's current nonce, since the relay's transaction carries
// it, not one of the account's own
const signed = await account.signAuthorization({
  address: CALIBUR,
  chainId: bruno.chainId,
  nonce: await node.getTransactionCount({ address: account.address }),
});
const authorization: Authorization = {
  chainId: toHex(signed.chainId),
  address: signed.address,
  nonce: toHex(signed.nonce),
  yParity: toHex(signed.yParity ?? 0),
  r: signed.r,
  s: signed.s,
};

// Calibur's execute(SignedBatchedCall, wrappedSignature), and the EIP-712 types its batch is
// signed as
const calibur = parseAbi([
  "struct Call { address to; uint256 value; bytes data; }",
  "struct BatchedCall { Call[] calls; bool revertOnFailure; }",
  "struct SignedBatchedCall { BatchedCall batchedCall; uint256 nonce; bytes32 keyHash; address executor; uint256 deadline; }",
  "function execute(SignedBatchedCall signedBatchedCall, bytes wrappedSignature) payable",
]);
const types = {
  SignedBatchedCall: [
    { name: "batchedCall", type: "BatchedCall" },
    { name: "nonce", type: "uint256" },
    { name: "keyHash", type: "bytes32" },
    { name: "executor", type: "address" },
    { name: "deadline", type: "uint256" },
  ],
  BatchedCall: [
    { name: "calls", type: "Call[]" },
    { name: "revertOnFailure", type: "bool" },
  ],
  Call: [
    { name: "to", type: "address" },
    { name: "value", type: "uint256" },
    { name: "data", type: "bytes" },
  ],
} as const;

interface Call {
  readonly to: Hex;
  readonly value: bigint;
  readonly data: Hex;
}

/** The call into the account that runs `calls` as one batch, all or nothing, signed by the
 * account's own key: Calibur's first batch for it (nonce 0), with no executor and no deadline. */
async function batch(calls: readonly Call[]): Promise<{ to: Hex; data: Hex }> {
  const signedBatchedCall = {
    batchedCall: { calls, revertOnFailure: true },
    nonce: 0n,
    keyHash: zeroHash, // the account's own key
    executor: zeroAddress,
    deadline: 0n,
  };
  const signature = await account.signTypedData({
    // Calibur's domain is the account's own, salted with the delegate's address
    domain: {
      name: "Calibur",
      version: "1.0.0",
      chainId: bruno.chainId,
      verifyingContract: account.address,
      salt: pad(CALIBUR),
    },
    types,
    primaryType: "SignedBatchedCall",
    message: signedBatchedCall,
  });
  const wrapped = encodeAbiParameters([{ type: "bytes" }, { type: "bytes" }], [signature, "0x"]);
  return {
    to: account.address,
    data: encodeFunctionData({
      abi: calibur,
      functionName: "execute",
      args: [signedBatchedCall, wrapped],
    }),
  };
}

function transfer(to: Hex, amount: bigint): Call {
  return {
    to: token.address,
    value: 0n,
    data: encodeFunctionData({ abi: erc20Abi, functionName: "transfer", args: [to, amount] }),
  };
}

// what the account does, then its payment to the relay's forwarder, in one batch
const { forwarder } = await bruno.terms();
const paying = (amount: bigint) =>
  batch([
    transfer(env("RECIPIENT") as Hex, BigInt(env("AMOUNT"))),
    transfer(forwarder, amount),
  ]);

// quoted with a batch paying one base unit, then the batch is signed again paying the price
const quote = await bruno.quoteSponsorship(account.address, [], [token.address], {
  authorizationList: [authorization],
  calls: [await paying(1n)],
});
const price = quote.prices[0];
if (price === undefined) throw new Error("the quote has no price");
if (price.baseUnits > most) throw new Error(`the price, ${price.baseUnits}, is over MAX`);
if (quote.forwarder !== forwarder) throw new Error("the quote names another forwarder");
// the gas the relay gives the call into the account's code, as it estimated it
const priced = quote.calls[0];
if (priced === undefined) throw new Error("the quote has no gas for the call");

const id = await bruno.submitSponsorship({
  account: account.address,
  authorizationList: [authorization],
  calls: [{ ...await paying(price.baseUnits), gas: priced.gas }],
  token: token.address,
  amount: { baseUnits: price.baseUnits },
  forwarder,
  maxBlockNumber: quote.maxBlockNumber,
});

// included: the relay's two transactions, the first delegating the account and running its batch
const status = await bruno.wait(id, "confirming");
console.log(JSON.stringify({
  account: account.address,
  status: status.status,
  reason: status.reason,
  transactions: status.transactionHashes,
  paid: price.baseUnits.toString(),
}));
if (status.status !== "included") process.exit(1);
