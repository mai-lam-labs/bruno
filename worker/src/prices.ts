/**
 * What a token is worth in wei, read from pools on the chain: each token's path of pools to
 * WETH, multiplied out, each pool's spot price; kept for the configured time. ETH is 1 wei a
 * wei. The same reads as the Python reference (bruno/prices.py).
 */

import { addressOf, type Chain } from "./chain.ts";
import { type Address, keccak, NATIVE_ASSET, utf8 } from "./evm.ts";
import { Fraction } from "./fraction.ts";
import { bigIntOf } from "./transactions.ts";

export type PoolKind = "uniswap-v2" | "uniswap-v3";

export interface Hop {
  readonly kind: PoolKind;
  readonly pool: Address;
}

export class PriceError extends Error {}

const TOKEN0 = keccak(utf8("token0()")).slice(0, 4);
const TOKEN1 = keccak(utf8("token1()")).slice(0, 4);
const GET_RESERVES = keccak(utf8("getReserves()")).slice(0, 4);
const SLOT0 = keccak(utf8("slot0()")).slice(0, 4);

/** Each token's price, kept `ttlSeconds`; one read at a time for a token, which concurrent
 * requests for that price wait for. */
export class Prices {
  private readonly read = new Map<Address, { wei: Fraction; at: number }>();
  private readonly reading = new Map<Address, Promise<Fraction>>();

  constructor(
    private readonly chain: Chain,
    private readonly weth: Address,
    private readonly paths: ReadonlyMap<Address, readonly Hop[]>,
    private readonly ttlSeconds: number,
  ) {}

  /** What one base unit of `token` is worth in wei: ETH's is 1. */
  async wei(token: Address): Promise<Fraction> {
    if (token === NATIVE_ASSET) {
      return new Fraction(1n);
    }
    const kept = this.read.get(token);
    if (kept !== undefined && Date.now() - kept.at < this.ttlSeconds * 1000) {
      return kept.wei;
    }
    let reading = this.reading.get(token);
    if (reading === undefined) {
      reading = this.along(token)
        .then((wei) => {
          this.read.set(token, { wei, at: Date.now() });
          return wei;
        })
        .finally(() => this.reading.delete(token));
      this.reading.set(token, reading);
    }
    return await reading;
  }

  private async along(token: Address): Promise<Fraction> {
    const path = this.paths.get(token);
    if (path === undefined) {
      throw new PriceError(`no price path for ${token}`);
    }
    let price = new Fraction(1n);
    let at = token;
    for (const hop of path) {
      const token0 = addressOf(await this.chain.call(hop.pool, TOKEN0));
      const token1 = addressOf(await this.chain.call(hop.pool, TOKEN1));
      const ofToken0 = await this.spot(hop);
      if (at === token0) {
        price = price.mul(ofToken0);
        at = token1;
      } else if (at === token1) {
        price = price.div(ofToken0);
        at = token0;
      } else {
        throw new PriceError(`${token}'s path: pool ${hop.pool} doesn't trade ${at}`);
      }
    }
    if (at !== this.weth) {
      throw new PriceError(`${token}'s path ends at ${at}, not WETH (${this.weth})`);
    }
    return price;
  }

  /** The pool's price of token0 in token1, base unit for base unit. */
  private async spot(hop: Hop): Promise<Fraction> {
    if (hop.kind === "uniswap-v2") {
      const answer = await this.chain.call(hop.pool, GET_RESERVES);
      const reserve0 = bigIntOf(answer.slice(0, 32));
      const reserve1 = bigIntOf(answer.slice(32, 64));
      if (reserve0 === 0n || reserve1 === 0n) {
        throw new PriceError(`pool ${hop.pool} is empty`);
      }
      return new Fraction(reserve1, reserve0);
    }
    const sqrtPriceX96 = bigIntOf((await this.chain.call(hop.pool, SLOT0)).slice(0, 32));
    if (sqrtPriceX96 === 0n) {
      throw new PriceError(`pool ${hop.pool} isn't initialised`);
    }
    return new Fraction(sqrtPriceX96 ** 2n, 1n << 192n);
  }
}
