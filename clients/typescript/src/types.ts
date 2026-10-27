/** What the client sends and receives: Bruno's wire shapes (SPEC.md), as TypeScript. */

import type { Value } from "./tokens.ts";

/** A 0x-prefixed hex string. */
export type Hex = `0x${string}`;

/** A call: the contract or account it's to, and its data. */
export interface Call {
  readonly to: Hex;
  readonly data: Hex;
}

/** An EIP-7702 authorization, as a type-4 transaction's JSON carries it. */
export interface Authorization {
  readonly chainId: Hex;
  readonly address: Hex;
  readonly nonce: Hex;
  readonly yParity: Hex;
  readonly r: Hex;
  readonly s: Hex;
}

/** What a chain's relay takes: its payee, the tokens it's paid in, its limits. */
export interface Terms {
  readonly payee: Hex;
  readonly tokens: readonly Hex[];
  readonly maxGas: bigint;
  readonly maxCallBytes: number;
}

/** A call's price in each token asked for, in their order, valid until a block. */
export interface Quote {
  readonly validUntilBlock: bigint;
  readonly prices: readonly Value[];
}

/** Where a submission is: waiting, sending, confirming, done or failed. */
export type Stage = "waiting" | "sending" | "confirming" | "done" | "failed";

/** A submission's status. */
export interface Status {
  readonly status: string;
  readonly stage: Stage;
  readonly attempts: number;
  readonly transactionHash: Hex | null;
  readonly blockNumber: bigint | null;
  readonly confirmations: bigint | null;
  readonly reason: string | null;
  readonly revertData: Hex | null;
}

/**
 * EIP-712 typed data in the JSON every signer takes (eth_signTypedData_v4, a wallet MCP's
 * sign-typed-data tool): `EIP712Domain` listed, which some signers require, and numbers as
 * decimal strings.
 */
export interface TypedData {
  readonly domain: {
    readonly name: string;
    readonly version: string;
    readonly chainId: number;
    readonly verifyingContract: Hex;
  };
  readonly types: Readonly<Record<string, readonly { name: string; type: string }[]>>;
  readonly primaryType: string;
  readonly message: Readonly<Record<string, string>>;
}

/**
 * An account to deploy before the call, if it has no code: `factory` called with `factoryData`
 * (ERC-4337's names).
 */
export interface Deployment {
  readonly account: Hex;
  readonly factory: Hex;
  readonly factoryData: Hex;
}

/** How a smart account is deployed while it isn't (ERC-4337's factory and its data): neither,
 * once it is. viem's smart accounts give this from `getFactoryArgs`. */
export interface FactoryArgs {
  readonly factory?: Hex | undefined;
  readonly factoryData?: Hex | undefined;
}

/**
 * What pays: an address, and something that signs typed data for it. A wallet MCP, a
 * browser wallet or a key; for viem's accounts, `viemSigner`. A smart account that may not be
 * deployed yet signs ERC-6492-wrapped, and can say how it's deployed (`getFactoryArgs`, as
 * viem's smart accounts do), so its first price includes its deployment.
 */
export interface Signer {
  readonly address: Hex;
  signTypedData(typedData: TypedData): Promise<Hex>;
  getFactoryArgs?(): Promise<FactoryArgs>;
}

/**
 * What a price is told of the payment's signer, each if known: its address (the payment then a
 * signed transfer), an account to deploy first, and the gas its signature check takes. Signing
 * with a smart account, the client fills in its address and, while it's undeployed, its
 * deployment.
 */
export interface Signing {
  readonly payer?: Hex;
  readonly deployment?: Deployment;
  readonly verifyGas?: bigint | number;
}

/** A call to pay for: the call, its gas, and any EIP-7702 authorizations; and what's known of
 * its payer, as for a quote. */
export interface PaidRequest extends Signing {
  readonly call: Call;
  readonly gas: bigint | number;
  readonly authorizationList?: readonly Authorization[];
}
