/**
 * The Worker against the golden vectors (bruno/tests/vectors.py, written by
 * `make vectors`): the same initcode, byte for byte, and the same gas, for every case.
 */

import { assertEquals, assertThrows } from "@std/assert";
import { fallenBaseFee, marketTip, risenBaseFee } from "../src/chain.ts";
import { approveCalldata, type Step, swapCalldata, withdrawCalldata } from "../src/router.ts";
import {
  DEPLOYER,
  overheadGas,
  runnerGasWithin,
  runnerSalt,
  RUNTIME,
  saltKey,
  STAND_IN_SALT,
  transactionGas,
} from "../src/deployer.ts";
import { type Address, bytes, checksum, type Hex, hex, keccak, NATIVE_ASSET } from "../src/evm.ts";
import { ethGas, forwarderOf, forwarding, tokenGas } from "../src/forwarder.ts";
import {
  digest,
  lastBlockBefore,
  quotedCalldata,
  type TransferAuthorization,
  transferCalldata,
  transferGas,
} from "../src/eip3009.ts";
import { type Deployment, unwrapped, WrapperError } from "../src/erc6492.ts";
import { verification } from "../src/wallets.ts";
import {
  balancesCode,
  ENTRY_POINT_0_7,
  estimated,
  estimating,
  feesPaid,
  handleOps,
  paid,
  paidAt,
  refusal,
  sending,
  used,
  userOperationHash,
  userOperationReceiptOf,
  withOverhead,
} from "../src/userops.ts";
import { userOperation } from "../src/wire.ts";
import { authorities, InvalidSubmissionError, submissionId } from "../src/submission.ts";
import { MalformedPaymentError, paramsOf, payloadOf } from "../src/x402.ts";
import { address, data, read, schedule } from "./reference.ts";
import { costWithin, feeCeiling } from "../src/pricing.ts";
import {
  basesCost,
  boundReads,
  callBound,
  CheckedByForwarder,
  checkGas,
  draftOf,
  isOwn,
  PaidByTransfer,
  planGasLimit,
  plannedCall,
  planOwn,
  proofOf,
  sponsored as sponsoredDraft,
} from "../src/plan.ts";
import { CODE_HASH, MULTICALL3, OWN_GAS } from "../src/multicall.ts";
import { revertReason } from "../src/reverts.ts";
import {
  checkedSponsorship,
  funding,
  type Sponsorship,
  sponsorshipId,
} from "../src/sponsorship.ts";
import { decoded, signer } from "../src/transactions.ts";
import { boundable, direct } from "../src/targets.ts";
import { JsonRpcError } from "../src/jsonrpc.ts";
import { intrinsic } from "../src/forks.ts";
import { qr } from "../src/qr.ts";
import {
  BALANCE_UNREADABLE,
  clientGasWithin,
  estimatingRunner,
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

interface SponsoredVector {
  name: string;
  account: string;
  authorizations: {
    chainId: string;
    address: string;
    nonce: string;
    yParity: number;
    r: string;
    s: string;
  }[];
  calls: { to: string; data: string; value: string; gas: number | null }[];
  transactions: string[];
  token: string;
  payee: string;
  amount: string;
  maxBlockNumber: number;
}

export interface Vectors {
  qr: { text: string; rows: string[] }[];
  sponsorships: {
    taken: (SponsoredVector & {
      id: string;
      funding: string;
      forwardGas: number | null;
      balanceGas: number | null;
      plans: {
        fork: string;
        accountNew: boolean;
        accountHasCode: boolean;
        delegated: string[];
        emptyAuthorities: number;
        payeeHolds: boolean;
        payeeNew: boolean;
        gasLimit: number;
        steps: ({
          to: string;
          data: string;
          value: string;
          gasLimit: number;
          size: number;
          authorizations: number;
          estimated: boolean;
        } | { signed: string })[];
      }[];
    })[];
    refused: (SponsoredVector & { field: string; reason: string })[];
    unmeasured: string;
  };
  forwarders: {
    payee: string;
    runtime: string;
    initcode: string;
    address: string;
    forwarding: { asset: string; least: string; data: string }[];
    gas: { fork: string; token: number; eth: number; ethNewPayee: number }[];
  }[];
  multicall3: { address: string; codeHash: string; ownGas: number[] };
  deployer: {
    runtime: string;
    address: string;
    standInSalt: string;
    saltKeys: { float: string; key: string }[];
    salts: { key: string; id: string; block: number; salt: string }[];
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
  sponsorshipCosts: {
    maxGas: number;
    blocks: number;
    nextBaseFee: string;
    tips: string[];
    bases: { gasLimit: number; size: number; value: string }[];
    maxFeePerGas: string;
    maxPriorityFeePerGas: string;
    most: string;
    needs: string;
    over: boolean;
  }[];
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
  risen: { baseFee: string; blocks: number; fee: string; fallen: string }[];
  runners: RunnerVector[];
  revertReasons: { data: string; refusal: string; reason: string }[];
  direct: {
    chainId: number;
    call: { to: string; data: string; value: string };
    direct: boolean;
    boundable: boolean;
  }[];
  bounds: {
    name: string;
    fork: string;
    chainId: number;
    call: { to: string; data: string };
    reads: number;
    code: string;
    held: string | null;
    authorities: string[];
    erc20: { balanceGas: number; transferGas: number | null } | null;
    bound: number | null;
    planned: ({ gas: number; bounded: boolean } | { refused: string; field: string })[];
  }[];
  estimatingRunners: {
    fork: string;
    call: { to: string; dataLength: number };
    payment: { token: { address: string; balanceGas: number } | null; payee: string };
    delegated: string[];
    deployment: DeploymentVector | null;
    initcodeKeccak: string;
    overheadGas: number;
    within: {
      authorizations: number;
      emptyAuthorities: number;
      gasLimit: number;
      runnerGas: number;
      clientGas: number;
    }[];
  }[];
  refusals: {
    name: string;
    fork: string;
    call: { to: string; dataLength: number; value: string };
    bounded: boolean;
    amount: string;
  }[];
  valueRunners: {
    fork: string;
    calls: { to: string; dataLength: number; value: string; gas: number }[];
    token: string | null;
    initcodeKeccak: string;
    overheadGas: number;
    frameGas: number;
    transactionGas: { authorizations: number; emptyAuthorities: number; gas: number }[];
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
  quotedCalldata: { signatureBytes: number | null; calldata: string }[];
  sentAs: {
    chainId: number;
    call: { to: string; data: string };
    transfer: { to: string; data: string } | null;
    deploys: boolean;
    kind: string;
  }[];
  severalCalls: {
    fork: string;
    calls: { to: string; dataLength: number; gas: number; bounded: boolean }[];
    transferCalldata: string;
    runner: { initcodeKeccak: string; overheadGas: number; frameGas: number };
    estimating: { initcodeKeccak: string; overheadGas: number };
    steps: {
      to: string;
      dataKeccak: string;
      gasLimit: number;
      size: number;
      estimated: boolean;
    }[];
  }[];
  transferPairs: {
    fork: string;
    call: { to: string; dataLength: number };
    transfer: { calldata: string; gas: number | null };
    authorities: string[];
    emptyAuthorities: number;
    steps: { to: string; data: string; gasLimit: number; size: number; estimated: boolean }[];
    callGas: { estimate: number; gas: number }[];
  }[];
  userOperations: {
    calldata: {
      name: string;
      entryPoint: string;
      op: Record<string, unknown>;
      limit: number;
      calls: { data: Hex; overrides: unknown }[];
    }[];
    estimates: {
      name: string;
      answers: Hex[];
      limit: number;
      verificationGasLimit: number;
      callGasLimit: number;
      paymasterVerificationGasLimit: number | null;
      paymasterPostOpGasLimit: number | null;
      gasLimit: number;
      preVerificationGas: number;
    }[];
    hashes: { name: string; chainId: number; delegate: Hex | null; hash: Hex }[];
    handleOps: { name: string; beneficiary: string; calldata: Hex }[];
    balances: { payee: string; tokens: string[]; code: Hex; block: string; sending: unknown[] };
    paid: { name: string; before: Hex; after: Hex; paid: Record<string, number> }[];
    feePayments: {
      name: string;
      runs: { eth: number; after: Hex }[];
      gas: number;
      baseFee: number;
      paid: number;
      paidAnyway: number;
    }[];
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

function sponsored(v: SponsoredVector): Sponsorship {
  return {
    account: checksum(v.account),
    authorizations: v.authorizations.map((a) => ({
      chainId: BigInt(a.chainId),
      address: checksum(a.address),
      nonce: BigInt(a.nonce),
      yParity: a.yParity,
      r: BigInt(a.r),
      s: BigInt(a.s),
    })),
    calls: v.calls.map((each) => ({
      call: { to: checksum(each.to), data: bytes(each.data as Hex), value: BigInt(each.value) },
      gas: each.gas,
    })),
    transactions: v.transactions.map((raw) => decoded(bytes(raw as Hex))),
    token: checksum(v.token),
    payee: checksum(v.payee),
    amount: BigInt(v.amount),
    maxBlockNumber: v.maxBlockNumber,
  };
}

Deno.test("sponsorships: each its id, its funding and its plan, or refused", () => {
  const { taken, refused, unmeasured } = vectors.sponsorships;
  for (const v of taken) {
    const sponsorship = checkedSponsorship(sponsored(v));
    assertEquals(hex(sponsorshipId(sponsorship)), v.id, v.name);
    assertEquals(funding(sponsorship).toString(), v.funding, v.name);
    const erc20 = v.forwardGas === null || v.balanceGas === null
      ? null
      : { address: sponsorship.token, balanceGas: v.balanceGas, forwardGas: v.forwardGas };
    for (const p of v.plans) {
      const what = `${v.name}, ${p.fork}, ${JSON.stringify([p.accountNew, p.payeeHolds])}`;
      const accounts = {
        delegated: new Set(p.delegated.map(checksum)),
        empty: p.emptyAuthorities,
        delegating: new Set<Address>(),
      };
      const proof = new CheckedByForwarder();
      const draft = sponsoredDraft(
        sponsorship,
        proof,
        authorities(sponsorship.authorizations),
        accounts,
        erc20,
        schedule(p.fork),
        sponsorship.calls.map(({ call, gas }) => ({ call, gas: gas ?? 0, bounded: false })),
        p,
      );
      const plan = proof.plan(11_155_111, draft, schedule(p.fork));
      assertEquals(planGasLimit(plan), p.gasLimit, what);
      assertEquals(
        plan.steps.map((step) =>
          isOwn(step)
            ? {
              to: step.transaction.to,
              data: hex(step.transaction.data),
              value: step.transaction.value.toString(),
              gasLimit: step.gasLimit,
              size: step.size,
              authorizations: step.authorizations.length,
              estimated: step.estimated,
            }
            : { signed: hex(step.transaction.signed.hash) }
        ),
        p.steps.map((step) => "to" in step ? { ...step, to: checksum(step.to) } : step),
        what,
      );
    }
  }
  for (const v of refused) {
    const e = assertThrows(
      () => checkedSponsorship(sponsored(v)),
      InvalidSubmissionError,
      undefined,
      v.name,
    );
    assertEquals([e.field, e.refused.refusal], [v.field, v.reason], v.name);
  }
  const prague = schedule("prague");
  const token = checksum(taken[0]?.token ?? "");
  const e = assertThrows(
    () =>
      checkGas(prague, { address: token, balanceGas: 2_600 }, {
        payeeHolds: true,
        payeeNew: false,
      }),
    InvalidSubmissionError,
  );
  assertEquals(e.refused.refusal, unmeasured);
});

Deno.test("forwarder: code, address, calls and its own gas", () => {
  for (const v of vectors.forwarders) {
    const forwarder = forwarderOf(checksum(v.payee));
    assertEquals(hex(forwarder.runtime), v.runtime);
    assertEquals(hex(forwarder.initcode), v.initcode);
    assertEquals(forwarder.address, v.address);
    for (const f of v.forwarding) {
      const call = forwarding(forwarder.address, checksum(f.asset), BigInt(f.least));
      assertEquals(call.to, v.address);
      assertEquals(hex(call.data), f.data, f.asset);
    }
    for (const g of v.gas) {
      assertEquals(tokenGas(schedule(g.fork)), g.token, g.fork);
      assertEquals(ethGas(schedule(g.fork), false), g.eth, g.fork);
      assertEquals(ethGas(schedule(g.fork), true), g.ethNewPayee, g.fork);
    }
  }
});

Deno.test("Multicall3: its address, its code's hash and its own gas", () => {
  assertEquals(MULTICALL3, checksum(vectors.multicall3.address));
  assertEquals(hex(CODE_HASH), vectors.multicall3.codeHash);
  assertEquals(OWN_GAS, vectors.multicall3.ownGas);
});

Deno.test("deployer: code, address, its runners' salts and its own gas", () => {
  assertEquals(hex(RUNTIME), vectors.deployer.runtime);
  assertEquals(DEPLOYER, vectors.deployer.address);
  assertEquals(hex(STAND_IN_SALT), vectors.deployer.standInSalt);
  for (const v of vectors.deployer.saltKeys) {
    assertEquals(hex(saltKey(signer(bytes(v.float as Hex)))), v.key, v.float);
  }
  for (const v of vectors.deployer.salts) {
    const salt = runnerSalt(bytes(v.key as Hex), bytes(v.id as Hex), v.block);
    assertEquals(hex(salt), v.salt, JSON.stringify(v));
  }
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

Deno.test("what a sponsorship may cost: the ceiling, the most, what it needs, over or not", () => {
  for (const v of vectors.sponsorshipCosts) {
    const latest = {
      number: 100,
      timestamp: 1_700_000_000,
      nextBaseFee: BigInt(v.nextBaseFee),
      tips: v.tips.map(BigInt),
    };
    const ceiling = feeCeiling(latest, v.blocks);
    const bases = v.bases.map((b) => ({ ...b, value: BigInt(b.value) }));
    assertEquals(ceiling.maxFeePerGas, BigInt(v.maxFeePerGas));
    assertEquals(ceiling.maxPriorityFeePerGas, BigInt(v.maxPriorityFeePerGas));
    assertEquals(basesCost(bases, ceiling), BigInt(v.needs));
    if (!v.over) {
      costWithin(bases, ceiling, v.maxGas, "transactions");
      continue;
    }
    const e = assertThrows(
      () => costWithin(bases, ceiling, v.maxGas, "transactions"),
      InvalidSubmissionError,
    );
    assertEquals(e.field, "transactions");
    assertEquals(e.refused.refusal, "cost-over");
    assertEquals(e.bound, { most: BigInt(v.most), needs: BigInt(v.needs) });
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

Deno.test("the base fee risen over blocks, rounded up, and fallen, rounded down", () => {
  for (const v of vectors.risen) {
    assertEquals(
      risenBaseFee(BigInt(v.baseFee), v.blocks),
      BigInt(v.fee),
      `${v.baseFee} ${v.blocks}`,
    );
    assertEquals(
      fallenBaseFee(BigInt(v.baseFee), v.blocks),
      BigInt(v.fallen),
      `${v.baseFee} ${v.blocks}`,
    );
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
    const calls = [{ call, gas: v.clientGas, bounded: false }];
    const runner = inCallRunner(calls, payment, s, { delegated });
    const what = `${v.fork} ${v.call.dataLength}B ${v.payment.amount} ${v.clientGas}`;

    assertEquals(runner.initcode.length, v.initcodeLength, what);
    assertEquals(hex(keccak(runner.initcode)), v.initcodeKeccak, what);
    assertEquals(runner.overheadGas, v.overheadGas, what);
    assertEquals(frameGas(runner, calls), v.frameGas, `${what} frame`);
    for (const t of v.transactionGas) {
      const gas = transactionGas(
        runner.initcode,
        frameGas(runner, calls),
        s,
        t.authorizations,
        t.emptyAuthorities,
      );
      assertEquals(gas, t.gas, `${what} ${JSON.stringify(t)}`);
    }
  }
});

Deno.test(`estimating runners: ${vectors.estimatingRunners.length} cases, initcode and gas within`, () => {
  for (const v of vectors.estimatingRunners) {
    const s = schedule(v.fork);
    const token = v.payment.token === null
      ? null
      : { address: checksum(v.payment.token.address), balanceGas: v.payment.token.balanceGas };
    const estimated = { to: checksum(v.call.to), data: data(v.call.dataLength), value: 0n };
    const runner = estimatingRunner(
      [{ call: estimated, gas: 0, bounded: false }],
      { token, payee: checksum(v.payment.payee), amount: 1n },
      s,
      {
        delegated: new Set<Address>(v.delegated.map(checksum)),
        deployment: v.deployment === null ? null : deploymentOf(v.deployment),
      },
    );
    const what = `${v.fork} ${v.call.dataLength}B ${v.delegated.length} ${v.deployment !== null}`;
    assertEquals(hex(keccak(runner.initcode)), v.initcodeKeccak, what);
    assertEquals(runner.overheadGas, v.overheadGas, what);
    for (const w of v.within) {
      const runnerGas = runnerGasWithin(
        runner.initcode,
        w.gasLimit,
        s,
        w.authorizations,
        w.emptyAuthorities,
      );
      assertEquals(runnerGas, w.runnerGas, `${what} ${JSON.stringify(w)}`);
      assertEquals(clientGasWithin(runnerGas - runner.overheadGas), w.clientGas, what);
    }
  }
});

Deno.test(`revert reasons: ${vectors.revertReasons.length} cases, the same refusal`, () => {
  for (const v of vectors.revertReasons) {
    const refused = revertReason(bytes(v.data as Hex));
    assertEquals(refused.refusal, v.refusal, v.data);
    assertEquals(String(refused), v.reason, v.data);
  }
});

Deno.test(`direct: ${vectors.direct.length} calls, sent directly as bruno/targets.py has it`, () => {
  for (const v of vectors.direct) {
    const call = {
      to: checksum(v.call.to),
      data: bytes(v.call.data as Hex),
      value: BigInt(v.call.value),
    };
    assertEquals(direct(v.chainId, call), v.direct, `${v.chainId} ${v.call.to} ${v.call.data}`);
    assertEquals(boundable(v.chainId, call), v.boundable, `${v.chainId} ${v.call.to}`);
  }
});

Deno.test(`bounds: ${vectors.bounds.length} calls, bounded and planned as bruno/plan.py has them`, () => {
  for (const v of vectors.bounds) {
    const what = `${v.fork}: ${v.name}`;
    const call = { to: checksum(v.call.to), data: bytes(v.call.data as Hex), value: 0n };
    assertEquals(boundReads(v.chainId, call, "latest").length, v.reads, what);
    const unread = new JsonRpcError("eth_call", -32000, "unread", null);
    const answers = [{ result: v.code }, v.held === null ? { error: unread } : { result: v.held }];
    const erc20 = v.erc20 === null ? null : {
      address: call.to,
      balanceGas: v.erc20.balanceGas,
      ...(v.erc20.transferGas === null ? {} : { transferGas: v.erc20.transferGas }),
    };
    const tokens = new Map(erc20 === null ? [] : [[call.to, erc20]]);
    const bound = callBound(
      v.chainId,
      call,
      answers.slice(0, v.reads),
      tokens,
      schedule(v.fork),
      v.authorities.map(checksum),
    );
    assertEquals(bound, v.bound, what);
    const planned = [60_000, null].map((gas) => {
      try {
        const { gas: given, bounded } = plannedCall(0, { call, gas }, bound);
        return { gas: given, bounded };
      } catch (e) {
        if (!(e instanceof InvalidSubmissionError)) throw e;
        return { refused: e.refused.refusal, field: e.field };
      }
    });
    assertEquals(planned, v.planned, what);
  }
});

Deno.test("refusals: what bruno/runner.py won't emit, runner.ts won't either", () => {
  for (const v of vectors.refusals) {
    const call = {
      to: checksum(v.call.to),
      data: data(v.call.dataLength),
      value: BigInt(v.call.value),
    };
    const payment = { token: null, payee: call.to, amount: BigInt(v.amount) };
    assertThrows(
      () => inCallRunner([{ call, gas: 0, bounded: v.bounded }], payment, schedule(v.fork)),
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
    assertEquals(hex(transferCalldata(transfer)), v.calldata);
  }
  for (const v of x402.transferGas) {
    assertEquals(transferGas(v.measured, schedule(v.fork), v.payeeHolds), v.gas, v.fork);
  }
  const latest = {
    number: x402.latest.number,
    timestamp: x402.latest.timestamp,
    nextBaseFee: 0n,
    tips: [],
  };
  for (const v of x402.deadlines) {
    assertEquals(lastBlockBefore(latest, BigInt(v.validBefore), v.slotSeconds), v.block);
  }
});

Deno.test("x402: PAYMENT-SIGNATURE headers and bodies, taken or refused as bruno/server/x402.py does", () => {
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

Deno.test("ERC-6492: signatures unwrapped, or refused, as bruno/erc6492.py decodes them", () => {
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
    const made = { to: checksum(v.call.to), data: data(v.call.dataLength), value: 0n };
    const runner = inCallRunner(
      [{ call: made, gas: v.clientGas, bounded: false }],
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
      calls: [{
        call: { to: checksum(v.call.to), data: data(v.call.dataLength), value: 0n },
        gas: v.gas,
      }],
      authorizations: [],
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

Deno.test(`sent as: ${vectors.sentAs.length} calls, each by the proof bruno/plan.py gives it`, () => {
  const deployment = {
    account: address("account"),
    factory: address("factory"),
    factoryData: data(100),
  };
  const encoded = (call: { to: string; data: string }) => ({
    to: checksum(call.to),
    data: bytes(call.data as Hex),
    value: 0n,
  });
  for (const v of vectors.sentAs) {
    const proof = proofOf(v.chainId, [encoded(v.call)], {
      transfer: v.transfer === null ? null : encoded(v.transfer),
      deployment: v.deploys ? deployment : null,
    });
    assertEquals(proof.stored().kind, v.kind, JSON.stringify(v));
  }
});

Deno.test(`runners whose calls send ETH: ${vectors.valueRunners.length} cases, initcode and gas`, () => {
  for (const v of vectors.valueRunners) {
    const planned = v.calls.map((each) => ({
      call: { to: checksum(each.to), data: data(each.dataLength), value: BigInt(each.value) },
      gas: each.gas,
      bounded: false,
    }));
    const token = v.token === null ? null : { address: checksum(v.token), balanceGas: 30_000 };
    const payment = { token, payee: address("payee"), amount: 1_000_000n };
    const runner = inCallRunner(planned, payment, schedule(v.fork));
    const label = `${v.fork} ${JSON.stringify(v.calls.map((each) => each.value))} ${v.token}`;
    assertEquals(hex(keccak(runner.initcode)), v.initcodeKeccak, label);
    assertEquals(runner.overheadGas, v.overheadGas, label);
    assertEquals(frameGas(runner, planned), v.frameGas, label);
    for (const t of v.transactionGas) {
      const gas = transactionGas(
        runner.initcode,
        frameGas(runner, planned),
        schedule(v.fork),
        t.authorizations,
        t.emptyAuthorities,
        true,
      );
      assertEquals(gas, t.gas, `${label} ${t.authorizations} ${t.emptyAuthorities}`);
    }
  }
});

Deno.test(`several calls: ${vectors.severalCalls.length} cases, their runner and sent directly`, () => {
  const proof = new PaidByTransfer();
  const token = address("token");
  const authority = address("authority 0");
  for (const v of vectors.severalCalls) {
    const s = schedule(v.fork);
    const planned = v.calls.map((each) => ({
      call: { to: checksum(each.to), data: data(each.dataLength), value: 0n },
      gas: each.gas,
      bounded: each.bounded,
    }));
    const erc20 = { address: token, balanceGas: 30_000, transferGas: 50_000 };
    const transfer = { calldata: bytes(v.transferCalldata as Hex), gas: 50_000 };
    const payment = { token: erc20, payee: address("payee"), amount: 1_000_000n, transfer };
    const label = `${v.fork} ${JSON.stringify(v.calls.map((each) => each.gas))}`;

    const runner = inCallRunner(planned, payment, s);
    assertEquals(hex(keccak(runner.initcode)), v.runner.initcodeKeccak, label);
    assertEquals(runner.overheadGas, v.runner.overheadGas, label);
    assertEquals(frameGas(runner, planned), v.runner.frameGas, label);
    const estimating = estimatingRunner(planned, payment, s);
    assertEquals(hex(keccak(estimating.initcode)), v.estimating.initcodeKeccak, label);
    assertEquals(estimating.overheadGas, v.estimating.overheadGas, label);

    const submission = {
      calls: planned.map((each) => ({ call: each.call, gas: each.gas })),
      authorizations: [],
      token,
      payee: address("payee"),
      amount: 1_000_000n,
      maxBlockNumber: 0,
    };
    const accounts = {
      delegated: new Set([authority]),
      empty: 1,
      delegating: new Set<Address>(),
    };
    const draft = {
      ...draftOf(submission, proof, [authority], accounts, erc20, planned),
      transfer,
    };
    const steps = planOwn(proof.plan(11_155_111, draft, s)).map((step) => ({
      to: step.transaction.to,
      dataKeccak: hex(keccak(step.transaction.data)),
      gasLimit: step.gasLimit,
      size: step.size,
      estimated: step.estimated,
    }));
    assertEquals(steps, v.steps.map((step) => ({ ...step, to: checksum(step.to) })), label);
  }
});

Deno.test(`a call and its transfer sent directly: ${vectors.transferPairs.length} pairs and their gas`, () => {
  const proof = new PaidByTransfer();
  const token = address("token");
  for (const v of vectors.transferPairs) {
    const s = schedule(v.fork);
    const call = { to: checksum(v.call.to), data: data(v.call.dataLength), value: 0n };
    const authorities = v.authorities.map(checksum);
    const submission = {
      calls: [{ call, gas: 45_000 }],
      authorizations: [],
      token,
      payee: address("payee"),
      amount: 1_000_000n,
      maxBlockNumber: 0,
    };
    const accounts = {
      delegated: new Set(authorities),
      empty: v.emptyAuthorities,
      delegating: new Set<Address>(),
    };
    const erc20 = { address: token, balanceGas: 30_000, transferGas: 50_000 };
    const draft = {
      ...draftOf(submission, proof, authorities, accounts, erc20, [
        { call, gas: 45_000, bounded: false },
      ]),
      transfer: { calldata: bytes(v.transfer.calldata as Hex), gas: v.transfer.gas },
    };
    const label = `${v.fork} ${v.call.dataLength} ${v.transfer.gas} ${v.authorities.length}`;
    const steps = planOwn(proof.plan(11_155_111, draft, s)).map((step) => ({
      to: step.transaction.to,
      data: hex(step.transaction.data),
      gasLimit: step.gasLimit,
      size: step.size,
      estimated: step.estimated,
    }));
    assertEquals(steps, v.steps.map((step) => ({ ...step, to: checksum(step.to) })), label);
    for (const { estimate, gas } of v.callGas) {
      assertEquals(proof.callGasOf(estimate, draft, 0, s), gas, `${label} ${estimate}`);
    }
  }
});

Deno.test("a quote's transfer calldata, and the wallet gas schedule", () => {
  for (const v of vectors.quotedCalldata) {
    assertEquals(hex(quotedCalldata(v.signatureBytes)), v.calldata, String(v.signatureBytes));
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

Deno.test("a user operation's simulations, and their answers read as an estimate", () => {
  const ops = new Map(
    vectors.userOperations.calldata.map((
      v,
    ) => [v.name, userOperation(v.op, checksum(v.entryPoint))]),
  );
  for (const v of vectors.userOperations.calldata) {
    const op = userOperation(v.op, checksum(v.entryPoint));
    const calls = estimating(op, BigInt(v.limit), "latest").map((c) => ({
      data: (c.params[0] as { data: Hex }).data,
      overrides: c.params[2],
    }));
    assertEquals(calls, v.calls, v.name);
  }
  for (const v of vectors.userOperations.estimates) {
    const op = ops.get(v.name)!;
    const gas = used(v.answers.map((a) => bytes(a)), op, BigInt(v.limit));
    const estimate = estimated(gas, op);
    const orNull = (n: number | null): bigint | null => (n === null ? null : BigInt(n));
    assertEquals(estimate, {
      preVerificationGas: 0n,
      verificationGasLimit: BigInt(v.verificationGasLimit),
      callGasLimit: BigInt(v.callGasLimit),
      paymasterVerificationGasLimit: orNull(v.paymasterVerificationGasLimit),
      paymasterPostOpGasLimit: orNull(v.paymasterPostOpGasLimit),
    }, v.name);
    assertEquals(
      withOverhead(estimate, gas, BigInt(v.gasLimit)).preVerificationGas,
      BigInt(v.preVerificationGas),
      v.name,
    );
  }
});

Deno.test("a user operation's hash, handleOps, the payee's balances, what it paid, its receipt", () => {
  const u = vectors.userOperations;
  const ops = new Map(
    u.calldata.map((v) => [v.name, userOperation(v.op, checksum(v.entryPoint))]),
  );
  for (const v of u.hashes) {
    const delegate = v.delegate === null ? null : bytes(v.delegate);
    assertEquals(hex(userOperationHash(ops.get(v.name)!, v.chainId, delegate)), v.hash, v.name);
  }
  for (const v of u.handleOps) {
    assertEquals(hex(handleOps(ops.get(v.name)!, checksum(v.beneficiary))), v.calldata, v.name);
  }
  const tokens = u.balances.tokens.map(checksum);
  const payee = checksum(u.balances.payee);
  assertEquals(hex(balancesCode(ENTRY_POINT_0_7, payee, tokens)), u.balances.code);
  const names = ["undeployed", "an EIP-7702 account"];
  assertEquals(
    JSON.parse(
      JSON.stringify(
        names.flatMap((name) =>
          sending(ops.get(name)!, payee, tokens, u.balances.block).map((c) => ({ name, ...c }))
        ),
      ),
    ),
    u.balances.sending,
  );
  for (const v of u.paid) {
    const p = paid(bytes(v.before), bytes(v.after), tokens);
    assertEquals(Object.fromEntries([...p].map(([t, n]) => [t, Number(n)])), v.paid, v.name);
  }
  for (const v of u.feePayments) {
    const runs = v.runs.map((r) => [BigInt(r.eth), bytes(r.after)] as const);
    const payment = feesPaid(ops.get(v.name)!, runs, tokens.length);
    assertEquals(payment.gas, BigInt(v.gas), v.name);
    assertEquals(paidAt(payment, BigInt(v.baseFee)), BigInt(v.paid), v.name);
    assertEquals(paidAt(payment, 0n), BigInt(v.paidAnyway), v.name);
  }
  for (const v of u.refusals) {
    assertEquals(refusal(bytes(v.revert))?.field ?? null, v.field, v.name);
  }
  for (const v of u.receipts) {
    const r = userOperationReceiptOf(bytes(v.opHash), v.receipt);
    assertEquals(
      r === null ? null : {
        entryPoint: r.entryPoint,
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
