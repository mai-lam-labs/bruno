/**
 * A client's submission (SPEC.md, Submission), and its id: keccak256 of the submission
 * ABI-encoded, as the Python reference computes it (bruno/relay.py, Submission.id).
 */

import { concatBytes } from "@noble/hashes/utils.js";
import { type Address, bytes, type EncodedCall, keccak } from "./evm.ts";
import { bytesOf, type SignedAuthorization } from "./transactions.ts";

export interface Submission {
  readonly call: EncodedCall;
  readonly authorizations: readonly SignedAuthorization[];
  readonly gas: number;
  readonly token: Address;
  readonly payee: Address;
  readonly amount: bigint;
  readonly maxBlockNumber: number;
}

/**
 * keccak256(abi.encode(address to, bytes data, (uint256,address,uint256,uint8,uint256,uint256)[]
 * authorizations, uint256 gas, address token, address payee, uint256 amount, uint256
 * maxBlockNumber)): the same submission, the same id.
 */
export function submissionId(s: Submission): Uint8Array {
  const head: Uint8Array[] = [];
  const tail: Uint8Array[] = [];
  const headSize = 8 * 32;
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
    word(BigInt(s.maxBlockNumber)),
  );
  return keccak(concatBytes(...head, ...tail));
}

function word(value: bigint): Uint8Array {
  return bytesOf(value, 32);
}

function addressWord(address: Address): Uint8Array {
  return concatBytes(new Uint8Array(12), bytes(address));
}

function encodeBytes(data: Uint8Array): Uint8Array {
  const padded = new Uint8Array(Math.ceil(data.length / 32) * 32);
  padded.set(data);
  return concatBytes(word(BigInt(data.length)), padded);
}
