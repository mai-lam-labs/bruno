/**
 * The port against the Python reference's golden vectors (bruno/tests/vectors.py, written by
 * `make vectors`): the same initcode, byte for byte, and the same gas, for every case.
 */

import { assertEquals, assertThrows } from "@std/assert";
import { marketTip, risen } from "../src/chain.ts";
import { approveCalldata, type Step, swapCalldata, withdrawCalldata } from "../src/router.ts";
import { DEPLOYER, INITCODE, overheadGas, RUNTIME, transactionGas } from "../src/deployer.ts";
import { type Address, bytes, checksum, type Hex, hex, keccak, NATIVE_ASSET } from "../src/evm.ts";
import { calldata, digest, lastBlockBefore, quotedCalldata, transferGas } from "../src/eip3009.ts";
import { type Deployment, unwrapped, WrapperError } from "../src/erc6492.ts";
import { verification } from "../src/wallets.ts";
import { submissionId } from "../src/submission.ts";
import { MalformedPaymentError, paramsOf, payloadOf } from "../src/x402.ts";
import { data, read, schedule } from "./reference.ts";
import { intrinsic } from "../src/forks.ts";
import { qr } from "../src/qr.ts";
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
  clientGas: number;
  initcodeLength: number;
  initcodeKeccak: string;
  overheadGas: number;
  frameGas: number;
  transactionGas: {
    authorizations: number;
    emptyAuthorities: number;
    gas: number;
  }[];
}

export interface Vectors {
  qr: { text: string; rows: string[] }[];
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
  tips: { tips: string[]; tip: string }[];
  router: {
    swaps: {
      name: string;
      steps: { kind: string; tokenIn: string; tokenOut: string; fee: number }[];
      amountIn: string;
      minOut: string;
      recipient: string;
      calldata: string;
    }[];
    approve: { spender: string; amount: string; calldata: string };
    withdraw: { amount: string; calldata: string };
  };
  risen: { baseFee: string; blocks: number; fee: string }[];
  runners: RunnerVector[];
  refusals: {
    name: string;
    fork: string;
    call: { to: string; dataLength: number; value: string };
    amount: string;
  }[];
  x402: {
    payloads: { name: string; header: string; taken: boolean }[];
    bodies: { name: string; body: string; call: boolean }[];
    latest: { number: number; timestamp: number };
    authorizations: {
      domain: { name: string; version: string };
      chainId: number;
      token: string;
      payer: string;
      to: string;
      value: string;
      validBefore: string;
      nonce: string;
      signature: string;
      digest: string;
      calldata: string;
      submission: { to: string; dataLength: number; gas: number };
      submissionId: string;
    }[];
    runners: {
      fork: string;
      payeeHolds: boolean;
      transferGas: number;
      calldata: string;
      initcodeKeccak: string;
      overheadGas: number;
    }[];
    deadlines: { validBefore: string; slotSeconds: number; block: number }[];
  };
  erc6492: {
    name: string;
    account: string;
    signature: string;
    refused?: boolean;
    inner?: string;
    factory?: string;
    factoryData?: string;
  }[];
  deploymentRunners: {
    fork: string;
    deployment: DeploymentVector;
    prepare: boolean;
    transfer: "none" | "bounded" | "left over";
    transferCalldata: string;
    initcodeKeccak: string;
    overheadGas: number;
  }[];
  submissionIds: {
    call: { to: string; dataLength: number };
    gas: number;
    token: string;
    payee: string;
    amount: string;
    maxBlockNumber: number;
    transfer: {
      payer: string;
      validAfter: string;
      validBefore: string;
      nonce: string;
      signature: string;
    } | null;
    deployment: DeploymentVector | null;
    id: string;
  }[];
  quotedCalldata: { signatureBytes: number | null; length: number }[];
  wallets: { fork: string; factory: string | null; gas: number; signatureBytes: number }[];
}

interface DeploymentVector {
  account: string;
  factory: string;
  factoryData: string;
}

function deploymentOf(v: DeploymentVector): Deployment {
  return {
    account: checksum(v.account),
    factory: checksum(v.factory),
    factoryData: bytes(v.factoryData as Hex),
  };
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

Deno.test("the market tip, from blocks' median tips", () => {
  for (const v of vectors.tips) {
    assertEquals(marketTip(v.tips.map(BigInt)), BigInt(v.tip), v.tips.join(","));
  }
});

Deno.test("the router's calldata: swaps along v2 and v3 legs, approve, withdraw", () => {
  for (const v of vectors.router.swaps) {
    const steps = v.steps.map((s) =>
      ({ ...s, tokenIn: checksum(s.tokenIn), tokenOut: checksum(s.tokenOut) }) as Step
    );
    const calldata = swapCalldata(
      steps,
      BigInt(v.amountIn),
      BigInt(v.minOut),
      checksum(v.recipient),
    );
    assertEquals(hex(calldata), v.calldata, `${v.name} ${v.amountIn}`);
  }
  const { approve, withdraw } = vectors.router;
  assertEquals(
    hex(approveCalldata(checksum(approve.spender), BigInt(approve.amount))),
    approve.calldata,
  );
  assertEquals(hex(withdrawCalldata(BigInt(withdraw.amount))), withdraw.calldata);
});

Deno.test("the base fee risen over blocks, rounded up", () => {
  for (const v of vectors.risen) {
    assertEquals(risen(BigInt(v.baseFee), v.blocks), BigInt(v.fee), `${v.baseFee} ${v.blocks}`);
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
    const runner = inCallRunner(call, payment, s, v.clientGas, { delegated });
    const what = `${v.fork} ${v.call.dataLength}B ${v.payment.amount} ${v.clientGas}`;

    assertEquals(runner.initcode.length, v.initcodeLength, what);
    assertEquals(hex(keccak(runner.initcode)), v.initcodeKeccak, what);
    assertEquals(runner.overheadGas, v.overheadGas, what);
    assertEquals(frameGas(runner, v.clientGas), v.frameGas, `${what} frame`);
    for (const t of v.transactionGas) {
      const gas = transactionGas(
        runner.initcode,
        frameGas(runner, v.clientGas),
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
      () => inCallRunner(call, payment, schedule(v.fork), 0),
      RunnerError,
      undefined,
      v.name,
    );
  }
});

Deno.test("qr: each version's capacity and past it, every mask, as segno makes them", () => {
  for (const { text, rows } of vectors.qr) {
    assertEquals(qr(text).map((row) => row.join("")), rows, text);
  }
});

Deno.test("x402: EIP-3009 digests, calldata, paid submissions' ids, runners and deadlines", () => {
  const x402 = vectors.x402;
  for (const v of x402.authorizations) {
    const nonce = bytes(v.nonce as Hex);
    const payer = checksum(v.payer);
    const to = checksum(v.to);
    const token = checksum(v.token);
    const value = BigInt(v.value);
    const validBefore = BigInt(v.validBefore);
    assertEquals(
      hex(digest(v.domain, v.chainId, token, payer, to, value, 0n, validBefore, nonce)),
      v.digest,
    );
    const transfer = {
      payer,
      to,
      value,
      validAfter: 0n,
      validBefore,
      nonce,
      signature: bytes(v.signature as Hex),
    };
    assertEquals(hex(calldata(transfer)), v.calldata);
    const call = {
      to: checksum(v.submission.to),
      data: data(v.submission.dataLength),
      value: 0n,
    };
    const id = submissionId({
      call,
      authorizations: [],
      gas: v.submission.gas,
      token,
      payee: to,
      amount: value,
      maxBlockNumber: 0,
      transfer,
    });
    assertEquals(hex(id), v.submissionId);
  }
  for (const v of x402.runners) {
    const s = schedule(v.fork);
    const gas = transferGas(50_000, s, v.payeeHolds);
    assertEquals(gas, v.transferGas, v.fork);
    const token = checksum(x402.authorizations[0]?.token as string);
    const runner = inCallRunner(
      { to: checksum(x402.authorizations[0]?.submission.to as string), data: data(100), value: 0n },
      {
        token: { address: token, balanceGas: 30_000, transferGas: 50_000 },
        payee: checksum(x402.authorizations[0]?.to as string),
        amount: 1n,
        transfer: { calldata: bytes(v.calldata as Hex), gas },
      },
      s,
      45_000,
    );
    assertEquals(hex(keccak(runner.initcode)), v.initcodeKeccak, v.fork);
    assertEquals(runner.overheadGas, v.overheadGas, v.fork);
  }
  const latest = {
    number: x402.latest.number,
    timestamp: x402.latest.timestamp,
    baseFeePerGas: 0n,
    nextBaseFee: 0n,
    tips: [],
  };
  for (const v of x402.deadlines) {
    assertEquals(lastBlockBefore(latest, BigInt(v.validBefore), v.slotSeconds), v.block);
  }
});

Deno.test("x402: PAYMENT-SIGNATURE headers and bodies, taken or refused as the reference does", () => {
  for (const v of vectors.x402.payloads) {
    let taken = true;
    try {
      payloadOf(v.header);
    } catch (e) {
      if (!(e instanceof MalformedPaymentError)) throw e;
      taken = false;
    }
    assertEquals(taken, v.taken, v.name);
  }
  for (const v of vectors.x402.bodies) {
    assertEquals(paramsOf(v.body) !== null, v.call, v.name);
  }
});

Deno.test("ERC-6492: signatures unwrapped, or refused, as the reference decodes them", () => {
  for (const v of vectors.erc6492) {
    const signature = bytes(v.signature as Hex);
    if (v.refused) {
      assertThrows(
        () => unwrapped(signature, checksum(v.account)),
        WrapperError,
        undefined,
        v.name,
      );
      continue;
    }
    const { signature: inner, deployment } = unwrapped(signature, checksum(v.account));
    assertEquals(hex(inner), v.inner, v.name);
    assertEquals(deployment?.factory ?? null, v.factory === undefined ? null : checksum(v.factory));
    assertEquals(deployment === null ? null : hex(deployment.factoryData), v.factoryData ?? null);
  }
});

Deno.test("runners that deploy first: initcode and gas", () => {
  for (const v of vectors.deploymentRunners) {
    const s = schedule(v.fork);
    const transferCalldata = bytes(v.transferCalldata as Hex);
    const transfer = v.transfer === "none"
      ? undefined
      : { calldata: transferCalldata, gas: v.transfer === "bounded" ? 60_000 : null };
    const runner = inCallRunner(
      {
        to: checksum(vectors.x402.authorizations[0]?.submission.to as string),
        data: data(100),
        value: 0n,
      },
      {
        token: {
          address: checksum(vectors.x402.authorizations[0]?.token as string),
          balanceGas: 30_000,
          transferGas: 50_000,
        },
        payee: checksum(vectors.x402.authorizations[0]?.to as string),
        amount: 1n,
        ...(transfer === undefined ? {} : { transfer }),
      },
      s,
      45_000,
      { deployment: deploymentOf(v.deployment), prepare: v.prepare },
    );
    const what = `${v.fork} prepare ${v.prepare} ${v.transfer}`;
    assertEquals(hex(keccak(runner.initcode)), v.initcodeKeccak, what);
    assertEquals(runner.overheadGas, v.overheadGas, what);
  }
});

Deno.test("submissions' ids, paid by a signed transfer and deploying an account", () => {
  for (const v of vectors.submissionIds) {
    const payee = checksum(v.payee);
    const amount = BigInt(v.amount);
    const transfer = v.transfer === null ? undefined : {
      payer: checksum(v.transfer.payer),
      to: payee,
      value: amount,
      validAfter: BigInt(v.transfer.validAfter),
      validBefore: BigInt(v.transfer.validBefore),
      nonce: bytes(v.transfer.nonce as Hex),
      signature: bytes(v.transfer.signature as Hex),
    };
    const id = submissionId({
      call: { to: checksum(v.call.to), data: data(v.call.dataLength), value: 0n },
      authorizations: [],
      gas: v.gas,
      token: checksum(v.token),
      payee,
      amount,
      maxBlockNumber: v.maxBlockNumber,
      ...(transfer === undefined ? {} : { transfer }),
      ...(v.deployment === null ? {} : { deployment: deploymentOf(v.deployment) }),
    });
    assertEquals(hex(id), v.id);
  }
});

Deno.test("a quote's transfer calldata, and the wallet gas schedule", () => {
  for (const v of vectors.quotedCalldata) {
    assertEquals(quotedCalldata(v.signatureBytes).length, v.length, String(v.signatureBytes));
  }
  for (const v of vectors.wallets) {
    const factory = v.factory === null ? null : checksum(v.factory);
    assertEquals(verification(factory, schedule(v.fork)), {
      gas: v.gas,
      signatureBytes: v.signatureBytes,
    });
  }
});
