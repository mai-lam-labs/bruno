/** What the client sends and receives: Bruno's wire shapes (SPEC.md), as TypeScript. */

import type { Category, Refusal, Rejection } from "./refusals.ts";
import type { Value } from "./tokens.ts";

/** A 0x-prefixed hex string. */
export type Hex = `0x${string}`;

/** A call: the contract or account it's to, its data, the ETH it sends, if any, and the gas
 * it's given (the relay's estimate, if not). */
export interface Call {
  readonly to: Hex;
  readonly data: Hex;
  readonly value?: bigint;
  readonly gas?: bigint | number;
}

/** A call a quote prices before it's built: its data's length, and its gas. */
export interface UnbuiltCall {
  readonly callDataLength: number;
  readonly gas: bigint | number;
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
  /** Where a sponsorship's payment is made: the payee's forwarder. */
  readonly forwarder: Hex;
  readonly tokens: readonly Hex[];
  readonly maxGas: bigint;
  readonly maxCallDataLength: number;
}

/** The calls' price in each token asked, in their order, valid until `validUntilBlock`, and
 * the gas each call is priced for, in the calls' order: what to give it in the submission. */
export interface Quote {
  readonly validUntilBlock: bigint;
  readonly prices: readonly Value[];
  readonly calls: readonly { readonly gas: bigint }[];
  /** Each price broken down, in the same order, for people to read. */
  readonly breakdowns: readonly (Breakdown | null)[];
  /** Each price's transfer for its payer to sign, in the same order: with a payer, for a token
   * taking EIP-3009 (EIP-712's JSON, as viem signs it; ethers wants `EIP712Domain` left out of
   * its `types`). Check it's the payment before signing it (checkedTransfer). */
  readonly transfers: readonly (TypedData | null)[];
}

/** A part of a price: its gas, and what it comes to in the price's token. */
export interface Part {
  readonly gas: bigint;
  readonly amount: bigint;
}

/** A price in parts, summing to it: the account's setup, the calls, the ETH they send (no gas
 * of its own), the payment's transfer and check, and the relay's own, the rest. */
export interface Breakdown {
  readonly setup: Part;
  readonly call: Part;
  readonly value: Part;
  readonly payment: Part;
  readonly relay: Part;
}

/** The calls to quote, each whole (its gas estimated, unless given) or only its data's length
 * and its gas; with their EIP-7702 authorizations, signed, or the account that will sign one
 * (`authority`). */
export interface QuoteRequest {
  readonly calls: readonly (Call | UnbuiltCall)[];
  readonly authorizationList?: readonly Authorization[];
  readonly authority?: Hex;
}

/** Where a submission is: waiting, sending, confirming, done or failed. */
export type Stage = "waiting" | "sending" | "confirming" | "done" | "failed";

/** A submission's status. */
export interface Status {
  readonly status: string;
  readonly stage: Stage;
  readonly attempts: number;
  /** Every transaction of it that landed, in order: a call paid by a transfer sent beside it
   * is the call's and then the transfer's; a sponsorship, the relay's first, the account's own
   * and the relay's last. */
  readonly transactionHashes: readonly Hex[];
  readonly blockNumber: bigint | null;
  readonly confirmations: bigint | null;
  readonly reason: Rejection | null;
  readonly revertData: Hex | null;
}

/**
 * A relay error's data: why (`reason`, the refusal), and what its code adds: for invalid params,
 * the field at fault, and if it's over a limit the `most` it may be and what it `needs` (hex
 * quantities); for unsupported or unavailable, who can act on it (`category`); for underpaid or
 * no capacity, what's `required` in `token`; for a simulation failed, the revert data whole; for a
 * chain not served, the chain.
 */
export interface ErrorData {
  readonly reason: Refusal;
  readonly field?: string;
  readonly most?: string;
  readonly needs?: string;
  readonly category?: Category;
  readonly token?: Hex;
  readonly required?: string;
  readonly revertData?: Hex;
  readonly chain?: string;
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

/** The calls to pay for, each with its gas (the relay's estimate, if not given), and any
 * EIP-7702 authorizations; and what's known of their payer, as for a quote. */
export interface PaidRequest extends Signing {
  readonly calls: readonly Call[];
  readonly authorizationList?: readonly Authorization[];
}
