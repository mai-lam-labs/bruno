/**
 * Bruno for Coinbase's AgentKit: an agent holding only USDC acts on Ethereum with no ETH. It
 * pays Bruno, the permissionless gas relay, over x402 from its own wallet, and only if its
 * transaction succeeds and lands.
 *
 * Bruno runs the paid call from its own one-shot contract, not from the agent's wallet: a call
 * whose effect depends on who sends it (a swap from the wallet, say) isn't one to send this way.
 * What is: the wallet's own signed USDC transfer (send_usdc_gasless), and any call that acts
 * for whoever it names.
 */

import { z } from "zod";
import { encodeFunctionData, erc20Abi, formatUnits, type Hex, parseUnits } from "viem";
// EvmWalletProvider stays a value import: the decorator metadata AgentKit's @CreateAction reads
// (design:paramtypes) names its runtime class, which a type-only import would erase
// deno-lint-ignore verbatim-module-syntax
import { ActionProvider, CreateAction, EvmWalletProvider, type Network } from "@coinbase/agentkit";
import {
  Bruno,
  freshNonce,
  type Requirement,
  type Transfer,
  typedData,
  viemSigner,
} from "@mailam/bruno";

/** The chains Bruno serves, by AgentKit's chain id, as Bruno's paths name them. */
export const CHAINS: Readonly<Record<string, string>> = { "1": "ethereum", "11155111": "sepolia" };
/** The most paid to Bruno for one call unless the agent says otherwise, in whole tokens. */
const DEFAULT_MAX_FEE = "5";
/** How long a USDC transfer the agent signs stays valid past the payment's own window: time
 * for Bruno to include it. */
const TRANSFER_SLACK_SECONDS = 600;
/** A call's gas over its estimate: an estimate can be a little under what a call needs. */
const GAS_HEADROOM_NUM = 5n;
const GAS_HEADROOM_DEN = 4n;

const TRANSFER_WITH_AUTHORIZATION = [{
  type: "function",
  name: "transferWithAuthorization",
  stateMutability: "nonpayable",
  inputs: [
    { name: "from", type: "address" },
    { name: "to", type: "address" },
    { name: "value", type: "uint256" },
    { name: "validAfter", type: "uint256" },
    { name: "validBefore", type: "uint256" },
    { name: "nonce", type: "bytes32" },
    { name: "v", type: "uint8" },
    { name: "r", type: "bytes32" },
    { name: "s", type: "bytes32" },
  ],
  outputs: [],
}] as const;

/** send_usdc_gasless's arguments. */
export interface SendUsdcArgs {
  recipient: string;
  amount: string;
  maxFee?: string | null | undefined;
}

/** send_call_gasless's arguments. */
export interface SendCallArgs {
  to: string;
  data: string;
  gas?: string | null | undefined;
  maxFee?: string | null | undefined;
}

/** get_gasless_price's arguments. */
export interface PriceArgs {
  to: string;
  data: string;
  gas?: string | null | undefined;
}

const SendUsdcSchema: z.ZodType<SendUsdcArgs> = z.object({
  recipient: z.string().describe("The address to send USDC to"),
  amount: z.string().describe("How much USDC to send, in whole units (e.g. '2.5')"),
  maxFee: z.string().nullable().optional().describe(
    `The most USDC to pay Bruno for sending it, in whole units (default ${DEFAULT_MAX_FEE})`,
  ),
}).describe("Send USDC from the wallet without ETH: Bruno pays the gas, paid in USDC");

const SendCallSchema: z.ZodType<SendCallArgs> = z.object({
  to: z.string().describe("The contract or account the call is to"),
  data: z.string().describe("The call's data, 0x-prefixed hex"),
  gas: z.string().nullable().optional().describe("The call's gas (estimated if not given)"),
  maxFee: z.string().nullable().optional().describe(
    `The most USDC to pay Bruno, in whole units (default ${DEFAULT_MAX_FEE})`,
  ),
}).describe("Send a call without ETH, paying Bruno in USDC: run from Bruno, not the wallet");

const PriceSchema: z.ZodType<PriceArgs> = z.object({
  to: z.string().describe("The contract or account the call is to"),
  data: z.string().describe("The call's data, 0x-prefixed hex"),
  gas: z.string().nullable().optional().describe("The call's gas (estimated if not given)"),
}).describe("What sending a call through Bruno costs, in USDC, without sending it");

export interface BrunoActionProviderConfig {
  /** Bruno's URL: its own deployment if not given. */
  readonly url?: string;
  /** `fetch`, if not the global one. */
  readonly fetch?: typeof fetch;
}

export class BrunoActionProvider extends ActionProvider<EvmWalletProvider> {
  readonly #config: BrunoActionProviderConfig;

  constructor(config: BrunoActionProviderConfig = {}) {
    super("bruno", []);
    this.#config = config;
  }

  @CreateAction({
    name: "send_usdc_gasless",
    description: `
Send USDC from this wallet to any address on Ethereum without holding ETH. Bruno, a
permissionless gas relay, sends the transfer and is paid in USDC from the same wallet, in the
same transaction: if the transfer fails or never lands, nothing is paid.

Inputs:
- recipient: the address to send to
- amount: how much USDC, in whole units (e.g. "2.5")
- maxFee: the most USDC to pay Bruno (default ${DEFAULT_MAX_FEE}); a higher price is refused

The wallet needs the amount plus Bruno's fee in USDC, and no ETH. Ethereum mainnet and Sepolia.
`,
    schema: SendUsdcSchema,
  })
  async sendUsdcGasless(
    walletProvider: EvmWalletProvider,
    args: SendUsdcArgs,
  ): Promise<string> {
    try {
      const bruno = this.bruno(walletProvider);
      const payer = walletProvider.getAddress() as Hex;
      // the relay's own token and its EIP-712 domain: from a price asked for any call
      const { requirement } = await bruno.price({ call: { to: payer, data: "0x" }, gas: 21_000 });
      const decimals = await this.decimals(walletProvider, requirement.asset);
      const value = parseUnits(args.amount, decimals);
      const call = await this.transferCall(
        walletProvider,
        requirement,
        payer,
        args.recipient as Hex,
        value,
      );
      const gas = await this.gas(walletProvider, call.to, call.data);
      const paid = await bruno.payAndSend({ call, gas }, viemSigner(walletProvider.toSigner()), {
        asset: requirement.asset,
        maxAmount: parseUnits(args.maxFee ?? DEFAULT_MAX_FEE, decimals),
      });
      return `Sent ${args.amount} USDC to ${args.recipient} in transaction ${paid.transaction}, ` +
        `paying Bruno ${formatUnits(paid.amount, decimals)} USDC for it. No ETH was used.`;
    } catch (e) {
      return `Sending USDC through Bruno failed: ${e instanceof Error ? e.message : String(e)}`;
    }
  }

  @CreateAction({
    name: "send_call_gasless",
    description: `
Send a call on Ethereum without holding ETH, paying Bruno (a permissionless gas relay) in USDC
from this wallet, only if the call succeeds and lands.

The call runs from Bruno's own one-shot contract, NOT from this wallet: use it only for calls
whose effect doesn't depend on who sends them (a claim to a named recipient, a signed
authorization, a mint for an address). A swap or transfer from this wallet must not be sent
this way: use send_usdc_gasless to move USDC.

Inputs: to, data (0x hex), gas (optional, estimated), maxFee (the most USDC to pay, default
${DEFAULT_MAX_FEE}). Ethereum mainnet and Sepolia.
`,
    schema: SendCallSchema,
  })
  async sendCallGasless(
    walletProvider: EvmWalletProvider,
    args: SendCallArgs,
  ): Promise<string> {
    try {
      const bruno = this.bruno(walletProvider);
      const call = { to: args.to as Hex, data: args.data as Hex };
      const gas = args.gas ? BigInt(args.gas) : await this.gas(walletProvider, call.to, call.data);
      const { requirement } = await bruno.price({ call, gas });
      const decimals = await this.decimals(walletProvider, requirement.asset);
      const paid = await bruno.payAndSend({ call, gas }, viemSigner(walletProvider.toSigner()), {
        asset: requirement.asset,
        maxAmount: parseUnits(args.maxFee ?? DEFAULT_MAX_FEE, decimals),
      });
      return `Sent the call in transaction ${paid.transaction}, paying Bruno ` +
        `${formatUnits(paid.amount, decimals)} USDC. No ETH was used.`;
    } catch (e) {
      return `Sending the call through Bruno failed: ${e instanceof Error ? e.message : String(e)}`;
    }
  }

  @CreateAction({
    name: "get_gasless_price",
    description: `
What sending a call on Ethereum through Bruno (a permissionless gas relay) costs, in USDC,
without sending it or paying anything. Inputs: to, data (0x hex), gas (optional, estimated).
`,
    schema: PriceSchema,
  })
  async getGaslessPrice(
    walletProvider: EvmWalletProvider,
    args: PriceArgs,
  ): Promise<string> {
    try {
      const bruno = this.bruno(walletProvider);
      const call = { to: args.to as Hex, data: args.data as Hex };
      const gas = args.gas ? BigInt(args.gas) : await this.gas(walletProvider, call.to, call.data);
      const { requirement } = await bruno.price({ call, gas });
      const decimals = await this.decimals(walletProvider, requirement.asset);
      return `Sending it through Bruno costs ${
        formatUnits(BigInt(requirement.amount), decimals)
      } ` +
        `USDC, paid only if it succeeds and lands (valid for about ` +
        `${requirement.maxTimeoutSeconds} seconds).`;
    } catch (e) {
      return `Asking Bruno's price failed: ${e instanceof Error ? e.message : String(e)}`;
    }
  }

  supportsNetwork(network: Network): boolean {
    return network.protocolFamily === "evm" && network.chainId !== undefined &&
      network.chainId in CHAINS;
  }

  private bruno(walletProvider: EvmWalletProvider): Bruno {
    const chain = CHAINS[String(walletProvider.getNetwork().chainId)];
    if (chain === undefined) throw new Error("Bruno serves Ethereum mainnet and Sepolia");
    return new Bruno({ ...this.#config, chain });
  }

  /** The call that moves `value` of the relay's token from `payer` to `recipient`: the token's
   * transferWithAuthorization, signed by the wallet, which anyone may send. */
  private async transferCall(
    walletProvider: EvmWalletProvider,
    requirement: Requirement,
    payer: Hex,
    recipient: Hex,
    value: bigint,
  ): Promise<{ to: Hex; data: Hex }> {
    const now = Math.floor(Date.now() / 1000);
    const transfer: Transfer = {
      from: payer,
      to: recipient,
      value,
      validAfter: 0n,
      validBefore: BigInt(now + requirement.maxTimeoutSeconds + TRANSFER_SLACK_SECONDS),
      nonce: freshNonce(),
    };
    const signer = viemSigner(walletProvider.toSigner());
    const signature = await signer.signTypedData(typedData(requirement, transfer));
    const r = `0x${signature.slice(2, 66)}` as Hex;
    const s = `0x${signature.slice(66, 130)}` as Hex;
    const v = Number.parseInt(signature.slice(130, 132), 16);
    const data = encodeFunctionData({
      abi: TRANSFER_WITH_AUTHORIZATION,
      functionName: "transferWithAuthorization",
      args: [
        transfer.from,
        transfer.to,
        transfer.value,
        transfer.validAfter,
        transfer.validBefore,
        transfer.nonce,
        v < 27 ? v + 27 : v,
        r,
        s,
      ],
    });
    return { to: requirement.asset, data };
  }

  /** A call's gas: estimated, with headroom. */
  private async gas(walletProvider: EvmWalletProvider, to: Hex, data: Hex): Promise<bigint> {
    const estimate = await walletProvider.getPublicClient().estimateGas({
      account: walletProvider.getAddress() as Hex,
      to,
      data,
    });
    return (estimate * GAS_HEADROOM_NUM + GAS_HEADROOM_DEN - 1n) / GAS_HEADROOM_DEN;
  }

  private async decimals(walletProvider: EvmWalletProvider, token: Hex): Promise<number> {
    return Number(
      await walletProvider.readContract({
        address: token,
        abi: erc20Abi,
        functionName: "decimals",
      }),
    );
  }
}

export const brunoActionProvider = (config: BrunoActionProviderConfig = {}): BrunoActionProvider =>
  new BrunoActionProvider(config);
