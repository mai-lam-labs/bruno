/**
 * What an OP-stack transaction pays beyond its gas: the L1 data fee and the operator fee,
 * taken from its sender's balance on top of gas x fee. Ethereum has neither: no `DataFee` at
 * all. GasPriceOracle's formulas (Fjord's getL1FeeUpperBound, Jovian's getOperatorFee),
 * computed from the inputs read with each block and checked against the oracle's own answers
 * when the chain is measured. A port of the Python reference (bruno/datafee.py).
 */

import { type Answer, type Call, quantity, resultOf } from "./chain.ts";
import { hex, keccak, utf8 } from "./evm.ts";

/** The OP stack's predeploys: the fee oracle, and the L1 attributes it reads. */
export const GAS_PRICE_ORACLE = "0x420000000000000000000000000000000000000F";
export const L1_BLOCK = "0x4200000000000000000000000000000000000015";

/** Fjord's regression from a FastLZ size to an estimated Brotli size, scaled by 1e6, and the
 * smallest size it's taken as. */
const COST_INTERCEPT = -42_585_600n;
const COST_FASTLZ_COEF = 836_500n;
const MIN_TRANSACTION_SIZE = 100n;
const SCALE = 10n ** 6n;
/** getL1FeeUpperBound: the signature's bytes, and FastLZ's practical bound on a size. */
const SIGNATURE_BYTES = 68n;
const FASTLZ_SLACK_DIVISOR = 255n;
const FASTLZ_SLACK_BYTES = 16n;
/** Jovian's operator fee: gas x scalar x 100, and the constant. */
const OPERATOR_FEE_MULTIPLIER = 100n;
/** What the L1 fees can rise to a block: an eighth (a blob base fee by less). */
const MAX_L1_FEE_RISE_NUMERATOR = 9n;
const MAX_L1_FEE_RISE_DENOMINATOR = 8n;
/** The sizes and gas the oracle is asked for, to check the formulas against. */
export const CHECKED_SIZES = [0, 40_000] as const;
export const CHECKED_GAS = [0, 1_000_000] as const;

/** An OP-stack chain's fee inputs as of a block. */
export interface DataFee {
  readonly l1BaseFee: bigint;
  readonly blobBaseFee: bigint;
  readonly baseFeeScalar: bigint;
  readonly blobBaseFeeScalar: bigint;
  readonly operatorFeeScalar: bigint;
  readonly operatorFeeConstant: bigint;
}

function selector(signature: string): string {
  return hex(keccak(utf8(signature)).slice(0, 4));
}

function word(n: number): string {
  return BigInt(n).toString(16).padStart(64, "0");
}

function call(to: string, data: string, block: string): Call {
  return { method: "eth_call", params: [{ to, data }, block] };
}

/** Each input, from the predeploy that answers it, in DataFee's order. */
const INPUTS: readonly (readonly [string, string])[] = [
  [GAS_PRICE_ORACLE, "l1BaseFee()"],
  [GAS_PRICE_ORACLE, "blobBaseFee()"],
  [GAS_PRICE_ORACLE, "baseFeeScalar()"],
  [GAS_PRICE_ORACLE, "blobBaseFeeScalar()"],
  [L1_BLOCK, "operatorFeeScalar()"],
  [L1_BLOCK, "operatorFeeConstant()"],
];

/** getL1FeeUpperBound(unsignedSize): the most an unsigned transaction pays for its data. */
export function l1Fee(fee: DataFee, unsignedSize: number): bigint {
  const size = BigInt(unsignedSize) + SIGNATURE_BYTES;
  const fastlz = size + size / FASTLZ_SLACK_DIVISOR + FASTLZ_SLACK_BYTES;
  const regressed = COST_INTERCEPT + COST_FASTLZ_COEF * fastlz;
  const floor = MIN_TRANSACTION_SIZE * SCALE;
  const estimated = regressed > floor ? regressed : floor;
  const scaled = fee.baseFeeScalar * 16n * fee.l1BaseFee + fee.blobBaseFeeScalar * fee.blobBaseFee;
  return (estimated * scaled) / (SCALE * SCALE);
}

/** getOperatorFee(gas): what `gas` used pays the operator. */
export function operatorFee(fee: DataFee, gas: number): bigint {
  return BigInt(gas) * fee.operatorFeeScalar * OPERATOR_FEE_MULTIPLIER + fee.operatorFeeConstant;
}

/** The most a transaction of `unsignedSize` bytes using up to `gas` pays beyond gas. */
export function beyondGas(fee: DataFee, unsignedSize: number, gas: number): bigint {
  return l1Fee(fee, unsignedSize) + operatorFee(fee, gas);
}

/** These inputs, the L1 fees risen as far as they can in `blocks` blocks. */
export function risenDataFee(fee: DataFee, blocks: number): DataFee {
  const num = MAX_L1_FEE_RISE_NUMERATOR ** BigInt(blocks);
  const den = MAX_L1_FEE_RISE_DENOMINATOR ** BigInt(blocks);
  const up = (v: bigint) => (v * num + den - 1n) / den;
  return { ...fee, l1BaseFee: up(fee.l1BaseFee), blobBaseFee: up(fee.blobBaseFee) };
}

/** The inputs' reads at `block`, in DataFee's order. */
export function reading(block = "latest"): Call[] {
  return INPUTS.map(([to, signature]) => call(to, selector(signature), block));
}

/** The inputs from their reads' answers (reading's order). */
export function read(answers: readonly Answer[]): DataFee {
  const [l1, blob, scalar, blobScalar, opScalar, opConstant] = answers.map((a) =>
    quantity(resultOf(a))
  );
  return {
    l1BaseFee: l1!,
    blobBaseFee: blob!,
    baseFeeScalar: scalar!,
    blobBaseFeeScalar: blobScalar!,
    operatorFeeScalar: opScalar!,
    operatorFeeConstant: opConstant!,
  };
}

/** The oracle's own answers at `block` to check the formulas against: getL1FeeUpperBound at
 * each of CHECKED_SIZES, then getOperatorFee at each of CHECKED_GAS. */
export function checking(block: string): Call[] {
  const upper = selector("getL1FeeUpperBound(uint256)");
  const operator = selector("getOperatorFee(uint256)");
  return [
    ...CHECKED_SIZES.map((n) => call(GAS_PRICE_ORACLE, upper + word(n), block)),
    ...CHECKED_GAS.map((g) => call(GAS_PRICE_ORACLE, operator + word(g), block)),
  ];
}

/** Whether the oracle's answers (checking's order) are what the formulas give from `fee`. */
export function agrees(fee: DataFee, answers: readonly Answer[]): boolean {
  const expected = [
    ...CHECKED_SIZES.map((n) => l1Fee(fee, n)),
    ...CHECKED_GAS.map((g) => operatorFee(fee, g)),
  ];
  return answers.length === expected.length &&
    answers.every((a, i) => quantity(resultOf(a)) === expected[i]);
}
