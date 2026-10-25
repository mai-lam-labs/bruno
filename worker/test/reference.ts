/**
 * The Python reference's golden vectors (bruno/tests/vectors.py, written by `make vectors`), and
 * the case data both sides derive the same way.
 */

import type { DataFee } from "../src/datafee.ts";
import { type Address, checksum, hex, keccak, utf8 } from "../src/evm.ts";
import { type GasSchedule, SCHEDULES } from "../src/forks.ts";

/** The vectors, read as `T`: each test declares the parts it reads. */
export async function read<T>(): Promise<T> {
  return JSON.parse(await Deno.readTextFile(new URL("./vectors.json", import.meta.url)));
}

export function schedule(fork: string): GasSchedule {
  const found = SCHEDULES[fork];
  if (found === undefined) {
    throw new Error(`no schedule ${fork}`);
  }
  return found;
}

/** The reference's case data: keccak of "data {counter}", every third byte zero. */
export function data(length: number): Uint8Array {
  const out = new Uint8Array(length);
  for (let counter = 0, filled = 0; filled < length; counter++) {
    const block = keccak(utf8(`data ${counter}`));
    for (const b of block) {
      if (filled === length) break;
      out[filled] = filled % 3 ? b : 0;
      filled++;
    }
  }
  return out;
}

/** The reference's case addresses: the last 20 bytes of keccak of a name. */
export function address(name: string): Address {
  return checksum(hex(keccak(utf8(name)).slice(-20)));
}

/** An OP-stack chain's fee inputs, as the vectors write them. */
export type DataFeeVector = Record<keyof DataFee, string>;

export function dataFeeOf(v: DataFeeVector): DataFee {
  return {
    l1BaseFee: BigInt(v.l1BaseFee),
    blobBaseFee: BigInt(v.blobBaseFee),
    baseFeeScalar: BigInt(v.baseFeeScalar),
    blobBaseFeeScalar: BigInt(v.blobBaseFeeScalar),
    operatorFeeScalar: BigInt(v.operatorFeeScalar),
    operatorFeeConstant: BigInt(v.operatorFeeConstant),
  };
}
