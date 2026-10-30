/**
 * The relay as an ERC-4337 bundler (ERC-7769): a UserOp estimated, priced, sent as a submission
 * whose call is the EntryPoint's handleOps of it, and its receipt read. A Bundler holds its relay
 * and keeps nothing of its own. A port of bruno/bundler.py.
 */

import { ChainError, fallen, hexOf, quantity, resultOf, REVERTED } from "./chain.ts";
import { type Address, bytes, type EncodedCall, type Hex, hex, NATIVE_ASSET } from "./evm.ts";
import { type GasSchedule, intrinsic } from "./forks.ts";
import { empty, runnerPlan, SimulationFailedError } from "./plan.ts";
import {
  feeCeiling,
  MAX_AMOUNT,
  type Quotes,
  type Relay,
  scheduleOf,
  UnsupportedError,
} from "./relay.ts";
import { inCallRunner, type Payment } from "./runner.ts";
import { authorities, checkSigned, InvalidSubmissionError } from "./submission.ts";
import {
  delegateOf,
  estimated,
  estimating,
  type FeePayment,
  feesPaid,
  type GasEstimate,
  gasEstimate,
  gasOf,
  type GasUsed,
  handleOps,
  InvalidUserOperationError,
  isEip7702,
  paid,
  paidAt,
  paysByFees,
  priceOf,
  receiptOf as userOperationReceiptOf,
  refusal,
  sending,
  simulationCalldata,
  used,
  type UserOperation,
  userOperationHash,
  type UserOperationReceipt,
  withOverhead,
} from "./userops.ts";

/** `relay`, answering ERC-7769's methods and the gas price ones (bruno/bundler.py's Bundler). */
export class Bundler {
  constructor(private readonly relay: Relay) {}

  /**
   * Take `op` (signed) as a submission whose call is the EntryPoint's handleOps of it, the payee
   * its beneficiary: its hash (ERC-7769's eth_sendUserOperation). What it pays is found by running
   * it through the EntryPoint's handleOps, the payee's balances read before and after, in the
   * batch that also estimates handleOps' gas: paying by its fees, the ETH its gas costs at its
   * price in the next block. As bruno/bundler.py's send_user_operation.
   */
  async sendUserOperation(op: UserOperation): Promise<Hex> {
    this.userOperationLimits(op);
    this.authorized(op);
    if (op.paymaster !== null) {
      const given = [
        ["paymasterVerificationGasLimit", op.paymaster.verificationGasLimit],
        ["paymasterPostOpGasLimit", op.paymaster.postOpGasLimit],
      ] as const;
      for (const [field, value] of given) {
        if (value === null) {
          throw new InvalidUserOperationError(field, "none: a signed op gives its limits");
        }
      }
    }
    const opHash = hex(userOperationHash(op, this.relay.chain.chainId, await this.delegate(op)));
    if (this.relay.operation(opHash) !== null) return opHash;
    const tokens = this.relay.terms().tokens;
    const [before, ran, priced, estimate] = await this.relay.chain.batch(
      sending(op, this.relay.payee, tokens),
    );
    if (
      before === undefined || ran === undefined || priced === undefined || estimate === undefined
    ) {
      throw new ChainError("no answer");
    }
    for (const answer of [ran, priced, estimate]) {
      if ("error" in answer) {
        if (answer.error.code !== REVERTED) throw answer.error;
        const data = typeof answer.error.data === "string" ? answer.error.data : "0x";
        const revert = bytes(data as Hex);
        throw refusal(revert) ?? new SimulationFailedError(revert);
      }
    }
    const start = bytes(hexOf(resultOf(before)));
    const after = bytes(hexOf(resultOf(ran)));
    const payments = paid(start, after, tokens);
    const latest = await this.relay.latest();
    let token: Address;
    let amount: bigint;
    let feePayment: FeePayment | null = null;
    if (paysByFees(op)) {
      if (!tokens.includes(NATIVE_ASSET)) {
        throw new UnsupportedError("ETH isn't taken: an op paying by its fees pays in it");
      }
      const afterPriced = bytes(hexOf(resultOf(priced)));
      const runs = [
        [payments.get(NATIVE_ASSET) ?? 0n, after],
        [paid(start, afterPriced, tokens).get(NATIVE_ASSET) ?? 0n, afterPriced],
      ] as const;
      feePayment = feesPaid(op, runs, tokens.length);
      amount = paidAt(feePayment, 0n);
      token = NATIVE_ASSET;
    } else {
      const first = [...payments].find(([, paying]) => paying > 0n);
      if (first === undefined) {
        throw new InvalidUserOperationError(
          "callData",
          "pays the relay's payee nothing, in any token it takes",
        );
      }
      [token, amount] = first;
    }
    const data = handleOps(op, this.relay.payee);
    const schedule = scheduleOf(await this.relay.available());
    const head = latest.number;
    await this.relay.submit(
      {
        call: { to: op.entryPoint.address, data, value: 0n },
        authorizations: op.authorization === null ? [] : [op.authorization],
        gas: Number(quantity(resultOf(estimate))) - intrinsic(schedule, data).standard,
        token,
        payee: this.relay.payee,
        amount,
        maxBlockNumber: head + this.relay.config.maxDeadlineBlocks,
      },
      opHash,
      feePayment,
    );
    return opHash;
  }

  /**
   * ERC-7769's receipt of the UserOp `opHash` this relay sent: null until it's included (or if
   * it never is, or was forgotten); once it is, read from its transaction's receipt, nothing
   * stored of it.
   */
  async userOperationReceipt(opHash: Hex): Promise<UserOperationReceipt | null> {
    const id = this.relay.operation(opHash);
    if (id === null) return null;
    const finished = this.relay.status(id);
    if (finished.status !== "included") return null;
    if (finished.transactionHash === null) return null;
    const receipt = await this.relay.chain.rpc("eth_getTransactionReceipt", [
      finished.transactionHash,
    ]);
    if (receipt === null || typeof receipt !== "object") return null;
    return userOperationReceiptOf(bytes(opHash), receipt as Record<string, unknown>);
  }

  /** What's refused of an op reading nothing: an EIP-7702 account's for EntryPoint v0.7, one with
   * a paymaster paying nothing by its fees, or call or factory data longer than a call's may be. */
  private userOperationLimits(op: UserOperation): void {
    if (isEip7702(op) && !op.entryPoint.eip712) {
      throw new InvalidUserOperationError("factory", "0x7702 is EntryPoint v0.8's");
    }
    if (op.paymaster !== null && !paysByFees(op)) {
      throw new InvalidUserOperationError(
        "maxFeePerGas",
        "zero: an op with a paymaster pays the relay by its fees",
      );
    }
    const most = this.relay.config.maxCallBytes;
    for (
      const [field, value] of [["callData", op.callData], ["factoryData", op.factoryData]] as const
    ) {
      if (value.length > most) throw new InvalidUserOperationError(field, `over ${most} bytes`);
    }
  }

  /** An authorization `op` brings signed by its account, for this chain or any: else
   * InvalidUserOperationError. */
  private authorized(op: UserOperation): void {
    const auth = op.authorization;
    if (auth === null) return;
    if (auth.chainId !== 0n && auth.chainId !== BigInt(this.relay.chain.chainId)) {
      throw new InvalidUserOperationError("eip7702Auth", `chain id ${auth.chainId}`);
    }
    let authority: Address;
    try {
      checkSigned(auth.yParity, auth.r, auth.s, auth.nonce);
      [authority] = authorities([auth]) as [Address];
    } catch (e) {
      if (e instanceof InvalidSubmissionError) {
        throw new InvalidUserOperationError("eip7702Auth", e.reason);
      }
      throw e;
    }
    if (authority !== op.sender) {
      throw new InvalidUserOperationError("eip7702Auth", "not signed by the sender");
    }
  }

  /** What an EIP-7702 account's `op` is hashed with: its authorization's address, else what its
   * account's code delegates to (one read). InvalidUserOperationError if it delegates to nothing;
   * null for any other account's op. */
  private async delegate(op: UserOperation): Promise<Uint8Array | null> {
    if (!isEip7702(op)) return null;
    if (op.authorization !== null) return bytes(op.authorization.address);
    const code = await this.relay.chain.rpc("eth_getCode", [op.sender, "latest"]);
    const delegate = typeof code === "string" ? delegateOf(code as Hex) : null;
    if (delegate === null) {
      throw new InvalidUserOperationError("eip7702Auth", "none, and the sender isn't delegated");
    }
    return delegate;
  }

  /**
   * `op`'s gas limits, as ERC-7769's eth_estimateUserOperationGas answers them, as used and within
   * have it. An op paying by its fees is charged, at the price its fees give at the least the base
   * fee can fall to by the quote's last block, as much gas as a quote of its transaction comes to:
   * the rest, beyond what it uses, is its preVerificationGas, whatever its fees (gas it's charged,
   * not gas its transaction uses). Fees paying nothing a gas then are invalid params.
   */
  async estimateUserOperation(op: UserOperation): Promise<GasEstimate> {
    const gasUsed = await this.used(op);
    const estimate = estimated(gasUsed, op);
    const needs = this.within(estimate);
    if (!paysByFees(op)) return estimate;
    const measured = await this.relay.available();
    this.relay.supported(measured, NATIVE_ASSET);
    const ofSize = sized(op, estimate);
    const call: EncodedCall = {
      to: op.entryPoint.address,
      data: handleOps(ofSize, this.relay.payee),
      value: 0n,
    };
    const schedule = scheduleOf(measured);
    const gas = await this.callGas(ofSize, needs, schedule);
    const payment: Payment = { token: null, payee: this.relay.payee, amount: MAX_AMOUNT };
    const runner = inCallRunner(call, payment, schedule, gas, {});
    // an authorization's account, holding ETH to pay the op's fees, isn't empty
    const authorizations = op.authorization === null ? 0 : 1;
    const plan = runnerPlan(this.relay.chain.chainId, runner, gas, schedule, authorizations, 0, {});
    this.relay.fits(plan);
    const latest = await this.relay.latest();
    const fees = feeCeiling(latest, this.relay.config.quoteBlocks - 1);
    const quoted = await this.relay.charge(measured, latest, NATIVE_ASSET, plan, fees);
    // its price at the least the base fee can be by the quote's last block (bruno/bundler.py)
    const price = priceOf(op, fallen(latest.nextBaseFee, this.relay.config.quoteBlocks - 1));
    const ceil = (a: bigint, b: bigint): bigint => (a + b - 1n) / b;
    if (price === 0n) {
      throw new InvalidUserOperationError(
        "maxFeePerGas",
        "pays nothing a gas as the base fee may fall",
      );
    }
    return withOverhead(estimate, gasUsed, ceil(quoted, price));
  }

  /**
   * What `op`, paying by its calls, is to pay in each token the relay takes and hasn't suspended,
   * for it to be signed once: its payment a placeholder, which only its amount then replaces. Its
   * transaction planned as its send plans it, with its limits as its estimate gives them and the
   * gas a node estimates of its simulation, its authority's account read to tell whether it's
   * empty, and 32 of its handleOps' zero bytes priced as other bytes; then priced as a quote is.
   * Refused as an estimate is, and an op paying by its fees or with a paymaster. As
   * bruno/bundler.py's quote_user_operation.
   */
  async quoteUserOperation(op: UserOperation): Promise<Quotes> {
    if (paysByFees(op)) {
      throw new InvalidUserOperationError(
        "maxFeePerGas",
        "above zero: an op paying by its fees is priced by its estimate",
      );
    }
    const estimate = estimated(await this.used(op), op);
    const needs = this.within(estimate);
    const measured = await this.relay.available();
    const schedule = scheduleOf(measured);
    const ofSize = sized(op, estimate);
    const gas = await this.callGas(ofSize, needs, schedule);
    const call: EncodedCall = {
      to: op.entryPoint.address,
      data: amountRoom(handleOps(ofSize, this.relay.payee)),
      value: 0n,
    };
    const authorities = op.authorization === null ? [] : [op.sender];
    const empties = await empty(this.relay.chain, authorities);
    const latest = await this.relay.latest();
    const quoteBlocks = this.relay.config.quoteBlocks;
    const fees = feeCeiling(latest, quoteBlocks - 1);
    const quotes = [];
    for (const token of this.relay.terms().tokens) {
      if (measured.suspended.has(token)) continue;
      const payment: Payment = {
        token: measured.tokens.get(token) ?? null,
        payee: this.relay.payee,
        amount: MAX_AMOUNT,
      };
      const runner = inCallRunner(call, payment, schedule, gas, {
        delegated: new Set(authorities),
      });
      const plan = runnerPlan(
        this.relay.chain.chainId,
        runner,
        gas,
        schedule,
        authorities.length,
        empties,
        {},
      );
      this.relay.fits(plan);
      quotes.push({ token, amount: await this.relay.charge(measured, latest, token, plan, fees) });
    }
    return { validUntilBlock: latest.number + quoteBlocks, quotes };
  }

  /**
   * What `op` uses, simulated: refused, reading nothing, as userOperationLimits has it; an
   * authorization it brings taken as its account's delegation, its signature unchecked
   * (ERC-7769). One read: its simulation, and with a paymaster its simulation without it (its
   * paymaster's limits measured unless given); SimulationFailedError if one reverts.
   */
  private async used(op: UserOperation): Promise<GasUsed> {
    this.userOperationLimits(op);
    const limit = BigInt(this.relay.config.maxGas);
    const calls = estimating(op, limit);
    const answers = await this.relay.chain.batch(calls);
    if (answers.length !== calls.length) throw new ChainError("no answer");
    const results: Uint8Array[] = [];
    for (const answer of answers) {
      if ("error" in answer) {
        if (answer.error.code !== REVERTED) throw answer.error;
        const revert = typeof answer.error.data === "string" ? answer.error.data : "0x";
        throw new SimulationFailedError(bytes(revert as Hex));
      }
      results.push(bytes(hexOf(answer.result)));
    }
    return used(results, op, limit);
  }

  /** All the gas `estimate`'s limits give its op: refused if over the most gas a transaction may
   * use. */
  private within(estimate: GasEstimate): bigint {
    const needs = gasOf(estimate);
    const limit = BigInt(this.relay.config.maxGas);
    if (needs > limit) {
      throw new InvalidUserOperationError("gas", `the operation needs ${needs}, over ${limit}`);
    }
    return needs;
  }

  /** The gas `ofSize`'s handleOps call is planned with, as it's sent: a node's estimate of its
   * simulation, less its intrinsic gas; at least its limits (`needs`). */
  private async callGas(
    ofSize: UserOperation,
    needs: bigint,
    schedule: GasSchedule,
  ): Promise<number> {
    const [estimate] = await this.relay.chain.batch([gasEstimate(ofSize)]);
    if (estimate === undefined) throw new ChainError("no answer");
    if ("error" in estimate) {
      if (estimate.error.code !== REVERTED) throw estimate.error;
      const revert = typeof estimate.error.data === "string" ? estimate.error.data : "0x";
      throw new SimulationFailedError(bytes(revert as Hex));
    }
    const simulated = Number(quantity(resultOf(estimate))) -
      intrinsic(schedule, simulationCalldata(ofSize, null)).standard;
    return Math.max(Number(needs), simulated);
  }

  /**
   * The gas price an op paying by its fees is asked, both its fees (EntryPoint v0.7 takes a max
   * fee that's its priority fee too as its price, whatever the base fee): a gas as a quote
   * charges it, and our fee a gas, in wei, ETH's margin included. As bruno/bundler.py's
   * user_operation_gas_price.
   */
  async userOperationGasPrice(): Promise<bigint> {
    const measured = await this.relay.available();
    this.relay.supported(measured, NATIVE_ASSET);
    const latest = await this.relay.latest();
    const fees = feeCeiling(latest, this.relay.config.quoteBlocks - 1);
    const tip = this.relay.tip(latest);
    const ours = tip > this.relay.config.minFeePerGas ? tip : this.relay.config.minFeePerGas;
    return await this.relay.price(NATIVE_ASSET, fees.maxFeePerGas + ours);
  }
}

// how many of an op's handleOps' zero bytes a quote prices as other bytes: its payment's amount
// word, a placeholder's when quoted, whatever amount it's then given
const AMOUNT_BYTES = 32;

/** `op` with the limits `estimate` gives it, its paymaster's too. */
function sized(op: UserOperation, estimate: GasEstimate): UserOperation {
  return {
    ...op,
    verificationGasLimit: estimate.verificationGasLimit,
    callGasLimit: estimate.callGasLimit,
    paymaster: op.paymaster === null ? null : {
      ...op.paymaster,
      verificationGasLimit: estimate.paymasterVerificationGasLimit,
      postOpGasLimit: estimate.paymasterPostOpGasLimit,
    },
  };
}

/** `data` with its first AMOUNT_BYTES zero bytes made 0xff: priced as its transaction's calldata
 * is, by how many of its bytes are zero, whichever its payment's amount fills. */
function amountRoom(data: Uint8Array): Uint8Array {
  const room = data.slice();
  let left = AMOUNT_BYTES;
  for (let i = 0; i < room.length && left > 0; i++) {
    if (room[i] === 0) {
      room[i] = 0xff;
      left--;
    }
  }
  return room;
}
