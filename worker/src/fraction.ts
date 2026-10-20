/**
 * Exact rationals over bigint: prices, margins and profits, as the Python reference computes
 * them with fractions.Fraction. Never a float.
 */

export class Fraction {
  readonly num: bigint;
  readonly den: bigint;

  constructor(num: bigint, den = 1n) {
    if (den === 0n) {
      throw new RangeError("a fraction's denominator isn't 0");
    }
    this.num = den < 0n ? -num : num;
    this.den = den < 0n ? -den : den;
  }

  /** A decimal string, exactly: "1.1" is 11/10. */
  static decimal(text: string): Fraction {
    const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(text.trim());
    if (match === null) {
      throw new RangeError(`not a decimal: ${text}`);
    }
    const [, sign, whole, fraction = ""] = match;
    const num = BigInt(`${whole}${fraction}`) * (sign === "-" ? -1n : 1n);
    return new Fraction(num, 10n ** BigInt(fraction.length));
  }

  add(other: Fraction | bigint): Fraction {
    const o = of(other);
    return new Fraction(this.num * o.den + o.num * this.den, this.den * o.den);
  }

  sub(other: Fraction | bigint): Fraction {
    const o = of(other);
    return new Fraction(this.num * o.den - o.num * this.den, this.den * o.den);
  }

  mul(other: Fraction | bigint): Fraction {
    const o = of(other);
    return new Fraction(this.num * o.num, this.den * o.den);
  }

  div(other: Fraction | bigint): Fraction {
    const o = of(other);
    return new Fraction(this.num * o.den, this.den * o.num);
  }

  compare(other: Fraction | bigint): number {
    const o = of(other);
    const d = this.num * o.den - o.num * this.den;
    return d < 0n ? -1 : d > 0n ? 1 : 0;
  }

  /** The greatest integer at most this, as Python's // and math.floor. */
  floor(): bigint {
    const q = this.num / this.den;
    return this.num < 0n && q * this.den !== this.num ? q - 1n : q;
  }

  /** The least integer at least this, as Python's math.ceil. */
  ceil(): bigint {
    return -new Fraction(-this.num, this.den).floor();
  }
}

function of(value: Fraction | bigint): Fraction {
  return typeof value === "bigint" ? new Fraction(value) : value;
}
