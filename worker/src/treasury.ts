/**
 * The treasury on one chain: the payee's revenue converted to ETH for the float, so the relay
 * keeps paying its own gas (the Python reference's bruno/treasury.py; DESIGN.md, Treasury).
 *
 * Each run reads the float, its concurrency accounts and the payee in one batch, and measures
 * what the float and accounts spent since the last run. Their target is `runwayDays` of that
 * spend, at least enough to fund every account once for the largest transaction at the fee
 * ceiling. What they lack comes from the payee: its ETH above its reserve, then its WETH
 * unwrapped and its other tokens swapped, in the configuration's order, each paying the payee,
 * which forwards the lot to the float. The run's transactions go to every builder as one bundle,
 * all or none, for each of the next BUNDLE_BLOCKS blocks; the next run reads the forward's
 * receipt. Its snapshot is kept in the chain's object's storage.
 *
 * Every transaction's gas is measured (eth_estimateGas), with headroom: forks price gas
 * differently. A swap is measured once the router may spend its token, so each token is approved
 * ahead of its first swap, in a run of its own; the next run follows soon after.
 */

import { derive } from "./accounts.ts";
import { sendBundle } from "./builders.ts";
import { atomicParams } from "./bundle.ts";
import { addressOf, type Call, ChainError, lastingFees, marketTip, resultOf } from "./chain.ts";
import type { ChainConfig, TreasuryConfig } from "./config.ts";
import { type Address, bytes, type Hex, hex, keccak, NATIVE_ASSET, utf8 } from "./evm.ts";
import { Fraction } from "./fraction.ts";
import { feeCeiling, type Relay, type Store } from "./relay.ts";
import { approveCalldata, type Step, swapCalldata, withdrawCalldata } from "./router.ts";
import { transfer } from "./forks.ts";
import { decimals } from "./tokens.ts";
import { sign, type SignedTransaction, type Signer } from "./transactions.ts";

/** A run's bundle is offered for each of the next this many blocks. */
export const BUNDLE_BLOCKS = 5;
const SECONDS_A_DAY = 86_400n;
/** A transaction's gas limit over its measured gas: the state may move before it lands. */
const GAS_HEADROOM = new Fraction(5n, 4n);
/** How many times a conversion is sized, its gas measured again each time it's more. */
const SIZINGS = 3;
const UNLIMITED = (1n << 256n) - 1n;
/** An allowance the router spends from without approving again. */
const APPROVED = 1n << 128n;
const BALANCE_OF = selector("balanceOf(address)");
const ALLOWANCE = selector("allowance(address,address)");
const TOKEN0 = selector("token0()");
const TOKEN1 = selector("token1()");
const FEE = selector("fee()");

export class TreasuryError extends Error {}

/** What an account holds of a token, and the token's decimals: ETH's as ERC-7528's address. */
export interface Holding {
  readonly token: Address;
  readonly amount: bigint;
  readonly decimals: number;
}

/** The relay's money on a chain as a run found it, and what the run did. */
export interface Snapshot {
  readonly at: number;
  readonly block: number;
  readonly floatBalance: bigint;
  readonly accounts: readonly bigint[];
  readonly payee: readonly Holding[];
  readonly spendADay: bigint | null;
  readonly target: bigint;
  readonly reserve: bigint;
  readonly covers: bigint;
  readonly outcome: string;
  readonly forwarded: bigint;
  readonly forwardTx: Hex | null;
  /** Whether the run approved tokens: the next follows soon, to swap them. */
  readonly approving: boolean;
}

/** A transaction's call from the payee, and its gas. */
interface PayeeCall {
  readonly to: Address;
  readonly data: Uint8Array;
}

/** One token converted to ETH at the payee: its transaction's call and gas, and the least ETH
 * it pays the payee. */
interface Conversion extends PayeeCall {
  readonly token: Address;
  readonly amount: bigint;
  readonly gas: bigint;
  readonly minOut: bigint;
}

export class Treasury {
  private readonly routes = new Map<Address, readonly Step[]>();
  private readonly decimals = new Map<Address, number>([[NATIVE_ASSET, 18]]);

  /** The treasury of `relay`'s chain, `payee` its payee's key, its snapshot in `store`. */
  constructor(
    private readonly relay: Relay,
    private readonly payee: Signer,
    private readonly settings: TreasuryConfig,
    private readonly store: Store,
  ) {
    if (payee.address !== relay.payee) {
      throw new TreasuryError(`the payee key is ${payee.address}'s, not ${relay.payee}'s`);
    }
    if (payee.address === relay.float.address) {
      throw new TreasuryError("the payee is the float: their nonces would collide");
    }
  }

  /** The last run's snapshot: null before the first. */
  get snapshot(): Snapshot | null {
    return storedSnapshot(this.store);
  }

  private get config(): ChainConfig {
    return this.relay.config;
  }

  /** One run: read, measure, convert and forward what's needed, sent as a bundle. Its
   * snapshot, kept. */
  async run(): Promise<Snapshot> {
    const chain = this.relay.chain;
    const latest = await chain.latest();
    const fees = lastingFees(latest, marketTip(latest.tips));
    const maxFee = fees.maxFeePerGas;
    const feeNow = latest.nextBaseFee + fees.maxPriorityFeePerGas;
    const floatAddress = this.relay.float.address;
    const accounts = Array.from(
      { length: this.config.accounts },
      (_, i) => derive(this.relay.float, i).address,
    );
    const tokens = this.config.tokens.map((t) => t.address).filter((t) => t !== NATIVE_ASSET);
    const payee = this.payee.address;
    const router = this.settings.router;
    const last = this.snapshot;
    const calls: Call[] = [floatAddress, ...accounts, payee].map((a) => ({
      method: "eth_getBalance",
      params: [a, "latest"],
    }));
    calls.push({ method: "eth_getTransactionCount", params: [payee, "latest"] });
    calls.push({ method: "eth_getTransactionCount", params: [floatAddress, "latest"] });
    for (const token of tokens) {
      calls.push(ethCall(token, concat(BALANCE_OF, word(payee))));
      calls.push(ethCall(token, concat(ALLOWANCE, word(payee), word(router))));
    }
    if (last !== null && last.forwardTx !== null) {
      calls.push({ method: "eth_getTransactionReceipt", params: [last.forwardTx] });
    }
    const answers = (await chain.batch(calls)).map(resultOf);
    const floatBalance = BigInt(answers[0] as string);
    const accountBalances = accounts.map((_, i) => BigInt(answers[1 + i] as string));
    const payeeEth = BigInt(answers[1 + accounts.length] as string);
    let nonce = Number(BigInt(answers[2 + accounts.length] as string));
    // a float with no balance and no nonce isn't in the state: the forward creates it
    const floatExists = floatBalance > 0n || BigInt(answers[3 + accounts.length] as string) > 0n;
    const transferGas = BigInt(transfer(chain.scheduleAfter(latest), !floatExists));
    const at = 4 + accounts.length;
    const balances = new Map(tokens.map((t, i) => [t, BigInt(answers[at + 2 * i] as string)]));
    const allowances = new Map(
      tokens.map((t, i) => [t, BigInt(answers[at + 2 * i + 1] as string)]),
    );
    const held = floatBalance + accountBalances.reduce((a, b) => a + b, 0n);
    const now = Math.floor(Date.now() / 1000);

    // measured: what the float and accounts spent since the last run, a day
    let spendADay: bigint | null = null;
    if (last !== null && now > last.at) {
      const receipt = last.forwardTx !== null ? answers[answers.length - 1] : null;
      const landed = receipt !== null && receipt !== undefined &&
          (receipt as { status: string }).status === "0x1"
        ? last.forwarded
        : 0n;
      const spent = max(0n, heldOf(last) + landed - held);
      spendADay = (spent * SECONDS_A_DAY) / BigInt(now - last.at);
    }
    const transaction = BigInt(this.config.maxGas) * feeCeiling(latest, this.config.quoteBlocks);
    const minimum = this.config.fundingMultiplier.mul(BigInt(this.config.accounts)).ceil() *
      transaction;
    const target = max(
      minimum,
      spendADay === null ? 0n : this.settings.runwayDays.mul(spendADay).ceil(),
    );
    const need = max(0n, target - held);
    // running dry: the float and accounts can't cover a transaction, or won't last until the
    // next run
    const emergency = held < transaction || (spendADay !== null &&
      held * SECONDS_A_DAY < spendADay * BigInt(this.settings.intervalSeconds));
    const weth = this.config.weth;
    // each token the router may not spend yet is approved, once, ahead of its swaps
    const unapproved = tokens.filter((t) => t !== weth && (allowances.get(t) ?? 0n) < APPROVED);
    const approvals: (PayeeCall & { token: Address })[] = unapproved.map((t) => ({
      token: t,
      to: t,
      data: approveCalldata(router, UNLIMITED),
    }));
    // each conversion's gas, measured on all the payee holds of its token
    const convertible = tokens.filter((t) =>
      (balances.get(t) ?? 0n) > 0n && !unapproved.includes(t)
    );
    const toMeasure: PayeeCall[] = [];
    for (const t of convertible) toMeasure.push(await this.call(t, balances.get(t) ?? 0n, 0n));
    const measured = await this.measure([...toMeasure, ...approvals]);
    const gasOf = new Map(convertible.map((t, i) => [t, measured[i] ?? null]));
    const approvalGas = measured.slice(convertible.length);
    const notes = [...gasOf].filter(([, g]) => g === null).map(([t]) =>
      `${t}: no conversion would succeed`
    );
    const measuredGas = [...gasOf.values()].reduce((a: bigint, g) => a + (g ?? 0n), 0n);
    const reserve = this.config.fundingMultiplier.mul((transferGas + measuredGas) * maxFee)
      .ceil();

    const approved: (PayeeCall & { token: Address; gas: bigint })[] = [];
    const conversions: Conversion[] = [];
    // the payee's ETH as its transactions go, each at its max cost, the conversions paying it
    // their least out
    let working = payeeEth;
    for (const [i, approval] of approvals.entries()) {
      const gas = approvalGas[i] ?? null;
      if (gas === null || working < gas * maxFee) {
        notes.push(`${approval.token}: can't approve the router`);
        continue;
      }
      approved.push({ ...approval, gas });
      working -= gas * maxFee;
    }
    // ETH the payee must gain for the float's need, its own reserve and the forward's gas, each
    // transaction at its max fee
    let shortfall = need + reserve + transferGas * maxFee - working;
    const costShare = this.settings.maxCostMultiplier.sub(1n);
    // a run that approves does only that: what its tokens' swaps cost is measured next run
    for (const token of approved.length > 0 ? [] : convertible) {
      const tokenGas = gasOf.get(token) ?? null;
      if (shortfall <= 0n) break;
      if (tokenGas === null) continue;
      const conversion = await this.conversion(
        token,
        balances.get(token) ?? 0n,
        tokenGas,
        shortfall,
        maxFee,
        emergency ? null : feeNow,
      );
      if (conversion === null) continue;
      const gas = conversion.gas;
      const cost = new Fraction(gas * feeNow + await this.cut(conversion));
      const value = (await this.relay.prices.wei(token)).mul(conversion.amount);
      if (!emergency && cost.compare(value.mul(costShare)) > 0) {
        notes.push(`${token}: converting ${conversion.amount} costs too much`);
        continue;
      }
      if (working < gas * maxFee) {
        notes.push("the payee holds too little ETH for a conversion's gas");
        break;
      }
      working += conversion.minOut - gas * maxFee;
      shortfall -= conversion.minOut - gas * maxFee;
      conversions.push(conversion);
    }

    let forward = approved.length > 0 ? 0n : min(need, working - transferGas * maxFee - reserve);
    if (forward > 0n && !emergency) {
      const cost = new Fraction(transferGas * feeNow);
      if (conversions.length === 0 && cost.compare(costShare.mul(forward)) > 0) {
        notes.push(`forwarding ${forward} wei costs too much`);
        forward = 0n;
      }
    }
    forward = max(forward, 0n);

    const txs: SignedTransaction[] = [];
    for (const { to, data, gas } of [...approved, ...conversions]) {
      txs.push(sign(this.payee, chain.chainId, nonce, fees, Number(gas), to, data));
      nonce += 1;
    }
    let forwardTx: SignedTransaction | null = null;
    if (forward > 0n) {
      forwardTx = sign(
        this.payee,
        chain.chainId,
        nonce,
        fees,
        Number(transferGas),
        floatAddress,
        new Uint8Array(),
        forward,
      );
      txs.push(forwardTx);
    }
    let outcome: string;
    if (txs.length > 0) {
      await this.send(latest.number + 1, txs);
      const done = [
        ...approved.map((a) => `approving ${a.token}`),
        ...conversions.map((c) => `converting ${c.amount} of ${c.token}`),
      ];
      if (forward > 0n) done.push(`forwarding ${forward} wei to the float`);
      outcome = `sent: ${done.join("; ")}`;
    } else if (need === 0n && payeeEth >= reserve) {
      outcome = "nothing to do";
    } else {
      outcome = "nothing sent";
    }
    if (notes.length > 0) outcome += ` (${notes.join("; ")})`;
    const payeeHoldings: Holding[] = [{ token: NATIVE_ASSET, amount: payeeEth, decimals: 18 }];
    for (const token of tokens) {
      if (!this.decimals.has(token)) this.decimals.set(token, await decimals(chain, token));
      payeeHoldings.push({
        token,
        amount: balances.get(token) ?? 0n,
        decimals: this.decimals.get(token) as number,
      });
    }
    const perTransaction = BigInt(this.config.maxGas) * feeNow;
    const snapshot: Snapshot = {
      at: now,
      block: latest.number,
      floatBalance,
      accounts: accountBalances,
      payee: payeeHoldings,
      spendADay,
      target,
      reserve,
      covers: perTransaction > 0n ? held / perTransaction : 0n,
      outcome,
      forwarded: forward,
      forwardTx: forwardTx === null ? null : hex(forwardTx.hash),
      approving: approved.length > 0,
    };
    this.store.put("treasury", snapshotToJson(snapshot));
    console.log(`chain ${chain.chainId}: treasury: ${outcome}`);
    return snapshot;
  }

  /** The payee's call converting `amount` of `token` to ETH, at least `minOut` wei: WETH
   * withdrawn, any other token swapped through the router. */
  private async call(token: Address, amount: bigint, minOut: bigint): Promise<PayeeCall> {
    if (token === this.config.weth) return { to: token, data: withdrawCalldata(amount) };
    const steps = await this.route(token);
    return {
      to: this.settings.router,
      data: swapCalldata(steps, amount, minOut, this.payee.address),
    };
  }

  /** Each of `calls`' gas from the payee, measured in one batch, with headroom: null for one
   * that would fail. */
  private async measure(calls: readonly PayeeCall[]): Promise<(bigint | null)[]> {
    const answers = await this.relay.chain.batch(calls.map(({ to, data }) => ({
      method: "eth_estimateGas",
      params: [{ from: this.payee.address, to, data: hex(data) }, "latest"],
    })));
    return answers.map((a) =>
      "error" in a ? null : GAS_HEADROOM.mul(BigInt(a.result as string)).ceil()
    );
  }

  /** What converts enough of `token` (holding `balance`, its conversion `gas`) to cover
   * `shortfall` and its own gas at `fee`; all of it, if it falls short. Given `feeNow`, at least
   * enough that the gas at it is within the cost allowed: a conversion worth making is made in
   * one. Its gas measured again as it will be sent, and the amount sized again if that's more;
   * null if nothing would come of it, or it would fail. */
  private async conversion(
    token: Address,
    balance: bigint,
    gas: bigint,
    shortfall: bigint,
    fee: bigint,
    feeNow: bigint | null,
  ): Promise<Conversion | null> {
    const one = new Fraction(1n);
    const weth = token === this.config.weth;
    const wei = weth ? one : await this.relay.prices.wei(token);
    const margin = weth ? one : this.margin(token);
    const slippage = weth ? one : this.settings.slippageMultiplier;
    for (let i = 0; i < SIZINGS; i++) {
      const wanted = margin.mul(slippage).mul(shortfall + gas * fee).div(wei).ceil();
      const amount = min(balance, max(wanted, this.economic(gas, feeNow, margin, wei)));
      const minOut = wei.mul(amount).div(margin).div(slippage).floor();
      if (minOut <= gas * fee) return null;
      const call = await this.call(token, amount, minOut);
      const [sentGas] = await this.measure([call]);
      if (sentGas === null || sentGas === undefined) return null;
      if (sentGas <= gas) return { token, amount, ...call, gas: sentGas, minOut };
      gas = sentGas;
    }
    return null;
  }

  /** The least amount of a token (`wei` a unit, `margin` its pools' cut) whose conversion's
   * `gas` at `feeNow` is within the cost allowed; 0 without `feeNow`, or if none is. */
  private economic(gas: bigint, feeNow: bigint | null, margin: Fraction, wei: Fraction): bigint {
    const allowed = this.settings.maxCostMultiplier.sub(margin);
    if (feeNow === null || allowed.compare(0n) <= 0) return 0n;
    return new Fraction(gas * feeNow).div(allowed).div(wei).ceil();
  }

  /** What the pools take of `conversion`, in wei: its amount's worth × (the token's margin −
   * 1), the margin being what converting it costs. */
  private async cut(conversion: Conversion): Promise<bigint> {
    const wei = await this.relay.prices.wei(conversion.token);
    return wei.mul(conversion.amount).mul(this.margin(conversion.token).sub(1n)).ceil();
  }

  private margin(token: Address): Fraction {
    return this.config.tokens.find((t) => t.address === token)?.marginMultiplier ??
      new Fraction(1n);
  }

  /** `token`'s path to WETH as swaps: each pool's tokens and fee tier, read once. */
  private async route(token: Address): Promise<readonly Step[]> {
    const known = this.routes.get(token);
    if (known !== undefined) return known;
    const hops = this.config.tokens.find((t) => t.address === token)?.price ?? [];
    const calls: Call[] = [];
    for (const hop of hops) {
      calls.push(ethCall(hop.pool, TOKEN0), ethCall(hop.pool, TOKEN1));
      if (hop.kind === "uniswap-v3") calls.push(ethCall(hop.pool, FEE));
    }
    const answers = (await this.relay.chain.batch(calls)).map(resultOf);
    let i = 0;
    const steps: Step[] = [];
    let at = token;
    for (const hop of hops) {
      const token0 = addressOf(bytes(answers[i++] as Hex));
      const token1 = addressOf(bytes(answers[i++] as Hex));
      const fee = hop.kind === "uniswap-v3" ? Number(BigInt(answers[i++] as string)) : 0;
      if (at !== token0 && at !== token1) {
        throw new TreasuryError(`${token}'s path: pool ${hop.pool} doesn't trade ${at}`);
      }
      const out = at === token0 ? token1 : token0;
      steps.push({ kind: hop.kind, tokenIn: at, tokenOut: out, fee });
      at = out;
    }
    if (at !== this.config.weth) {
      throw new TreasuryError(`${token}'s path ends at ${at}, not WETH`);
    }
    this.routes.set(token, steps);
    return steps;
  }

  /** Every builder the bundle of `txs`, for each of BUNDLE_BLOCKS blocks from `block`. */
  private async send(block: number, txs: readonly SignedTransaction[]): Promise<void> {
    const sends = this.relay.builders.flatMap((builder) =>
      Array.from({ length: BUNDLE_BLOCKS }, (_, i) => ({
        builder,
        params: atomicParams(block + i, txs),
      }))
    );
    const results = await Promise.allSettled(
      sends.map(({ builder, params }) => sendBundle(builder, params)),
    );
    results.forEach((result, i) => {
      if (result.status === "rejected") {
        console.warn(`treasury: ${sends[i]?.builder.url} refused a bundle:`, result.reason);
      }
    });
    if (results.every((r) => r.status === "rejected")) {
      throw new ChainError("every builder refused the treasury's bundle");
    }
  }
}

/** The last run's snapshot in `store`: null before the first. */
export function storedSnapshot(store: Store): Snapshot | null {
  const stored = store.get("treasury");
  return stored === undefined ? null : snapshotFromJson(stored);
}

/** What the float and its accounts hold. */
export function heldOf(snapshot: Snapshot): bigint {
  return snapshot.floatBalance + snapshot.accounts.reduce((a, b) => a + b, 0n);
}

function ethCall(to: Address, data: Uint8Array): Call {
  return { method: "eth_call", params: [{ to, data: hex(data) }, "latest"] };
}

function selector(signature: string): Uint8Array {
  return keccak(utf8(signature)).slice(0, 4);
}

function word(address: Address): Uint8Array {
  return concat(new Uint8Array(12), bytes(address));
}

function concat(...parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of parts) {
    out.set(p, at);
    at += p.length;
  }
  return out;
}

function max(a: bigint, b: bigint): bigint {
  return a > b ? a : b;
}

function min(a: bigint, b: bigint): bigint {
  return a < b ? a : b;
}

function snapshotToJson(s: Snapshot): string {
  return JSON.stringify(s, (_, v) => (typeof v === "bigint" ? v.toString() : v));
}

function snapshotFromJson(json: string): Snapshot {
  const s = JSON.parse(json);
  return {
    ...s,
    floatBalance: BigInt(s.floatBalance),
    accounts: s.accounts.map(BigInt),
    payee: s.payee.map((h: { token: Address; amount: string; decimals: number }) => ({
      ...h,
      amount: BigInt(h.amount),
    })),
    spendADay: s.spendADay === null ? null : BigInt(s.spendADay),
    target: BigInt(s.target),
    reserve: BigInt(s.reserve),
    covers: BigInt(s.covers),
    forwarded: BigInt(s.forwarded),
  };
}
