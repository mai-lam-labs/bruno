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
import {
  type Action,
  ActionProvider,
  type EvmWalletProvider,
  type Network,
} from "@coinbase/agentkit";
import {
  baseUnitsOf,
  Bruno,
  CHAIN_IDS,
  type Deployment,
  deploymentOf,
  type Hex,
  hex,
  type Offer,
  type PayWith,
  type Requirement,
  type Token,
  transferCalldata,
  transferOf,
  typedData,
  unwrapped,
  viemSigner,
} from "@mailam/bruno";

/** The most paid to Bruno for one call unless the agent says otherwise, in whole USDC. */
const DEFAULT_MAX_FEE = "5";
/** How long a USDC transfer the agent signs stays valid past the payment's own window: time
 * for Bruno to include it. */
const TRANSFER_SLACK_SECONDS = 600;
/** A call's gas over its estimate: an estimate can be a little under what a call needs. */
const GAS_HEADROOM_NUM = 5n;
const GAS_HEADROOM_DEN = 4n;

/** Ample for measuring a transfer after its wallet's deployment, a smart wallet's check in it. */
const MEASURING_GAS = 2_000_000n;

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

/** Each action's description, as the agent reads it. */
const SEND_USDC = `
Send USDC from this wallet to any address on Ethereum without holding ETH. Bruno, a
permissionless gas relay, sends the transfer and is paid in USDC from the same wallet, in the
same transaction: if the transfer fails or never lands, nothing is paid.

Inputs:
- recipient: the address to send to
- amount: how much USDC, in whole units (e.g. "2.5")
- maxFee: the most USDC to pay Bruno, in whole units (default "${DEFAULT_MAX_FEE}"); a higher
  price is refused

The wallet needs the amount plus Bruno's fee in USDC, and no ETH. Ethereum mainnet and Sepolia.
`;

const SEND_CALL = `
Send a call on Ethereum without holding ETH, paying Bruno (a permissionless gas relay) in USDC
from this wallet, only if the call succeeds and lands.

The call runs from Bruno's own one-shot contract, NOT from this wallet: use it only for calls
whose effect doesn't depend on who sends them (a claim to a named recipient, a signed
authorization, a mint for an address). A swap or transfer from this wallet must not be sent
this way: use send_usdc_gasless to move USDC.

Inputs: to, data (0x hex), gas (optional, estimated), maxFee (the most USDC to pay, in whole
units, default "${DEFAULT_MAX_FEE}"). Ethereum mainnet and Sepolia.
`;

const PRICE = `
What sending a call on Ethereum through Bruno (a permissionless gas relay) costs, in USDC,
without sending it or paying anything. Inputs: to, data (0x hex), gas (optional, estimated).
`;

export interface BrunoActionProviderConfig {
  /** Bruno's URL: its own deployment if not given. */
  readonly url?: string;
  /** `fetch`, if not the global one. */
  readonly fetch?: typeof fetch;
  /** Chains beyond Ethereum mainnet and Sepolia, by chain id, each with its tokens: USDC
   * among them, which the actions pay in. */
  readonly chains?: Readonly<Record<string, readonly Token[]>>;
}

export class BrunoActionProvider extends ActionProvider<EvmWalletProvider> {
  readonly #config: BrunoActionProviderConfig;

  constructor(config: BrunoActionProviderConfig = {}) {
    super("bruno", []);
    this.#config = config;
  }

  /**
   * The actions, bound to `walletProvider`: listed directly rather than by AgentKit's
   * `@CreateAction`, a legacy decorator reading emitted type metadata, which a package compiled
   * by anyone else (JSR's npm build, a bundler) needn't keep. Named as the decorator names them.
   */
  override getActions(walletProvider: EvmWalletProvider): Action[] {
    const actions: Action[] = [
      {
        name: "send_usdc_gasless",
        description: SEND_USDC,
        schema: SendUsdcSchema,
        invoke: (args: SendUsdcArgs) => this.sendUsdcGasless(walletProvider, args),
      },
      {
        name: "send_call_gasless",
        description: SEND_CALL,
        schema: SendCallSchema,
        invoke: (args: SendCallArgs) => this.sendCallGasless(walletProvider, args),
      },
      {
        name: "get_gasless_price",
        description: PRICE,
        schema: PriceSchema,
        invoke: (args: PriceArgs) => this.getGaslessPrice(walletProvider, args),
      },
    ];
    return actions.map((a) => ({ ...a, name: `BrunoActionProvider_${a.name}` }));
  }

  async sendUsdcGasless(
    walletProvider: EvmWalletProvider,
    args: SendUsdcArgs,
  ): Promise<string> {
    try {
      const bruno = this.bruno(walletProvider);
      const usdc = bruno.token("USDC");
      const payer = walletProvider.getAddress() as Hex;
      // the relay's USDC, its EIP-712 domain and how long a payment waits: from a price asked
      // for any call, before the transfer it sends is signed
      const { offers } = await bruno.x402.price({ call: { to: payer, data: "0x" }, gas: 21_000 });
      const { requirement } = usdcOffer(offers, usdc.address);
      const { call, deployment } = await this.transferCall(
        walletProvider,
        requirement,
        payer,
        args.recipient as Hex,
        baseUnitsOf({ decimal: args.amount }, usdc),
      );
      const gas = deployment === undefined
        ? await this.gas(walletProvider, call.to, call.data)
        : await this.deployedGas(walletProvider, deployment, call.to, call.data);
      const paid = await bruno.x402.payAndSend(
        { call, gas, ...(deployment === undefined ? {} : { deployment }) },
        viemSigner(walletProvider.toSigner()),
        { pay: payWith(args.maxFee) },
      );
      return `Sent ${args.amount} USDC to ${args.recipient} in transaction ${paid.transaction}, ` +
        `paying Bruno ${paid.paid.decimal} USDC for it. No ETH was used.`;
    } catch (e) {
      return `Sending USDC through Bruno failed: ${e instanceof Error ? e.message : String(e)}`;
    }
  }

  async sendCallGasless(
    walletProvider: EvmWalletProvider,
    args: SendCallArgs,
  ): Promise<string> {
    try {
      const bruno = this.bruno(walletProvider);
      const call = { to: args.to as Hex, data: args.data as Hex };
      const gas = args.gas ? BigInt(args.gas) : await this.gas(walletProvider, call.to, call.data);
      const paid = await bruno.x402.payAndSend(
        { call, gas },
        viemSigner(walletProvider.toSigner()),
        { pay: payWith(args.maxFee) },
      );
      return `Sent the call in transaction ${paid.transaction}, paying Bruno ` +
        `${paid.paid.decimal} USDC. No ETH was used.`;
    } catch (e) {
      return `Sending the call through Bruno failed: ${e instanceof Error ? e.message : String(e)}`;
    }
  }

  async getGaslessPrice(
    walletProvider: EvmWalletProvider,
    args: PriceArgs,
  ): Promise<string> {
    try {
      const bruno = this.bruno(walletProvider);
      const call = { to: args.to as Hex, data: args.data as Hex };
      const gas = args.gas ? BigInt(args.gas) : await this.gas(walletProvider, call.to, call.data);
      const { offers } = await bruno.x402.price({ call, gas });
      const { requirement, price } = usdcOffer(offers, bruno.token("USDC").address);
      return `Sending it through Bruno costs ${price.decimal} USDC, paid only if it succeeds ` +
        `and lands (valid for about ${requirement.maxTimeoutSeconds} seconds).`;
    } catch (e) {
      return `Asking Bruno's price failed: ${e instanceof Error ? e.message : String(e)}`;
    }
  }

  supportsNetwork(network: Network): boolean {
    return network.protocolFamily === "evm" && network.chainId !== undefined &&
      this.#serves(network.chainId);
  }

  /** Whether the actions serve chain `id`: Ethereum mainnet and Sepolia, and the chains given. */
  #serves(id: string): boolean {
    return Object.values(CHAIN_IDS).includes(Number(id)) || id in (this.#config.chains ?? {});
  }

  private bruno(walletProvider: EvmWalletProvider): Bruno {
    const id = String(walletProvider.getNetwork().chainId);
    if (!this.#serves(id)) {
      throw new Error(`chain ${id}: Bruno serves Ethereum mainnet and Sepolia, and chains given`);
    }
    return new Bruno({
      chain: Number(id),
      tokens: this.#config.chains?.[id] ?? [],
      ...(this.#config.url === undefined ? {} : { url: this.#config.url }),
      ...(this.#config.fetch === undefined ? {} : { fetch: this.#config.fetch }),
    });
  }

  /**
   * The call that moves `value` of the relay's token from `payer` to `recipient`: the token's
   * transferWithAuthorization, signed by the wallet, which anyone may send; and, if the wallet
   * isn't deployed yet (its signature ERC-6492-wrapped), its deployment, which Bruno makes first
   * in the same transaction. The token is given the inner signature, through the overload its
   * length takes.
   */
  private async transferCall(
    walletProvider: EvmWalletProvider,
    requirement: Requirement,
    payer: Hex,
    recipient: Hex,
    value: bigint,
  ): Promise<{ call: { to: Hex; data: Hex }; deployment?: Deployment }> {
    const transfer = transferOf(
      payer,
      recipient,
      value,
      requirement.maxTimeoutSeconds + TRANSFER_SLACK_SECONDS,
    );
    const signer = viemSigner(walletProvider.toSigner());
    const signed = await signer.signTypedData(typedData(requirement, transfer));
    const call = {
      to: requirement.asset,
      data: transferCalldata(transfer, unwrapped(signed)?.signature ?? signed),
    };
    const deployment = deploymentOf(signed, payer);
    return deployment === undefined ? { call } : { call, deployment };
  }

  /**
   * A call's gas once `deployment` is made: measured by a deployless eth_call (initcode run, not
   * deployed, as ERC-6492's off-chain checks are) that makes the deployment, then the call
   * between two GAS readings, and returns whether the call succeeded and their difference; with
   * headroom. Estimated alone, the call would fail: the wallet has no code yet.
   */
  private async deployedGas(
    walletProvider: EvmWalletProvider,
    deployment: Deployment,
    to: Hex,
    data: Hex,
  ): Promise<bigint> {
    const answer = await walletProvider.getPublicClient().request({
      method: "eth_call",
      params: [{ data: measuringCode(deployment, to, data), gas: hex(MEASURING_GAS) }, "latest"],
    }) as Hex;
    if (BigInt(answer.slice(0, 66)) !== 1n) {
      throw new Error("the transfer would fail, even once the wallet is deployed");
    }
    const measured = BigInt(`0x${answer.slice(66, 130)}`);
    return (measured * GAS_HEADROOM_NUM + GAS_HEADROOM_DEN - 1n) / GAS_HEADROOM_DEN;
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
}

/** `n` in `bytes` bytes, as hex without its 0x. */
function fixed(n: number, bytes: number): string {
  return n.toString(16).padStart(bytes * 2, "0");
}

/**
 * Initcode that calls `deployment`'s factory with its data, then `to` with `data` between two
 * GAS readings, and returns 64 bytes: whether that call succeeded, and the gas between the
 * readings. Its code is 91 bytes, the factory's data and the call's appended after it.
 */
function measuringCode(deployment: Deployment, to: Hex, data: Hex): Hex {
  const CODE_BYTES = 91;
  const factoryData = deployment.factoryData.slice(2);
  const callData = data.slice(2);
  const f = factoryData.length / 2;
  const c = callData.length / 2;
  const code = [
    // memory[0:f] = the factory's data; CALL(GAS, factory, 0, 0, f, 0, 0), its success dropped
    `61${fixed(f, 2)}61${fixed(CODE_BYTES, 2)}5f39`,
    `5f5f61${fixed(f, 2)}5f5f73${deployment.factory.slice(2)}5af150`,
    // memory[0:c] = the call's data; GAS; CALL(GAS, to, 0, 0, c, 0, 0); GAS
    `61${fixed(c, 2)}61${fixed(CODE_BYTES + f, 2)}5f39`,
    `5a5f5f61${fixed(c, 2)}5f5f73${to.slice(2)}5af15a`,
    // memory[0] = success, memory[32] = g1 - g2; RETURN(0, 64)
    "905f52",
    "9003602052",
    "60405ff3",
  ].join("");
  if (code.length / 2 !== CODE_BYTES) throw new Error("the measuring code isn't 91 bytes");
  return `0x${code}${factoryData}${callData}`;
}

/** USDC's offer among a price's: the only token the actions pay in, over x402. */
function usdcOffer(offers: readonly Offer[], usdc: Hex): Offer {
  const offer = offers.find((o) => o.requirement.asset.toLowerCase() === usdc.toLowerCase());
  if (offer === undefined) throw new Error("Bruno doesn't take USDC over x402 here");
  return offer;
}

/** What the agent pays Bruno in: USDC, up to `maxFee` whole USDC. */
function payWith(maxFee: string | null | undefined): readonly PayWith[] {
  return [{ token: "USDC", max: { decimal: maxFee ?? DEFAULT_MAX_FEE } }];
}

export const brunoActionProvider = (config: BrunoActionProviderConfig = {}): BrunoActionProvider =>
  new BrunoActionProvider(config);
