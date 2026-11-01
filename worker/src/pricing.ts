/**
 * What a call costs on one chain, in each token it's paid in, and whether it fits: the fees a
 * transaction can pay by a quote's last block, our fee, and the margins; the limits on a call
 * and its transaction; and the tokens taken. As bruno/pricing.py's.
 */

import {
  type Chain,
  ChainError,
  hasCode,
  type Latest,
  marketTip,
  quantity,
  risenBaseFee,
} from "./chain.ts";
import type { ChainConfig } from "./config.ts";
import { risen } from "./datafee.ts";
import { quotedCalldata, transferGas } from "./eip3009.ts";
import type { Deployment } from "./erc6492.ts";
import { type Address, type EncodedCall, hex } from "./evm.ts";
import { intrinsic } from "./forks.ts";
import { Fraction } from "./fraction.ts";
import { type Answer, type Call, resultOf } from "./jsonrpc.ts";
import {
  type Category,
  lookup,
  type Measurements,
  scheduleOf,
  UnavailableError,
} from "./measurements.ts";
import { empty, type Plan, runnerPlan } from "./plan.ts";
import type { Prices } from "./prices.ts";
import { inCallRunner, type Payment } from "./runner.ts";
import { InvalidSubmissionError } from "./submission.ts";
import { cost as txCost, type Fees } from "./transactions.ts";
import { verification } from "./wallets.ts";

/** How far the tip may rise over a quote's blocks, as a multiple of today's: a quote allows for
 * it, and an account is funded for it. */
const TIP_ALLOWANCE = 2n;
/** The largest amount: a quote's runner pushes it, so its initcode is never longer. */
export const MAX_AMOUNT = (1n << 256n) - 1n;

/** Why a token or payee isn't taken (Unsupported): its category the reason's
 * (bruno/pricing.py's Unsupported). */
export type Unsupported =
  | "not-accepted"
  | "no-eip3009"
  | "wrong-payee"
  | "eth-not-taken"
  | "suspended";

/** A suspended token is measured again, and may be taken then: retried. The rest are asked for
 * otherwise. */
export function unsupportedCategory(reason: Unsupported): Category {
  return reason === "suspended" ? "retry" : "request";
}

/** A token or payee the relay doesn't take: why (`reason`), and its details for people. */
export class UnsupportedError extends Error {
  constructor(readonly reason: Unsupported, message: string) {
    super(message);
  }
}

/** A batch's answer at `index`: ChainError if it's missing. */
function answerAt(answers: readonly Answer[], index: number): Answer {
  const answer = answers[index];
  if (answer === undefined) throw new ChainError("no answer");
  return answer;
}

/**
 * What a quote is told of its payment's signer, each if known: its `address` (the payment then a
 * transfer, EIP-3009), an account to deploy first, and the gas its signature check takes.
 */
export interface Payer {
  readonly address?: Address;
  readonly deployment?: Deployment;
  readonly verifyGas?: number;
}

/**
 * How a quote prices its payment's transfer: whether there is one, and if its signer is a
 * contract, its check's gas (`verifyGas`) and its signature's length (`signatureBytes`); and
 * the gas its deployment's factory call takes, if one is made.
 */
interface Signing {
  readonly transferring: boolean;
  readonly verifyGas: number;
  readonly signatureBytes: number | null;
  readonly deployment: Deployment | null;
  readonly deployGas: number;
}

export interface Quotes {
  readonly validUntilBlock: number;
  readonly quotes: readonly { token: Address; amount: bigint }[];
}

/** The fees a transaction can pay by a quote's last block, as of `latest`: the next base fee
 * risen for `quoteBlocks` blocks, and the tip doubled; what's paid beyond gas, risen for them
 * too. Accounts are funded to it. */
export function feeCeiling(latest: Latest, quoteBlocks: number): Fees {
  const tip = marketTip(latest.tips);
  return {
    maxFeePerGas: risenBaseFee(latest.nextBaseFee, quoteBlocks) + TIP_ALLOWANCE * tip,
    maxPriorityFeePerGas: tip,
    ...(latest.dataFee === undefined ? {} : { dataFee: risen(latest.dataFee, quoteBlocks) }),
  };
}

export class Pricing {
  private readonly margins: Map<Address, Fraction>;

  constructor(
    private readonly chain: Chain,
    private readonly config: ChainConfig,
    private readonly prices: Prices,
    private readonly payee: Address,
  ) {
    this.margins = new Map(
      config.tokens.map((t) => [t.address, config.marginMultiplier.mul(t.marginMultiplier)]),
    );
  }

  /**
   * How `payer`'s transfer is priced, if `transferring`. Its signer is `payer.address`, else the
   * account its deployment deploys. A contract (one with `verifyGas` given, one deployed, or one
   * with code) has its check priced at `verifyGas`, else at the wallet gas schedule's figure for
   * its deployment's factory, else at the schedule's largest. A deployment is priced as the
   * runner checks it, and its factory call as estimated: for an account with no code, made
   * (InvalidSubmissionError if it reverts); for one with code, made only to prepare it
   * (ERC-6492), which a transfer's wrapped signature may ask, so priced if it can be.
   * InvalidSubmissionError too for `verifyGas` with no transfer to check, or over the most gas a
   * call may have. One read batch, if any.
   */
  async signing(
    measured: Measurements,
    payer: Payer,
    transferring: boolean,
  ): Promise<Signing> {
    if (payer.verifyGas !== undefined && !transferring) {
      throw new InvalidSubmissionError("verifyGas", "only with a payer");
    }
    if (payer.verifyGas !== undefined && payer.verifyGas > this.config.maxGas) {
      const most = this.config.maxGas;
      throw new InvalidSubmissionError("verifyGas", `over ${most}`, { most: BigInt(most) });
    }
    const deployment = payer.deployment ?? null;
    let signer = payer.address ?? null;
    if (signer === null && transferring && deployment !== null) signer = deployment.account;
    const calls: Call[] = [];
    if (deployment !== null) {
      calls.push(
        { method: "eth_getCode", params: [deployment.account, "latest"] },
        {
          method: "eth_estimateGas",
          params: [{ to: deployment.factory, data: hex(deployment.factoryData) }],
        },
      );
    }
    // the signer's own code, unless it's the account deployed
    const readSigner = signer !== null && (deployment === null || deployment.account !== signer);
    if (readSigner) calls.push({ method: "eth_getCode", params: [signer, "latest"] });
    const answers = calls.length > 0 ? await this.chain.batch(calls) : [];
    const schedule = scheduleOf(measured);
    let deployGas = 0;
    if (deployment !== null) {
      const deploys = !hasCode(resultOf(answerAt(answers, 0)));
      const estimate = answerAt(answers, 1);
      if ("error" in estimate) {
        if (deploys) {
          throw new InvalidSubmissionError(
            "deployment",
            `its factory call fails: ${estimate.error.nodeMessage}`,
          );
        }
      } else if (deploys || transferring) {
        deployGas = Number(quantity(estimate.result)) -
          intrinsic(schedule, deployment.factoryData).standard;
      }
    }
    const signerHasCode = readSigner &&
      hasCode(resultOf(answerAt(answers, answers.length - 1)));
    const contract = transferring && (
      payer.verifyGas !== undefined ||
      (deployment !== null && deployment.account === signer) ||
      signerHasCode
    );
    if (!contract) {
      return { transferring, verifyGas: 0, signatureBytes: null, deployment, deployGas };
    }
    const known = verification(deployment, schedule);
    return {
      transferring,
      verifyGas: payer.verifyGas ?? known.gas,
      signatureBytes: known.signatureBytes,
      deployment,
      deployGas,
    };
  }

  async quoteAt(
    measured: Measurements,
    latest: Latest,
    gas: number,
    callBytes: number,
    authorityAddress: Address | null,
    tokens: readonly Address[],
    signing: Signing,
    existing: boolean,
  ): Promise<Quotes> {
    const schedule = scheduleOf(measured);
    const target = authorityAddress ?? this.payee;
    const call: EncodedCall = {
      to: target,
      data: new Uint8Array(callBytes).fill(0xff),
      value: 0n,
    };
    const authorities = authorityAddress === null ? [] : [authorityAddress];
    const empties = existing ? 0 : await empty(this.chain, authorities);
    // the base fee risen and the tip doubled, as they can be by the quote's last block
    const fees = feeCeiling(latest, this.config.quoteBlocks - 1);
    const quotes = [];
    for (const token of tokens) {
      const erc20 = lookup(measured.tokens, token, "a token's measurement");
      let payment: Payment = { token: erc20, payee: this.payee, amount: MAX_AMOUNT };
      if (signing.transferring && erc20?.transferGas !== undefined) {
        const bound = transferGas(erc20.transferGas, schedule, measured.payeeHolds.has(token));
        payment = {
          ...payment,
          transfer: {
            calldata: quotedCalldata(signing.signatureBytes),
            gas: bound + signing.verifyGas,
          },
        };
      }
      const runner = inCallRunner(call, payment, schedule, gas, {
        delegated: new Set(authorities),
        deployment: signing.deployment,
      });
      const plan = runnerPlan(
        this.chain.chainId,
        runner,
        gas,
        schedule,
        authorities.length,
        empties,
        {
          extraGas: signing.deployGas,
        },
      );
      this.fits(plan);
      quotes.push({ token, amount: await this.charge(measured, latest, token, plan, fees) });
    }
    return { validUntilBlock: latest.number + this.config.quoteBlocks, quotes };
  }

  /**
   * The plan's transaction within the most gas a transaction may use, everything in it: the
   * runner, a deployment, a transfer and its check, and the call.
   */
  fits(plan: Plan): void {
    const most = this.config.maxGas;
    if (plan.gasLimit > most) {
      throw new InvalidSubmissionError(
        "gas",
        `the transaction needs ${plan.gasLimit}, over ${most}`,
        {
          most: BigInt(most),
          needs: BigInt(plan.gasLimit),
        },
      );
    }
  }

  /**
   * The call's own gas, and its length, within the limits: a call given more than a whole
   * transaction may use can never fit in one.
   */
  withinLimits(gas: number, callBytes: number): void {
    const { maxGas, maxCallBytes } = this.config;
    if (gas > maxGas) {
      throw new InvalidSubmissionError("gas", `over ${maxGas}`, { most: BigInt(maxGas) });
    }
    if (callBytes > maxCallBytes) {
      throw new InvalidSubmissionError("call", `over ${maxCallBytes} bytes`, {
        most: BigInt(maxCallBytes),
      });
    }
  }

  supported(measured: Measurements, token: Address): void {
    if (!this.margins.has(token)) {
      throw new UnsupportedError("not-accepted", `${token} isn't accepted`);
    }
    const why = measured.suspended.get(token);
    if (why !== undefined) throw new UnsupportedError("suspended", `${token} is suspended: ${why}`);
  }

  /** The tip each transaction offers: the market's (marketTip). */
  tip(latest: Latest): bigint {
    return marketTip(latest.tips);
  }

  /** The fees of the block after `latest`: exactly its base fee, and the tip; what's paid
   * beyond gas risen for the one block (its L1 inputs may be a new L1 block's). */
  fees(latest: Latest): Fees {
    const tip = this.tip(latest);
    return {
      maxFeePerGas: latest.nextBaseFee + tip,
      maxPriorityFeePerGas: tip,
      ...(latest.dataFee === undefined ? {} : { dataFee: risen(latest.dataFee, 1) }),
    };
  }

  /** Our fee on a transaction of `gasLimit`, in wei: the tip a gas, at least `minFeePerGas`;
   * and at least `minFee`. */
  private async ourFee(measured: Measurements, latest: Latest, gasLimit: number): Promise<bigint> {
    const tip = this.tip(latest);
    const perGas = tip > this.config.minFeePerGas ? tip : this.config.minFeePerGas;
    const fee = perGas * BigInt(gasLimit);
    const minFee = this.config.minFee;
    if (minFee === null) return fee;
    const decimals = measured.decimals.get(minFee.token);
    if (decimals === undefined) {
      const why = `min_fee's token ${minFee.token} is suspended`;
      throw new UnavailableError("min-fee-suspended", why);
    }
    const units = minFee.amount.mul(new Fraction(10n ** BigInt(decimals))).ceil();
    const least = (await this.prices.wei(minFee.token)).mul(units).ceil();
    return fee > least ? fee : least;
  }

  /** What `plan`'s transaction is charged in `token`: its cost at `fees` (the fees of the block
   * after `latest`, if not given), and our fee, margins included. */
  async charge(
    measured: Measurements,
    latest: Latest,
    token: Address,
    plan: Plan,
    fees: Fees = this.fees(latest),
  ): Promise<bigint> {
    const cost = txCost(fees, plan.gasLimit, plan.size);
    return await this.price(token, cost + await this.ourFee(measured, latest, plan.gasLimit));
  }

  /** `wei` in `token`'s base units, margins included, rounded up. */
  async price(token: Address, wei: bigint): Promise<bigint> {
    const margin = lookup(this.margins, token, "a token's margin");
    return new Fraction(wei).div(await this.prices.wei(token)).mul(margin).ceil();
  }
}
