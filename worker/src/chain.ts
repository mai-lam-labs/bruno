/**
 * A chain through its nodes: reads, fees and receipts. Each call, or batch of calls,
 * goes to the first node that answers.
 */

import { type DataFee, read, reading, risen } from "./datafee.ts";
import { type Address, bytes, checksum, type Hex, hex, hexQuantity } from "./evm.ts";
import { type GasSchedule, scheduleOf } from "./forks.ts";
import { Fraction } from "./fraction.ts";
import {
  type Answer,
  AnswerError,
  batch,
  type Call,
  call,
  JsonRpcError,
  MAX_BATCH,
  type Node,
  resultOf,
  StatusError,
} from "./jsonrpc.ts";
import type { Fees } from "./transactions.ts";
import { Millis, millisOf, Seconds } from "./units.ts";

/** The nodes failed: unexpected, not a client's doing. */
export class ChainError extends Error {}

/** A transaction's receipt, as much of it as the relay reads: its status (1 a success) and its
 * block (bruno/chain.py's Receipt). */
export interface Receipt {
  readonly status: number;
  readonly blockNumber: number;
}

/** A block, as much of it as the relay reads: its number and its timestamp (bruno/chain.py's
 * Block). */
export interface Block {
  readonly number: number;
  readonly timestamp: number;
}

/** eth_feeHistory's answer, as much of it as the relay reads: the base fee of the block after
 * its last, and each of its blocks' tips at the percentile asked for, oldest first (0 for a
 * block without one: none of its transactions tipped). */
export interface FeeHistory {
  readonly nextBaseFee: bigint;
  readonly tips: readonly bigint[];
}

/** The latest block, the next one's base fee, and the median tip of each of the last
 * TIP_BLOCKS blocks, oldest first; on an OP-stack chain, the inputs of what a transaction pays
 * beyond gas: what an alarm's work is priced and scheduled by, read once. */
export interface Latest {
  readonly number: number;
  readonly timestamp: number;
  readonly nextBaseFee: bigint;
  readonly tips: readonly bigint[];
  readonly dataFee?: DataFee;
}

/** The latest block's number and timestamp, and the finalized block's number. */
export interface Finality {
  readonly head: number;
  readonly timestamp: number;
  readonly finalized: number;
}

/** A node named without what its URL may carry (an API key in its path or query). */
export function host(url: string): string {
  return new URL(url).host;
}

/** Why a node didn't answer, naming it by its host: an HTTP status, what isn't an answer, or
 * the kind of failure. */
export function failure(url: string, e: unknown): string {
  if (e instanceof StatusError || e instanceof AnswerError) return `${host(url)}: ${e.message}`;
  return `${host(url)}: ${e instanceof Error ? e.name : "failed"}`;
}

/** The tip is read from the last TIP_BLOCKS blocks' median tips, and the last TIP_RECENT of
 * them: enough history to be steady, and recent enough to rise within a few blocks. */
export const TIP_BLOCKS = 20;
export const TIP_RECENT = 5;
/** EIP-1559: the base fee rises by at most an eighth a block. */
export const MAX_BASE_FEE_RISE = new Fraction(9n, 8n);
/** And the most it falls in one: by an eighth (EIP-1559). */
export const MAX_BASE_FEE_FALL = new Fraction(7n, 8n);

/** eth_estimateGas of a plain call to an account made for it: no code, no value, no data. Its
 * gas is the fork's base transaction cost, which tells the forks apart (scheduleOf). */
export function emptyCall(): Call {
  return {
    method: "eth_estimateGas",
    params: [{ to: hex(crypto.getRandomValues(new Uint8Array(20))) }],
  };
}

/** What of a slot passes, by default, before its block can be read at a chain's nodes: a second
 * on Ethereum (bruno/chain.py's SLOTS_A_BLOCK_DELAY). */
export const SLOTS_A_BLOCK_DELAY = 12;

/** How long after a slot starts its block can be read, by default, on a chain of `slot`. */
export function blockDelayOf(slot: Seconds): Millis {
  return Millis(Math.floor(millisOf(slot) / SLOTS_A_BLOCK_DELAY));
}

export class Chain {
  constructor(
    readonly chainId: number,
    /** Its nodes: each a URL, or a node with its batch cap. */
    nodes: readonly (string | Node)[],
    /** The chain's block time: the next block is a slot after the latest. */
    readonly slot: Seconds,
    /** How long after a slot starts its block can be read at these nodes. */
    readonly blockDelay: Millis = blockDelayOf(slot),
  ) {
    this.configured = nodes.map((n) => typeof n === "string" ? { url: n, maxBatch: MAX_BATCH } : n);
  }

  private readonly configured: readonly Node[];

  /** When the block after the one at `timestamp` can be read (by the wall clock, as block
   * timestamps are): a slot on, and the chain's block delay. */
  nextBlockAt(timestamp: number): Millis {
    return Millis(millisOf(Seconds(timestamp + this.slot)) + this.blockDelay);
  }

  /** The highest block `latest` has read: a node behind the others (a load balancer's backend,
   * say) answering an older one doesn't lower it. */
  seen = 0;

  /** The highest block seen, as a block tag: "latest" before any is. */
  get seenTag(): string {
    return this.seen > 0 ? hexQuantity(this.seen) : "latest";
  }

  /** `method` at the first node that answers; a node's error answer is raised as it is. */
  async rpc(method: string, params: readonly unknown[]): Promise<unknown> {
    const failures: string[] = [];
    for (const node of this.nodes()) {
      try {
        return await call(node.url, method, params);
      } catch (e) {
        if (e instanceof JsonRpcError) throw e;
        failures.push(failure(node.url, e));
      }
    }
    throw new ChainError(`${method}: no node answered: ${failures.join("; ")}`);
  }

  /**
   * Each of `calls`, at the first node that answers: as JSON-RPC batches of at most its
   * `maxBatch`, a node that fails leaving the rest to the next. The answers in the calls' order,
   * a node's error answer to one call its own, not the batch's.
   */
  async batch(calls: readonly Call[]): Promise<Answer[]> {
    const answers: Answer[] = [];
    const failures: string[] = [];
    for (const node of this.nodes()) {
      try {
        while (answers.length < calls.length) {
          const chunk = calls.slice(answers.length, answers.length + node.maxBatch);
          answers.push(...(await batch(node.url, chunk)));
        }
        return answers;
      } catch (e) {
        failures.push(failure(node.url, e));
      }
    }
    const methods = [...new Set(calls.slice(answers.length).map((c) => c.method))].join(", ");
    throw new ChainError(`${methods}: no node answered: ${failures.join("; ")}`);
  }

  /** The nodes in the order a request tries them: shuffled each time, so the load is spread
   * across them, and a node that refuses (a public one rate-limiting, say) is passed over for
   * the rest. */
  nodes(): Node[] {
    const order = [...this.configured];
    for (let i = order.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [order[i], order[j]] = [order[j] as Node, order[i] as Node];
    }
    return order;
  }

  /** eth_call of `data` to `to`, at the latest block. */
  async call(to: Address, data: Uint8Array): Promise<Uint8Array> {
    return bytes(hexOf(await this.rpc("eth_call", [{ to, data: hex(data) }, "latest"])));
  }

  /** The latest block, the next one's base fee (eth_feeHistory's last) and the last
   * TIP_BLOCKS blocks' median tips (its 50th percentile), in one request; on an OP-stack chain
   * (`opStack`, as measured) its data fee's inputs with them. A block without a reward (none of
   * its transactions tipped) tips 0. */
  async latest(opStack = false): Promise<Latest> {
    const answers = await this.batch([
      { method: "eth_getBlockByNumber", params: ["latest", false] },
      { method: "eth_feeHistory", params: [hexQuantity(TIP_BLOCKS), "latest", [50]] },
      ...(opStack ? reading() : []),
    ]);
    const [head, fees] = answers.slice(0, 2).map(resultOf);
    const block = blockOf(head, "the latest block");
    const history = feeHistoryOf(fees);
    this.seen = Math.max(this.seen, block.number);
    return {
      number: block.number,
      timestamp: block.timestamp,
      nextBaseFee: history.nextBaseFee,
      tips: history.tips,
      ...(opStack ? { dataFee: read(answers.slice(2).map((a) => wordOf(resultOf(a)))) } : {}),
    };
  }

  /** The latest block and the finalized one, in one request. */
  async finality(): Promise<Finality> {
    const [latest, finalized] = (await this.batch([
      { method: "eth_getBlockByNumber", params: ["latest", false] },
      { method: "eth_getBlockByNumber", params: ["finalized", false] },
    ])).map(resultOf);
    const head = blockOf(latest);
    return { head: head.number, timestamp: head.timestamp, finalized: blockOf(finalized).number };
  }

  /** The gas schedule in force, measured: a plain call to an account made for it. */
  async schedule(): Promise<GasSchedule> {
    const call = emptyCall();
    const gas = Number(quantity(await this.rpc(call.method, call.params)));
    const schedule = scheduleOf(gas);
    if (schedule === null) {
      throw new ChainError(`a fork no schedule knows: a plain call costs ${gas}`);
    }
    return schedule;
  }
}

/** Fees valid for `blocks` blocks however the base fee rises: the next base fee risen for them,
 * and `priority`; what's paid beyond gas, its L1 fees risen for them too. A transaction pays
 * only the base fee of its block, and the tip. */
export function lastingFees(latest: Latest, priority: bigint, blocks: number): Fees {
  return {
    maxFeePerGas: risenBaseFee(latest.nextBaseFee, blocks) + priority,
    maxPriorityFeePerGas: priority,
    ...(latest.dataFee === undefined ? {} : { dataFee: risen(latest.dataFee, blocks) }),
  };
}

/** The tip the market pays: the larger of the median of `tips` (each a block's median tip,
 * oldest first) and the median of the last TIP_RECENT. The upper median of an even count; 0 for
 * none. */
export function marketTip(tips: readonly bigint[]): bigint {
  const all = median(tips);
  const recent = median(tips.slice(-TIP_RECENT));
  return all > recent ? all : recent;
}

function median(values: readonly bigint[]): bigint {
  const sorted = [...values].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
  return sorted[Math.floor(sorted.length / 2)] ?? 0n;
}

/** The most `baseFee` can be `blocks` blocks later: risen by an eighth each block, rounded up. */
export function risenBaseFee(baseFee: bigint, blocks: number): bigint {
  let rise = new Fraction(1n);
  for (let i = 0; i < blocks; i++) rise = rise.mul(MAX_BASE_FEE_RISE);
  return rise.mul(baseFee).ceil();
}

/** The least `baseFee` can be `blocks` blocks later: fallen by an eighth each block, rounded
 * down (bruno/chain.py's fallen). */
export function fallenBaseFee(baseFee: bigint, blocks: number): bigint {
  let fall = new Fraction(1n);
  for (let i = 0; i < blocks; i++) fall = fall.mul(MAX_BASE_FEE_FALL);
  return fall.mul(baseFee).floor();
}

/** A block eth_getBlockByNumber answered, read: ChainError if it isn't one. */
export function blockOf(value: unknown, what = "a block"): Block {
  const { number, timestamp } = objectOf(value, what);
  return { number: Number(quantity(number)), timestamp: Number(quantity(timestamp)) };
}

/** A fee history eth_feeHistory answered, read: ChainError if it isn't one. */
export function feeHistoryOf(value: unknown): FeeHistory {
  const { baseFeePerGas, reward } = objectOf(value, "eth_feeHistory");
  const fees = listOf(baseFeePerGas, "eth_feeHistory's baseFeePerGas");
  if (fees.length === 0) throw new ChainError("eth_feeHistory: no base fee");
  return {
    nextBaseFee: quantity(fees[fees.length - 1]),
    tips: (reward === undefined || reward === null ? [] : listOf(reward, "reward")).map(tip),
  };
}

/** A transaction's receipt, as eth_getTransactionReceipt answers it: null if there's none,
 * ChainError if it isn't one. */
export function receiptOf(receipt: unknown): Receipt | null {
  if (receipt === null) return null;
  if (typeof receipt !== "object" || Array.isArray(receipt)) {
    throw new ChainError(`not a receipt: ${JSON.stringify(receipt)}`);
  }
  const { status, blockNumber } = receipt as Record<string, unknown>;
  return { status: Number(quantity(status)), blockNumber: Number(quantity(blockNumber)) };
}

/** What a node's quantity is: hex digits after 0x (leading zeros let through, as nodes differ);
 * and a 32-byte word, as an eth_call returning one value answers. */
const QUANTITY = /^0x[0-9a-fA-F]+$/;
const WORD = /^0x[0-9a-fA-F]{64}$/;
/** Data: whole bytes after 0x. */
const DATA = /^0x(?:[0-9a-fA-F]{2})*$/;

/** A node's quantity: ChainError, the node's failure, if it isn't one. */
export function quantity(value: unknown): bigint {
  if (typeof value !== "string" || !QUANTITY.test(value)) {
    throw new ChainError(`not a quantity: ${JSON.stringify(value)}`);
  }
  return BigInt(value);
}

/** A node's 32-byte word, as an eth_call returning one value answers: ChainError if it isn't
 * one. */
function word(value: unknown): Hex {
  if (typeof value !== "string" || !WORD.test(value)) {
    throw new ChainError(`not a 32-byte word: ${JSON.stringify(value)}`);
  }
  return value as Hex;
}

export function wordBytes(value: unknown): Uint8Array {
  return bytes(word(value));
}

/** A node's 32-byte word, as a uint256. */
export function wordOf(value: unknown): bigint {
  return BigInt(word(value));
}

/** A node's object (a block, a fee history): ChainError if it isn't one. */
export function objectOf(value: unknown, what: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new ChainError(`${what}: not an object: ${JSON.stringify(value)}`);
  }
  return value as Record<string, unknown>;
}

/** A node's list: ChainError if it isn't one. */
export function listOf(value: unknown, what: string): unknown[] {
  if (!Array.isArray(value)) throw new ChainError(`${what}: not a list: ${JSON.stringify(value)}`);
  return value;
}

/** A block's median tip, eth_feeHistory's reward for it: 0 if it has none (none of its
 * transactions tipped). */
function tip(reward: unknown): bigint {
  const rewards = listOf(reward, "reward");
  return rewards.length > 0 ? quantity(rewards[0]) : 0n;
}

/** A revert's data, as a node's error answer carries it (EIP-1474's execution error): none if
 * it gives none; ChainError if what it gives isn't data. */
export function revertData(error: JsonRpcError): Uint8Array {
  return typeof error.data === "string" ? dataOf(error.data) : new Uint8Array();
}

/** A node's data, as hex: ChainError if it isn't whole bytes after 0x. */
export function hexOf(value: unknown): Hex {
  if (typeof value !== "string" || !DATA.test(value)) {
    throw new ChainError(`not hex: ${JSON.stringify(value)}`);
  }
  return value as Hex;
}

/** A node's data. */
export function dataOf(value: unknown): Uint8Array {
  return bytes(hexOf(value));
}

/** An account's code, as eth_getCode answers it: nodes answer one without code "0x", some
 * "0x0". */
export function codeOf(value: unknown): Uint8Array {
  return value === "0x0" ? new Uint8Array() : dataOf(value);
}

/** Whether eth_getCode's answer is any code. */
export function hasCode(value: unknown): boolean {
  return codeOf(value).length > 0;
}

/** The address a 32-byte ABI word holds: ChainError if it isn't 32 bytes. */
export function addressOf(word: Uint8Array): Address {
  if (word.length !== 32) throw new ChainError(`not a 32-byte word: ${word.length} bytes`);
  return checksum(hex(word.slice(12, 32)));
}
