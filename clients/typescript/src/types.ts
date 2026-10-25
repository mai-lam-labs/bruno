/** What the client sends and receives: Bruno's wire shapes (SPEC.md), as TypeScript. */

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

/** A call's price in each token asked for, valid until a block. */
export interface Quote {
  readonly validUntilBlock: bigint;
  readonly amounts: ReadonlyMap<Hex, bigint>;
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
 * What pays: an address, and something that signs typed data for it. A wallet MCP, a
 * browser wallet or a key; for viem's accounts, `viemSigner`.
 */
export interface Signer {
  readonly address: Hex;
  signTypedData(typedData: TypedData): Promise<Hex>;
}
