/**
 * Accounts, signed transactions, the funding transaction, bundles and the Flashbots signature,
 * against the Python reference's golden vectors: the same bytes.
 */

import { assertEquals } from "@std/assert";
import { derive } from "../src/accounts.ts";
import { flashbotsSignature } from "../src/builders.ts";
import { bundles, fundingInitcode, type Mode, signFunding } from "../src/bundle.ts";
import { bytes, checksum, type Hex, hex, utf8 } from "../src/evm.ts";
import {
  authority,
  type Fees,
  maxCost,
  type Rlp,
  rlp,
  sign,
  signDrain,
  type SignedAuthorization,
  type SignedTransaction,
  signer,
} from "../src/transactions.ts";
import { address, data, dataFeeOf, type DataFeeVector, read, schedule } from "./reference.ts";

interface AuthorizationVector {
  chainId: string;
  address: string;
  nonce: string;
  yParity: number;
  r: string;
  s: string;
}

interface Signed {
  raw: string;
  hash: string;
  sender: string;
  maxCost: string;
}

interface Vectors {
  rlp: ({ kind: "bytes" | "list"; length: number } | { kind: "int"; value: string })[] & {
    rlp: string;
  }[];
  chainId: number;
  float: { key: string; address: string };
  fees: { maxFeePerGas: string; maxPriorityFeePerGas: string };
  accounts: { index: number; address: string }[];
  authorities: { authorization: AuthorizationVector; authority: string }[];
  transactions: ({
    name: string;
    account: number;
    nonce: number;
    gas: number;
    to: string | null;
    dataLength: number;
    value: string;
    authorizations: AuthorizationVector[];
    dataFee: DataFeeVector | null;
  } & Signed)[];
  drains: (
    & { account: number; balance: string; dataFee: DataFeeVector | null }
    & Partial<
      Signed
    >
  )[];
  fundings: {
    fork: string;
    topUps: { account: string; amount: string; newAccount: boolean }[];
    initcode: string;
    gas: number;
    transaction: Signed;
  }[];
  bundles: { mode: Mode; block: number; funding: boolean; params: unknown }[];
  flashbots: { body: string; identity: string; header: string }[];
}

const vectors = await read<Vectors>();
const float = signer(bytes(vectors.float.key as Hex));
const fees: Fees = {
  maxFeePerGas: BigInt(vectors.fees.maxFeePerGas),
  maxPriorityFeePerGas: BigInt(vectors.fees.maxPriorityFeePerGas),
};

function authorizationOf(v: AuthorizationVector): SignedAuthorization {
  return {
    chainId: BigInt(v.chainId),
    address: checksum(v.address),
    nonce: BigInt(v.nonce),
    yParity: v.yParity,
    r: BigInt(v.r),
    s: BigInt(v.s),
  };
}

function assertSigned(tx: SignedTransaction, expected: Signed, what: string): void {
  assertEquals(hex(tx.raw), expected.raw, what);
  assertEquals(hex(tx.hash), expected.hash, what);
  assertEquals(tx.sender, expected.sender, what);
  assertEquals(maxCost(tx).toString(), expected.maxCost, what);
}

Deno.test("rlp: each side of its length boundaries", () => {
  for (const v of vectors.rlp) {
    const item: Rlp = v.kind === "int"
      ? BigInt(v.value)
      : v.kind === "bytes"
      ? data(v.length)
      : Array.from({ length: v.length }, () => Uint8Array.of(1));
    assertEquals(hex(rlp(item)), v.rlp, JSON.stringify({ ...v, rlp: undefined }));
  }
});

Deno.test("accounts: the float, and concurrency accounts derived from it", () => {
  assertEquals(float.address, vectors.float.address);
  for (const v of vectors.accounts) {
    assertEquals(derive(float, v.index).address, v.address, `account ${v.index}`);
  }
});

Deno.test("authorizations: their authority recovered", () => {
  for (const v of vectors.authorities) {
    assertEquals(authority(authorizationOf(v.authorization)), v.authority);
  }
});

Deno.test("transactions: types 2 and 4, to an address or creating, with a data fee or none", () => {
  for (const v of vectors.transactions) {
    const tx = sign(
      derive(float, v.account),
      vectors.chainId,
      v.nonce,
      v.dataFee === null ? fees : { ...fees, dataFee: dataFeeOf(v.dataFee) },
      v.gas,
      v.to === null ? null : checksum(v.to),
      data(v.dataLength),
      BigInt(v.value),
      v.authorizations.map(authorizationOf),
    );
    assertSigned(tx, v, `${v.name}${v.dataFee === null ? "" : ", with a data fee"}`);
  }
});

Deno.test("drains: type 1, leaving 0 (or a data fee's slack); none when the balance doesn't cover it", () => {
  for (const v of vectors.drains) {
    const tx = signDrain(
      derive(float, v.account),
      vectors.chainId,
      4,
      BigInt(v.balance),
      10n ** 9n,
      float.address,
      21_000,
      v.dataFee === null ? undefined : dataFeeOf(v.dataFee),
    );
    const what = `account ${v.account}${v.dataFee === null ? "" : ", with a data fee"}`;
    if (v.raw === undefined) {
      assertEquals(tx, null, what);
    } else {
      assertEquals(tx === null ? null : hex(tx.raw), v.raw, what);
      assertEquals(tx === null ? null : maxCost(tx).toString(), v.maxCost, what);
    }
  }
});

Deno.test("funding: initcode, its gas, and the float's signed transaction", () => {
  for (const v of vectors.fundings) {
    const topUps = v.topUps.map((t) => ({
      account: checksum(t.account),
      amount: BigInt(t.amount),
      newAccount: t.newAccount,
    }));
    const { code, gas } = fundingInitcode(topUps, schedule(v.fork));
    assertEquals(hex(code), v.initcode, v.fork);
    assertEquals(gas, v.gas, v.fork);
    const tx = signFunding(float, vectors.chainId, 9, fees, topUps, schedule(v.fork));
    assertSigned(tx, v.transaction, `${v.fork} funding`);
  }
});

Deno.test("bundles: both modes' eth_sendBundle parameters", () => {
  // as the reference builds them: two submissions to address("to"), and a funding transaction
  // topping up both senders, in the dropping bundle only
  const txs = [0, 1].map((i) =>
    sign(derive(float, i), vectors.chainId, 0, fees, 50_000, address("to"), data(10))
  );
  const topUps = txs.map((tx) => ({ account: tx.sender, amount: 10n ** 15n, newAccount: true }));
  const funding = signFunding(float, vectors.chainId, 0, fees, topUps, schedule("amsterdam"));
  for (const v of vectors.bundles) {
    const params = bundles(v.mode, v.block, v.mode === "dropping" ? funding : null, txs);
    assertEquals(params, v.params, v.mode);
  }
});

Deno.test("flashbots: the X-Flashbots-Signature header", () => {
  for (const v of vectors.flashbots) {
    const identity = signer(bytes(v.identity as Hex));
    assertEquals(flashbotsSignature(utf8(v.body), identity), v.header, v.body);
  }
});
