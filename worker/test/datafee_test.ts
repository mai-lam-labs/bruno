/**
 * The OP-stack fee formulas, their inputs risen, the fee oracle's calls, and transactions' most
 * bytes unsigned, against the Python reference's golden vectors.
 */

import { assertEquals } from "@std/assert";
import { checking, l1Fee, operatorFee, reading, risenDataFee } from "../src/datafee.ts";
import { checksum } from "../src/evm.ts";
import { unsignedSize } from "../src/transactions.ts";
import { data, dataFeeOf, type DataFeeVector, read } from "./reference.ts";

interface Vectors {
  chainId: number;
  dataFee: {
    fees: {
      inputs: DataFeeVector;
      l1Fees: { size: number; fee: string }[];
      operatorFees: { gas: number; fee: string }[];
      risen: { blocks: number; inputs: DataFeeVector }[];
    }[];
    reading: unknown[][];
    checking: unknown[][];
    sizes: {
      to: string | null;
      dataLength: number;
      value: number;
      authorizations: number;
      size: number;
    }[];
  };
}

const vectors = await read<Vectors>();

Deno.test("data fee: Fjord's L1 fee upper bound and Jovian's operator fee", () => {
  for (const v of vectors.dataFee.fees) {
    const fee = dataFeeOf(v.inputs);
    for (const { size, fee: expected } of v.l1Fees) {
      assertEquals(l1Fee(fee, size).toString(), expected, `${JSON.stringify(v.inputs)} ${size}`);
    }
    for (const { gas, fee: expected } of v.operatorFees) {
      assertEquals(operatorFee(fee, gas).toString(), expected, `${gas} gas`);
    }
    for (const r of v.risen) {
      assertEquals(risenDataFee(fee, r.blocks), dataFeeOf(r.inputs), `risen ${r.blocks}`);
    }
  }
});

Deno.test("data fee: the oracle's reads and checks", () => {
  assertEquals(reading().map((c) => c.params), vectors.dataFee.reading);
  assertEquals(checking("0x10").map((c) => c.params), vectors.dataFee.checking);
});

Deno.test("sizes: a transaction's most bytes unsigned", () => {
  for (const v of vectors.dataFee.sizes) {
    const size = unsignedSize(
      vectors.chainId,
      v.to === null ? null : checksum(v.to),
      data(v.dataLength),
      BigInt(v.value),
      v.authorizations,
    );
    assertEquals(size, v.size, JSON.stringify(v));
  }
});
