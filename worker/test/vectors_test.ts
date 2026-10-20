/**
 * The port against the Python reference's golden vectors (bruno/tests/vectors.py, written by
 * `make vectors`): the same initcode, byte for byte, and the same gas, for every case.
 */

import { assertEquals, assertThrows } from "@std/assert";
import { DEPLOYER, INITCODE, overheadGas, RUNTIME, transactionGas } from "../src/deployer.ts";
import { type Address, checksum, hex, keccak, NATIVE_ASSET } from "../src/evm.ts";
import { data, read, schedule } from "./reference.ts";
import { intrinsic } from "../src/forks.ts";
import {
  BALANCE_UNREADABLE,
  frameGas,
  GAS_EXHAUSTED,
  inCallRunner,
  RunnerError,
  UNDERPAID,
} from "../src/runner.ts";

interface RunnerVector {
  fork: string;
  call: { to: string; dataLength: number };
  payment: { token: { address: string; balanceGas: number } | null; payee: string; amount: string };
  delegated: string[];
  initcodeLength: number;
  initcodeKeccak: string;
  overheadGas: number;
  frameGas: Record<string, number>;
  transactionGas: {
    clientGas: number;
    authorizations: number;
    emptyAuthorities: number;
    gas: number;
  }[];
}

export interface Vectors {
  deployer: {
    runtime: string;
    initcode: string;
    address: string;
    overheadGas: { fork: string; initcodeLength: number; gas: number }[];
  };
  errors: { underpaid: string; balanceUnreadable: string; gasExhausted: string };
  nativeAsset: string;
  intrinsic: {
    fork: string;
    calldataLength: number;
    creation: boolean;
    authorizations: number;
    standard: number;
    floor: number;
  }[];
  runners: RunnerVector[];
  refusals: {
    name: string;
    fork: string;
    call: { to: string; dataLength: number; value: string };
    amount: string;
  }[];
}

const vectors = await read<Vectors>();

Deno.test("deployer: code, address and its own gas", () => {
  assertEquals(hex(RUNTIME), vectors.deployer.runtime);
  assertEquals(hex(INITCODE), vectors.deployer.initcode);
  assertEquals(DEPLOYER, vectors.deployer.address);
  for (const v of vectors.deployer.overheadGas) {
    assertEquals(overheadGas(v.initcodeLength, schedule(v.fork)), v.gas, JSON.stringify(v));
  }
});

Deno.test("errors and the native asset", () => {
  assertEquals(hex(UNDERPAID), vectors.errors.underpaid);
  assertEquals(hex(BALANCE_UNREADABLE), vectors.errors.balanceUnreadable);
  assertEquals(hex(GAS_EXHAUSTED), vectors.errors.gasExhausted);
  assertEquals(NATIVE_ASSET, vectors.nativeAsset);
});

Deno.test("intrinsic gas and the floor", () => {
  for (const v of vectors.intrinsic) {
    const got = intrinsic(schedule(v.fork), data(v.calldataLength), {
      creation: v.creation,
      authorizations: v.authorizations,
    });
    assertEquals(got, { standard: v.standard, floor: v.floor }, JSON.stringify(v));
  }
});

Deno.test(`runners: ${vectors.runners.length} cases, initcode and gas`, () => {
  for (const v of vectors.runners) {
    const s = schedule(v.fork);
    const call = { to: checksum(v.call.to), data: data(v.call.dataLength), value: 0n };
    const token = v.payment.token === null
      ? null
      : { address: checksum(v.payment.token.address), balanceGas: v.payment.token.balanceGas };
    const payment = { token, payee: checksum(v.payment.payee), amount: BigInt(v.payment.amount) };
    const delegated = new Set<Address>(v.delegated.map(checksum));
    const runner = inCallRunner(call, payment, s, delegated);
    const what = `${v.fork} ${v.call.dataLength}B ${v.payment.amount}`;

    assertEquals(runner.initcode.length, v.initcodeLength, what);
    assertEquals(hex(keccak(runner.initcode)), v.initcodeKeccak, what);
    assertEquals(runner.overheadGas, v.overheadGas, what);
    for (const [clientGas, gas] of Object.entries(v.frameGas)) {
      assertEquals(frameGas(runner, Number(clientGas)), gas, `${what} frame ${clientGas}`);
    }
    for (const t of v.transactionGas) {
      const gas = transactionGas(
        runner.initcode,
        frameGas(runner, t.clientGas),
        s,
        t.authorizations,
        t.emptyAuthorities,
      );
      assertEquals(gas, t.gas, `${what} ${JSON.stringify(t)}`);
    }
  }
});

Deno.test("refusals: what the reference won't emit, the port won't either", () => {
  for (const v of vectors.refusals) {
    const call = {
      to: checksum(v.call.to),
      data: data(v.call.dataLength),
      value: BigInt(v.call.value),
    };
    const payment = { token: null, payee: call.to, amount: BigInt(v.amount) };
    assertThrows(
      () => inCallRunner(call, payment, schedule(v.fork)),
      RunnerError,
      undefined,
      v.name,
    );
  }
});
