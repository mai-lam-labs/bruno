/**
 * The port against the Python reference's golden vectors (bruno/tests/vectors.py, written by
 * `make vectors`): the same initcode, byte for byte, and the same gas, for every case.
 */

import { assertEquals, assertThrows } from "@std/assert";
import { marketTip, risen } from "../src/chain.ts";
import { approveCalldata, type Step, swapCalldata, withdrawCalldata } from "../src/router.ts";
import { DEPLOYER, INITCODE, overheadGas, RUNTIME, transactionGas } from "../src/deployer.ts";
import { type Address, bytes, checksum, type Hex, hex, keccak, NATIVE_ASSET } from "../src/evm.ts";
import {
  calldata,
  digest,
  lastBlockBefore,
  quotedCalldata,
  type TransferAuthorization,
  transferGas,
} from "../src/eip3009.ts";
import { type Deployment, unwrapped, WrapperError } from "../src/erc6492.ts";
import { verification } from "../src/wallets.ts";
import {
  balancesCode,
  estimated,
  feePayment,
  handleOps,
  paid,
  receiptOf,
  refusal,
  sending,
  simulation,
  simulationCalldata,
  userOperationHash,
  withOverhead,
} from "../src/userops.ts";
import { userOperation } from "../src/wire.ts";
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
    }[];
    transferGas: { fork: string; measured: number; payeeHolds: boolean; gas: number }[];
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
  transferRunners: {
    fork: string;
    call: { to: string; dataLength: number };
    clientGas: number;
    payment: {
      token: { address: string; balanceGas: number; transferGas: number };
      payee: string;
      amount: string;
    };
    transfer: { calldata: string; gas: number | null } | null;
    deployment: DeploymentVector | null;
    prepare: boolean;
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
    transfer: TransferVector | null;
    deployment: DeploymentVector | null;
    id: string;
  }[];
  quotedCalldata: { signatureBytes: number | null; length: number }[];
  userOperations: {
    calldata: {
      name: string;
      op: Record<string, unknown>;
      limit: number;
      calldata: Hex;
      overrides: unknown;
    }[];
    estimates: {
      answer: Hex;
      limit: number;
      verificationGasLimit: number;
      callGasLimit: number;
      gasLimit: number;
      preVerificationGas: number;
    }[];
    hashes: { name: string; chainId: number; hash: Hex }[];
    handleOps: { name: string; beneficiary: string; calldata: Hex }[];
    balances: { payee: string; tokens: string[]; code: Hex; sending: unknown[] };
    paid: { name: string; before: Hex; after: Hex; paid: Record<string, number> }[];
    feePayments: { name: string; eth: number; after: Hex; baseFee: number; paid: number }[];
    refusals: { name: string; revert: Hex; field: string | null }[];
    receipts: {
      name: string;
      opHash: Hex;
      receipt: Record<string, unknown>;
      read: Record<string, unknown> | null;
    }[];
  };
  wallets: {
    fork: string;
    deployment: { account: Hex; factory: Hex; factoryData: Hex } | null;
    gas: number;
    signatureBytes: number;
  }[];
}

/** A signed transfer as a submission's payment carries it. */
interface TransferVector {
  payer: string;
  validAfter: string;
  validBefore: string;
  nonce: string;
  signature: string;
}

/** `v`, a transfer to `payee` of `amount`. */
function transferOf(v: TransferVector, payee: Address, amount: bigint): TransferAuthorization {
  return {
    payer: checksum(v.payer),
    to: payee,
    value: amount,
    validAfter: BigInt(v.validAfter),
    validBefore: BigInt(v.validBefore),
    nonce: bytes(v.nonce as Hex),
    signature: bytes(v.signature as Hex),
  };
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

Deno.test("x402: EIP-3009 digests, calldata, a key's transfer's bounds, and deadlines", () => {
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
  }
  for (const v of x402.transferGas) {
    assertEquals(transferGas(v.measured, schedule(v.fork), v.payeeHolds), v.gas, v.fork);
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

Deno.test("runners paid by a signed transfer, deploying first, or both: initcode and gas", () => {
  for (const v of vectors.transferRunners) {
    const token = v.payment.token;
    const runner = inCallRunner(
      { to: checksum(v.call.to), data: data(v.call.dataLength), value: 0n },
      {
        token: {
          address: checksum(token.address),
          balanceGas: token.balanceGas,
          transferGas: token.transferGas,
        },
        payee: checksum(v.payment.payee),
        amount: BigInt(v.payment.amount),
        ...(v.transfer === null
          ? {}
          : { transfer: { calldata: bytes(v.transfer.calldata as Hex), gas: v.transfer.gas } }),
      },
      schedule(v.fork),
      v.clientGas,
      {
        deployment: v.deployment === null ? null : deploymentOf(v.deployment),
        prepare: v.prepare,
      },
    );
    const what = `${v.fork} transfer ${v.transfer?.gas} deployment ${v.deployment !== null} ` +
      `prepare ${v.prepare}`;
    assertEquals(hex(keccak(runner.initcode)), v.initcodeKeccak, what);
    assertEquals(runner.overheadGas, v.overheadGas, what);
  }
});

Deno.test("submissions' ids, paid by a signed transfer and deploying an account", () => {
  for (const v of vectors.submissionIds) {
    const payee = checksum(v.payee);
    const amount = BigInt(v.amount);
    const transfer = v.transfer === null ? undefined : transferOf(v.transfer, payee, amount);
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
    const deployment = v.deployment === null ? null : {
      account: checksum(v.deployment.account),
      factory: checksum(v.deployment.factory),
      factoryData: bytes(v.deployment.factoryData),
    };
    assertEquals(verification(deployment, schedule(v.fork)), {
      gas: v.gas,
      signatureBytes: v.signatureBytes,
    });
  }
});

Deno.test("a user operation's simulation, and its answer read as an estimate", () => {
  for (const v of vectors.userOperations.calldata) {
    const op = userOperation(v.op);
    assertEquals(hex(simulationCalldata(op, BigInt(v.limit))), v.calldata, v.name);
    assertEquals(simulation(op, BigInt(v.limit)).params[2], v.overrides, v.name);
  }
  for (const v of vectors.userOperations.estimates) {
    const estimate = estimated(bytes(v.answer), BigInt(v.limit));
    assertEquals(estimate, {
      preVerificationGas: 0n,
      verificationGasLimit: BigInt(v.verificationGasLimit),
      callGasLimit: BigInt(v.callGasLimit),
    });
    assertEquals(
      withOverhead(estimate, bytes(v.answer), BigInt(v.limit), BigInt(v.gasLimit))
        .preVerificationGas,
      BigInt(v.preVerificationGas),
    );
  }
});

Deno.test("a user operation's hash, handleOps, the payee's balances, what it paid, its receipt", () => {
  const u = vectors.userOperations;
  const ops = new Map(u.calldata.map((v) => [v.name, userOperation(v.op)]));
  for (const v of u.hashes) {
    assertEquals(hex(userOperationHash(ops.get(v.name)!, v.chainId)), v.hash, v.name);
  }
  for (const v of u.handleOps) {
    assertEquals(hex(handleOps(ops.get(v.name)!, checksum(v.beneficiary))), v.calldata, v.name);
  }
  const tokens = u.balances.tokens.map(checksum);
  const payee = checksum(u.balances.payee);
  assertEquals(hex(balancesCode(payee, tokens)), u.balances.code);
  assertEquals(
    JSON.parse(JSON.stringify(sending(ops.get("undeployed")!, payee, tokens))),
    u.balances.sending,
  );
  for (const v of u.paid) {
    const p = paid(bytes(v.before), bytes(v.after), tokens);
    assertEquals(Object.fromEntries([...p].map(([t, n]) => [t, Number(n)])), v.paid, v.name);
  }
  for (const v of u.feePayments) {
    const amount = feePayment(
      ops.get(v.name)!,
      BigInt(v.eth),
      bytes(v.after),
      BigInt(v.baseFee),
      tokens.length,
    );
    assertEquals(amount, BigInt(v.paid), v.name);
  }
  for (const v of u.refusals) {
    assertEquals(refusal(bytes(v.revert))?.field ?? null, v.field, v.name);
  }
  for (const v of u.receipts) {
    const r = receiptOf(bytes(v.opHash), v.receipt);
    assertEquals(
      r === null ? null : {
        sender: r.sender,
        nonce: Number(r.nonce),
        paymaster: r.paymaster,
        actualGasCost: Number(r.actualGasCost),
        actualGasUsed: Number(r.actualGasUsed),
        success: r.success,
        reason: hex(r.reason),
        logs: r.logs,
      },
      v.read,
      v.name,
    );
  }
});
