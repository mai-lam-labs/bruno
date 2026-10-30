/**
 * The relay as an ERC-4337 bundler (ERC-7769): a UserOp estimated, priced, sent as a submission
 * whose call is the EntryPoint's handleOps of it, and its receipt read. A Bundler holds its relay
 * and keeps nothing of its own. A port of bruno/bundler.py.
 */

import { ChainError, fallen, hexOf, quantity, resultOf, REVERTED } from "./chain.ts";
import { type Address, bytes, type EncodedCall, type Hex, hex, NATIVE_ASSET } from "./evm.ts";
import { intrinsic } from "./forks.ts";
import { runnerPlan, SimulationFailedError } from "./plan.ts";
import { feeCeiling, MAX_AMOUNT, type Relay, scheduleOf, UnsupportedError } from "./relay.ts";
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
   * `op`'s gas limits, as ERC-7769's eth_estimateUserOperationGas answers them. Refused, reading
   * nothing, as userOperationLimits has it; an authorization it brings is taken as its account's
   * delegation, its signature unchecked (ERC-7769). One read: its simulation, and with a
   * paymaster its simulation without it (its paymaster's limits measured unless given);
   * SimulationFailedError if one reverts. Refused if
   * its limits are over the most gas a transaction may use. An op paying by its fees is charged,
   * at the price its fees give at the least the base fee can fall to by the quote's last block,
   * as much gas as a quote of its transaction comes to: the rest, beyond what it uses, is its
   * preVerificationGas, whatever its fees (gas it's charged, not gas its transaction uses). Fees
   * paying nothing a gas then are invalid params.
   */
  async estimateUserOperation(op: UserOperation): Promise<GasEstimate> {
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
    const gasUsed = used(results, op, limit);
    const estimate = estimated(gasUsed, op);
    const needs = gasOf(estimate);
    if (needs > limit) {
      throw new InvalidUserOperationError("gas", `the operation needs ${needs}, over ${limit}`);
    }
    if (!paysByFees(op)) return estimate;
    const measured = await this.relay.available();
    this.relay.supported(measured, NATIVE_ASSET);
    const sized: UserOperation = {
      ...op,
      verificationGasLimit: estimate.verificationGasLimit,
      callGasLimit: estimate.callGasLimit,
      paymaster: op.paymaster === null ? null : {
        ...op.paymaster,
        verificationGasLimit: estimate.paymasterVerificationGasLimit,
        postOpGasLimit: estimate.paymasterPostOpGasLimit,
      },
    };
    const call: EncodedCall = {
      to: op.entryPoint.address,
      data: handleOps(sized, this.relay.payee),
      value: 0n,
    };
    const schedule = scheduleOf(measured);
    const payment: Payment = { token: null, payee: this.relay.payee, amount: MAX_AMOUNT };
    // the call's gas as it's sent: a node's estimate of the op's simulation, less its intrinsic
    // gas; at least its limits (bruno/bundler.py)
    const [sizedGas] = await this.relay.chain.batch([gasEstimate(sized)]);
    if (sizedGas === undefined) throw new ChainError("no answer");
    if ("error" in sizedGas) {
      if (sizedGas.error.code !== REVERTED) throw sizedGas.error;
      const revert = typeof sizedGas.error.data === "string" ? sizedGas.error.data : "0x";
      throw new SimulationFailedError(bytes(revert as Hex));
    }
    const simulated = Number(quantity(resultOf(sizedGas))) -
      intrinsic(schedule, simulationCalldata(sized, null)).standard;
    const gas = Math.max(Number(needs), simulated);
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
