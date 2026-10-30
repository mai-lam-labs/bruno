/**
 * Sends USDC from an EOA holding only USDC, no ETH, through a Bruno relay, as an EIP-7702 smart
 * account (viem's toSimple7702SmartAccount, EntryPoint v0.8), its gas paid in USDC through
 * Circle's paymaster: Circle's quickstart (developers.circle.com/paymaster/pay-gas-fees-usdc),
 * the relay its bundler. The paymaster takes USDC by an EIP-2612 permit in its data, and pays
 * the relay in ETH from its deposit in the EntryPoint. The EOA's first operation brings its
 * authorization, delegating it to Simple7702Account; later ones need none.
 *
 *   npm install viem
 *   node userop-viem-circle.ts
 *
 * Set BRUNO_URL to a chain's endpoint (such as https://bruno.mailam.app/sepolia), RPC_URL to a
 * node on that chain, PRIVATE_KEY to the EOA's key, USDC to USDC's address on that chain,
 * PAYMASTER to Circle's paymaster for EntryPoint v0.8 there (0x3BA9A96eE3eFf3A69E2B18886AcF52027EFF8966
 * on Sepolia, 0x0578cFB241215b77442a541325d6A4E6dFE700Ec on Ethereum), RECIPIENT, and AMOUNT
 * in USDC's smallest unit.
 */

import {
  createClient,
  createPublicClient,
  createWalletClient,
  defineChain,
  encodeFunctionData,
  encodePacked,
  erc20Abi,
  getContract,
  type Hex,
  hexToBigInt,
  http,
  maxUint256,
  parseErc6492Signature,
  rpcSchema,
} from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { createBundlerClient, toSimple7702SmartAccount } from "viem/account-abstraction";

function env(name: string): string {
  const value = process.env[name];
  if (value === undefined || value === "") throw new Error(`${name} isn't set`);
  return value;
}

const relay = env("BRUNO_URL");
const rpc = env("RPC_URL");
const usdc = env("USDC") as Hex;
const paymasterAddress = env("PAYMASTER") as Hex;
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
  address: usdc,
  abi: erc20Abi,
  functionName: "balanceOf",
  args: [account.address],
});
if (held <= amount) {
  console.error(`${account.address} holds ${held}: send it more than ${amount} USDC, then run again`);
  process.exit(1);
}

// Circle's permit, as its quickstart signs it
const eip2612Abi = [
  ...erc20Abi,
  {
    inputs: [{ internalType: "address", name: "owner", type: "address" }],
    stateMutability: "view",
    type: "function",
    name: "nonces",
    outputs: [{ internalType: "uint256", name: "", type: "uint256" }],
  },
  {
    inputs: [],
    name: "version",
    outputs: [{ internalType: "string", name: "", type: "string" }],
    stateMutability: "view",
    type: "function",
  },
] as const;

async function signPermit(value: bigint): Promise<Hex> {
  const token = getContract({ client, address: usdc, abi: eip2612Abi });
  const permitData = {
    types: {
      Permit: [
        { name: "owner", type: "address" },
        { name: "spender", type: "address" },
        { name: "value", type: "uint256" },
        { name: "nonce", type: "uint256" },
        { name: "deadline", type: "uint256" },
      ],
    },
    primaryType: "Permit",
    domain: {
      name: await token.read.name(),
      version: await token.read.version(),
      chainId: chain.id,
      verifyingContract: usdc,
    },
    message: {
      owner: account.address,
      spender: paymasterAddress,
      value,
      nonce: await token.read.nonces([account.address]),
      // the paymaster can't read the block's time, so the deadline is the most there is
      deadline: maxUint256,
    },
  } as const;
  const { signature } = parseErc6492Signature(await account.signTypedData(permitData));
  return signature;
}

// Circle's paymaster: its data a permit for it to take what the account can spare (its prefund is
// the op's whole maxCost in USDC, refunded but its cost; Circle's quickstart permits 10 USDC, which
// a first op on Glamsterdam can be over), its postOp limit as Circle gives it (its least). Circle's quickstart also fixes its validation's at 200,000, too little
// where EIP-8037 prices a new storage slot (Glamsterdam) for an account's first permit: the
// relay's estimate measures it
const paymaster = {
  async getPaymasterData() {
    const permitAmount = held - amount;
    const paymasterData = encodePacked(
      ["uint8", "address", "uint256", "bytes"],
      [0, usdc, permitAmount, await signPermit(permitAmount)],
    );
    return {
      paymaster: paymasterAddress,
      paymasterData,
      paymasterPostOpGasLimit: 35_000n,
      isFinal: true,
    };
  },
};

// the relay is the bundler; its gas price asked as Circle's quickstart asks Pimlico's
type GasPrice = { maxFeePerGas: Hex; maxPriorityFeePerGas: Hex };
const prices = createClient({
  transport: http(relay),
  rpcSchema: rpcSchema<[{
    Method: "pimlico_getUserOperationGasPrice";
    Parameters?: undefined;
    ReturnType: { slow: GasPrice; standard: GasPrice; fast: GasPrice };
  }]>(),
});
const bundler = createBundlerClient({
  account,
  client,
  chain,
  paymaster,
  userOperation: {
    async estimateFeesPerGas() {
      const { standard: fees } = await prices.request({
        method: "pimlico_getUserOperationGasPrice",
      });
      return {
        maxFeePerGas: hexToBigInt(fees.maxFeePerGas),
        maxPriorityFeePerGas: hexToBigInt(fees.maxPriorityFeePerGas),
      };
    },
  },
  transport: http(relay),
});

// the EOA's authorization of Simple7702Account, until it's delegated
const authorization = await account.isDeployed() ? undefined : await createWalletClient({
  account: owner,
  chain,
  transport: http(rpc),
}).signAuthorization({ contractAddress: account.authorization.address });

const hash = await bundler.sendUserOperation({
  authorization,
  calls: [{
    to: usdc,
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
