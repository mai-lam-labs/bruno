/**
 * A client's submission (SPEC.md, Submission): its id, keccak256 of the submission
 * ABI-encoded, as the Python reference computes it (bruno/submission.py, Submission.id), and
 * the checks on its authorizations that need nothing from the chain.
 */

import { concatBytes } from "@noble/hashes/utils.js";
import { encoded, type TransferAuthorization, transferDeployment } from "./eip3009.ts";
import { type Deployment, deploymentEncoded, sameDeployment, WrapperError } from "./erc6492.ts";
import { type Address, type EncodedCall, keccak, SECP256K1_N } from "./evm.ts";
import { authority, type SignedAuthorization } from "./transactions.ts";
import { addressWord, encodeBytes, word } from "./abi.ts";

export interface Submission {
  readonly call: EncodedCall;
  readonly authorizations: readonly SignedAuthorization[];
  readonly gas: number;
  readonly token: Address;
  readonly payee: Address;
  readonly amount: bigint;
  readonly maxBlockNumber: number;
  /** A signed transfer of the amount (EIP-3009), if it comes with one, made before the call. */
  readonly transfer?: TransferAuthorization;
  /** An account to deploy before the call, if it names one. */
  readonly deployment?: Deployment;
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
    if (e instanceof WrapperError) throw new InvalidSubmissionError("payment", e.message);
    throw e;
  }
  if (fromWrapper === null) return s.deployment ?? null;
  if (s.deployment !== undefined && !sameDeployment(s.deployment, fromWrapper)) {
    throw new InvalidSubmissionError(
      "deployment",
      "not the one the payment's signature deploys: at most one",
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
 * keccak256(abi.encode(address to, bytes data, (uint256,address,uint256,uint8,uint256,uint256)[]
 * authorizations, uint256 gas, address token, address payee, uint256 amount, uint256
 * maxBlockNumber)): the same submission, the same id. One paid by a transfer has the transfer,
 * (address,address,uint256,uint256,uint256,bytes32,bytes), in place of its deadline, which over
 * x402 follows from the transfer's validBefore. Its own deployment, (address,address,bytes), if
 * it names one, is appended.
 */
export function submissionId(s: Submission): Uint8Array {
  const head: Uint8Array[] = [];
  const tail: Uint8Array[] = [];
  const headSize = (s.deployment === undefined ? 8 : 9) * 32;
  let tailSize = 0;
  const dynamic = (encoded: Uint8Array): void => {
    head.push(word(BigInt(headSize + tailSize)));
    tail.push(encoded);
    tailSize += encoded.length;
  };
  head.push(addressWord(s.call.to));
  dynamic(encodeBytes(s.call.data));
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
    word(BigInt(s.gas)),
    addressWord(s.token),
    addressWord(s.payee),
    word(s.amount),
  );
  if (s.transfer === undefined) head.push(word(BigInt(s.maxBlockNumber)));
  else dynamic(encoded(s.transfer));
  if (s.deployment !== undefined) dynamic(deploymentEncoded(s.deployment));
  return keccak(concatBytes(...head, ...tail));
}

/** Each authorization's signer, recovered: InvalidSubmissionError if one can't be. */
export function authorities(authorizations: readonly SignedAuthorization[]): Address[] {
  try {
    return authorizations.map(authority);
  } catch (e) {
    throw new InvalidSubmissionError("authorizationList", `unrecoverable: ${e}`);
  }
}

/** EIP-7702's bound on an authorization's nonce: under 2**64 - 1. */
const MAX_AUTHORIZATION_NONCE = (1n << 64n) - 1n;

/** A field of the submission is the client's mistake. */
export class InvalidSubmissionError extends Error {
  constructor(readonly field: string, message: string) {
    super(`${field}: ${message}`);
  }
}

/**
 * At most `most`, each one EIP-7702 would take: its chain id `chainId` or 0, yParity 0 or 1,
 * r and s in range with s low, its nonce under 2**64 - 1.
 */
export function checkAuthorizations(
  authorizations: readonly SignedAuthorization[],
  chainId: number,
  most: number,
): void {
  if (authorizations.length > most) {
    throw new InvalidSubmissionError("authorizationList", `over ${most}`);
  }
  for (const a of authorizations) {
    if (a.chainId !== 0n && a.chainId !== BigInt(chainId)) {
      throw new InvalidSubmissionError("authorizationList", `chain id ${a.chainId}`);
    }
    if (a.yParity !== 0 && a.yParity !== 1) {
      throw new InvalidSubmissionError("authorizationList", "yParity is 0 or 1");
    }
    if (!(a.r > 0n && a.r < SECP256K1_N && a.s > 0n && a.s <= SECP256K1_N / 2n)) {
      throw new InvalidSubmissionError("authorizationList", "r or s out of range");
    }
    if (a.nonce >= MAX_AUTHORIZATION_NONCE) {
      throw new InvalidSubmissionError("authorizationList", "nonce out of range");
    }
  }
}
