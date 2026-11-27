/**
 * A client's submission (SPEC.md, Submission): its id, keccak256 of the submission
 * ABI-encoded, as bruno/submission.py computes it (Submission.id), and
 * the checks on its authorizations that need nothing from the chain.
 */

import { concatBytes } from "@noble/hashes/utils.js";
import { type TransferAuthorization, transferDeployment, transferEncoded } from "./eip3009.ts";
import { type Deployment, deploymentEncoded, sameDeployment, WrapperError } from "./erc6492.ts";
import { type Address, type EncodedCall, keccak, SECP256K1_N } from "./evm.ts";
import { authority, type SignedAuthorization } from "./transactions.ts";
import { addressWord, encodeBytes, word } from "./abi.ts";
import { type Refused, refused } from "./refusals.ts";

/** The limit a field is over: the `most` it may be, and what it `needs`, where that isn't what
 * was given (a transaction's gas, the runner's included). */
export interface Bound {
  readonly most: bigint;
  readonly needs?: bigint;
}

/**
 * The account the runner deploys, if any: the submission's own deployment, or its transfer's
 * signature's ERC-6492 wrapper's, which must be the same if both are given. Its wrapper, if any,
 * is checked: InvalidSubmissionError otherwise. Checked as a submission's made (checkedSubmission).
 */
export function toDeploy(s: Submission): Deployment | null {
  if (s.transfer === undefined) return s.deployment ?? null;
  let fromWrapper: Deployment | null;
  try {
    fromWrapper = transferDeployment(s.transfer);
  } catch (e) {
    if (e instanceof WrapperError) {
      throw new InvalidSubmissionError("payment", refused("signature-wrapper", { why: e.message }));
    }
    throw e;
  }
  if (fromWrapper === null) return s.deployment ?? null;
  if (s.deployment !== undefined && !sameDeployment(s.deployment, fromWrapper)) {
    throw new InvalidSubmissionError(
      "deployment",
      refused("deployment-not-signatures"),
    );
  }
  return fromWrapper;
}

/** `s`, its transfer's wrapper and its deployments checked (toDeploy), as a submission's made. */
export function checkedSubmission(s: Submission): Submission {
  toDeploy(s);
  return s;
}

/**
 * keccak256(abi.encode((address,bytes,uint256,uint256)[] calls (each its target, data, value and
 * gas), (uint256,address,uint256,uint8,uint256,uint256)[] authorizations, address token, address
 * payee, uint256 amount, uint256 maxBlockNumber)): the same submission, the same id. One paid by
 * a transfer has the transfer,
 * (address,address,uint256,uint256,uint256,bytes32,bytes), in place of its deadline, which over
 * x402 follows from the transfer's validBefore. Its own deployment, (address,address,bytes), if
 * it names one, is appended.
 */
export function submissionId(s: Submission): Uint8Array {
  const head: Uint8Array[] = [];
  const tail: Uint8Array[] = [];
  const headSize = (s.deployment === undefined ? 6 : 7) * 32;
  let tailSize = 0;
  const dynamic = (encoded: Uint8Array): void => {
    head.push(word(BigInt(headSize + tailSize)));
    tail.push(encoded);
    tailSize += encoded.length;
  };
  // each call a dynamic tuple, its data after its four words; the list their offsets, then them
  const calls = s.calls.map(({ call, gas }) =>
    concatBytes(
      addressWord(call.to),
      word(4n * 32n),
      word(call.value),
      word(BigInt(gas)),
      encodeBytes(call.data),
    )
  );
  let callsSize = calls.length * 32;
  const offsets = calls.map((encoded) => {
    const offset = word(BigInt(callsSize));
    callsSize += encoded.length;
    return offset;
  });
  dynamic(concatBytes(word(BigInt(calls.length)), ...offsets, ...calls));
  dynamic(
    concatBytes(
      word(BigInt(s.authorizations.length)),
      ...s.authorizations.flatMap((a) => [
        word(a.chainId),
        addressWord(a.address),
        word(a.nonce),
        word(BigInt(a.yParity)),
        word(a.r),
        word(a.s),
      ]),
    ),
  );
  head.push(
    addressWord(s.token),
    addressWord(s.payee),
    word(s.amount),
  );
  if (s.transfer === undefined) head.push(word(BigInt(s.maxBlockNumber)));
  else dynamic(transferEncoded(s.transfer));
  if (s.deployment !== undefined) dynamic(deploymentEncoded(s.deployment));
  return keccak(concatBytes(...head, ...tail));
}

/** A field of the submission is the client's mistake: `field`, why (`reason`), and the limit
 * it's over, if that's why (`bound`). */
export class InvalidSubmissionError extends Error {
  constructor(readonly field: string, readonly refused: Refused, readonly bound?: Bound) {
    super(`${field}: ${refused}`);
  }
}

/**
 * The largest nonce an authorization may have: the largest integer a float64 holds exactly,
 * read alike by both relays (EIP-7702's bound is under 2**64 - 1; no account's nonce gets near
 * this one).
 */
export const MAX_AUTHORIZATION_NONCE = BigInt(Number.MAX_SAFE_INTEGER);

/** One of a submission's calls: the `call`, and the `gas` it's given. */
export interface SubmittedCall {
  readonly call: EncodedCall;
  readonly gas: number;
}

/** A submission's one call. Until a plan takes several: then every reader takes `calls`. */
export function soleCall(s: Submission): SubmittedCall {
  const [sole, ...others] = s.calls;
  if (sole === undefined || others.length > 0) {
    throw new Error(`a submission of ${s.calls.length} calls, where one is taken`);
  }
  return sole;
}

export interface Submission {
  readonly calls: readonly SubmittedCall[];
  readonly authorizations: readonly SignedAuthorization[];
  readonly token: Address;
  readonly payee: Address;
  readonly amount: bigint;
  readonly maxBlockNumber: number;
  /** A signed transfer of the amount (EIP-3009), if it comes with one, made before the call. */
  readonly transfer?: TransferAuthorization;
  /** An account to deploy before the call, if it names one. */
  readonly deployment?: Deployment;
}

/** Each authorization's signer, recovered: InvalidSubmissionError if one can't be. */
export function authorities(authorizations: readonly SignedAuthorization[]): Address[] {
  try {
    return authorizations.map(authority);
  } catch (e) {
    throw new InvalidSubmissionError(
      "authorizationList",
      refused("authorization-unrecoverable", { why: String(e) }),
    );
  }
}

/**
 * An authorization's yParity 0 or 1, its r and s in range with s low, and its nonce at most
 * MAX_AUTHORIZATION_NONCE: InvalidSubmissionError otherwise.
 */
export function checkSigned(yParity: number, r: bigint, s: bigint, nonce: bigint): void {
  if (yParity !== 0 && yParity !== 1) {
    throw new InvalidSubmissionError("authorizationList", refused("authorization-y-parity"));
  }
  if (!(r > 0n && r < SECP256K1_N && s > 0n && s <= SECP256K1_N / 2n)) {
    throw new InvalidSubmissionError("authorizationList", refused("authorization-r-s"));
  }
  if (nonce > MAX_AUTHORIZATION_NONCE) {
    throw new InvalidSubmissionError("authorizationList", refused("authorization-nonce"));
  }
}

/**
 * At most `most`, each one EIP-7702 would take: its chain id `chainId` or 0, and its signature
 * and nonce as checkSigned takes them.
 */
export function checkAuthorizations(
  authorizations: readonly SignedAuthorization[],
  chainId: number,
  most: number,
): void {
  if (authorizations.length > most) {
    throw new InvalidSubmissionError(
      "authorizationList",
      refused("authorizations-over", { most }),
      {
        most: BigInt(most),
      },
    );
  }
  for (const a of authorizations) {
    if (a.chainId !== 0n && a.chainId !== BigInt(chainId)) {
      throw new InvalidSubmissionError(
        "authorizationList",
        refused("authorization-chain", { chain_id: a.chainId }),
      );
    }
    checkSigned(a.yParity, a.r, a.s, a.nonce);
  }
}
