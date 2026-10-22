/**
 * A chain through its nodes, over JSON-RPC: reads, fees, receipts and sending. Each call, or
 * batch of calls, goes to the first node that answers.
 */

import { type Address, bytes, checksum, type Hex, hex } from "./evm.ts";
import { type GasSchedule, scheduleOf } from "./forks.ts";
import { Fraction } from "./fraction.ts";
import type { Fees } from "./transactions.ts";

/** The nodes failed: unexpected, not a client's doing. */
export class ChainError extends Error {}

/** A node answered with an error object: a revert included. */
export class JsonRpcError extends Error {
  constructor(
    readonly method: string,
    readonly code: number,
    message: string,
    readonly data: unknown,
  ) {
    super(`${method}: ${code} ${message}`);
  }
}

export interface Receipt {
  readonly status: Hex;
  readonly blockNumber: number;
}

/** One JSON-RPC call. */
export interface Call {
  readonly method: string;
  readonly params: readonly unknown[];
}

/** One call's answer in a batch: its result, or the node's error answer to it. */
export type Answer = { readonly result: unknown } | { readonly error: JsonRpcError };

/** The latest block, the next one's base fee, and the median tip of each of the last
 * TIP_BLOCKS blocks, oldest first: what an alarm's work is priced and scheduled by, read once. */
export interface Latest {
  readonly number: number;
  readonly timestamp: number;
  readonly baseFeePerGas: bigint;
  readonly nextBaseFee: bigint;
  readonly tips: readonly bigint[];
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

/** An account's balance and nonce. */
export interface AccountState {
  readonly balance: bigint;
  readonly nonce: number;
}

/** How long a node is given to answer one request: past it, the next node is asked. */
export const RPC_TIMEOUT_MS = 10_000;
/** The most calls in one batch request: nodes cap a batch (geth at 1,000), and a larger batch
 * is sent as several. */
export const MAX_BATCH = 100;
/** The tip is read from the last TIP_BLOCKS blocks' median tips, and the last TIP_RECENT of
 * them: enough history to be steady, and recent enough to rise within a few blocks. */
export const TIP_BLOCKS = 20;
export const TIP_RECENT = 5;
/** EIP-1559: the base fee rises by at most an eighth a block. */
export const MAX_BASE_FEE_RISE = new Fraction(9n, 8n);

let ids = 0;

/** eth_estimateGas of a plain call to an account made for it: no code, no value, no data. Its
 * gas is the fork's base transaction cost, which tells the forks apart (scheduleOf). */
export function emptyCall(): Call {
  return {
    method: "eth_estimateGas",
    params: [{ to: hex(crypto.getRandomValues(new Uint8Array(20))) }],
  };
}

export class Chain {
  constructor(
    readonly chainId: number,
    readonly rpcUrls: readonly string[],
    /** The chain's block time: the next block is a slot after the latest. */
    readonly slotSeconds: number,
  ) {}

  /** `method` at the first node that answers; a node's error answer is raised as it is. */
  async rpc(method: string, params: readonly unknown[]): Promise<unknown> {
    const request = { jsonrpc: "2.0", id: ++ids, method, params };
    return resultOf(await this.post(method, request, (body) => answerTo(request, body)));
  }

  /**
   * Each of `calls`, as JSON-RPC batches of at most MAX_BATCH, each at the first node that
   * answers all of it: the answers in the calls' order, a node's error answer to one call its
   * own, not the batch's.
   */
  async batch(calls: readonly Call[]): Promise<Answer[]> {
    const answers: Answer[] = [];
    for (let start = 0; start < calls.length; start += MAX_BATCH) {
      answers.push(...(await this.batchOnce(calls.slice(start, start + MAX_BATCH))));
    }
    return answers;
  }

  private batchOnce(calls: readonly Call[]): Promise<Answer[]> {
    const requests = calls.map((c) => ({ jsonrpc: "2.0", id: ++ids, ...c }));
    const methods = [...new Set(calls.map((c) => c.method))].join(", ");
    return this.post(methods, requests, (body) => answersTo(requests, body));
  }

  /** The nodes in the order a request tries them: shuffled each time, so the load is spread
   * across them, and a node that refuses (a public one rate-limiting, say) is passed over for
   * the rest. */
  nodes(): string[] {
    const order = [...this.rpcUrls];
    for (let i = order.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [order[i], order[j]] = [order[j] as string, order[i] as string];
    }
    return order;
  }

  /** `payload` at the first node whose answer `read` takes. */
  private async post<T>(what: string, payload: unknown, read: (body: unknown) => T): Promise<T> {
    const failures: string[] = [];
    for (const url of this.nodes()) {
      try {
        const response = await fetch(url, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(payload),
          signal: AbortSignal.timeout(RPC_TIMEOUT_MS),
        });
        if (!response.ok) {
          failures.push(`${host(url)}: HTTP ${response.status}`);
          continue;
        }
        return read(await response.json());
      } catch (e) {
        // no answer in time, or none that parses or answers every call: the next node
        failures.push(`${host(url)}: ${e instanceof Error ? e.name : "failed"}`);
      }
    }
    throw new ChainError(`${what}: no node answered: ${failures.join("; ")}`);
  }

  async checkChain(): Promise<void> {
    const chainId = Number(quantity(await this.rpc("eth_chainId", [])));
    if (chainId !== this.chainId) {
      throw new ChainError(`the nodes are chain ${chainId}, not ${this.chainId}`);
    }
  }

  async nonce(account: Address): Promise<number> {
    return Number(quantity(await this.rpc("eth_getTransactionCount", [account, "latest"])));
  }

  /** eth_call of `data` to `to`, at the latest block. */
  async call(to: Address, data: Uint8Array): Promise<Uint8Array> {
    return bytes(hexOf(await this.rpc("eth_call", [{ to, data: hex(data) }, "latest"])));
  }

  /** The latest block, the next one's base fee (eth_feeHistory's last) and the last
   * TIP_BLOCKS blocks' median tips (its 50th percentile), in one request: as of the same block,
   * unless one arrives between the node's two answers. A block without a reward (none of its
   * transactions tipped) tips 0. */
  async latest(): Promise<Latest> {
    const [block, history] = (await this.batch([
      { method: "eth_getBlockByNumber", params: ["latest", false] },
      { method: "eth_feeHistory", params: [`0x${TIP_BLOCKS.toString(16)}`, "latest", [50]] },
    ])).map(resultOf);
    const { number, timestamp, baseFeePerGas } = block as Record<string, unknown>;
    const { baseFeePerGas: fees, reward } = history as {
      baseFeePerGas: unknown[];
      reward?: unknown[][] | null;
    };
    return {
      number: Number(quantity(number)),
      timestamp: Number(quantity(timestamp)),
      baseFeePerGas: quantity(baseFeePerGas),
      nextBaseFee: quantity(fees[fees.length - 1]),
      tips: (reward ?? []).map((r) => (r.length > 0 ? quantity(r[0]) : 0n)),
    };
  }

  /** The latest block and the finalized one, in one request. */
  async finality(): Promise<Finality> {
    const [latest, finalized] = (await this.batch([
      { method: "eth_getBlockByNumber", params: ["latest", false] },
      { method: "eth_getBlockByNumber", params: ["finalized", false] },
    ])).map(resultOf) as Record<string, unknown>[];
    return {
      head: Number(quantity(latest?.number)),
      timestamp: Number(quantity(latest?.timestamp)),
      finalized: Number(quantity(finalized?.number)),
    };
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

  /** Each of `accounts`' balance and nonce, in one batch. */
  async accounts(accounts: readonly Address[]): Promise<AccountState[]> {
    const answers = (await this.batch(accounts.flatMap((a) => [
      { method: "eth_getBalance", params: [a, "latest"] },
      { method: "eth_getTransactionCount", params: [a, "latest"] },
    ]))).map(resultOf);
    return accounts.map((_, i) => ({
      balance: quantity(answers[2 * i]),
      nonce: Number(quantity(answers[2 * i + 1])),
    }));
  }

  async sendRaw(raw: Uint8Array): Promise<void> {
    await this.rpc("eth_sendRawTransaction", [hex(raw)]);
  }
}

/** Fees valid for `blocks` blocks however the base fee rises: the next base fee risen for them,
 * and `priority`. A transaction pays only the base fee of its block, and the tip. */
export function lastingFees(latest: Latest, priority: bigint, blocks: number): Fees {
  return {
    maxFeePerGas: risen(latest.nextBaseFee, blocks) + priority,
    maxPriorityFeePerGas: priority,
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
export function risen(baseFee: bigint, blocks: number): bigint {
  let rise = new Fraction(1n);
  for (let i = 0; i < blocks; i++) rise = rise.mul(MAX_BASE_FEE_RISE);
  return rise.mul(baseFee).ceil();
}

/** A transaction's receipt, as eth_getTransactionReceipt answers it: null if there's none. */
export function receiptOf(receipt: unknown): Receipt | null {
  if (receipt === null) {
    return null;
  }
  const { status, blockNumber } = receipt as Record<string, unknown>;
  return { status: hexOf(status), blockNumber: Number(quantity(blockNumber)) };
}

/** An answer's result: its error answer raised. */
export function resultOf(answer: Answer): unknown {
  if ("error" in answer) throw answer.error;
  return answer.result;
}

/** A batch's answers, matched to its requests by id: an answer missing is the node's failure. */
function answersTo(
  requests: readonly { id: number; method: string }[],
  body: unknown,
): Answer[] {
  if (!Array.isArray(body)) {
    throw new ChainError("a batch answered with no list");
  }
  const byId = new Map<unknown, Record<string, unknown>>();
  for (const item of body) {
    if (typeof item === "object" && item !== null) {
      const answer = item as Record<string, unknown>;
      byId.set(answer.id, answer);
    }
  }
  return requests.map((request) => answerTo(request, byId.get(request.id)));
}

/** One request's answer: one that isn't an answer is the node's failure. */
function answerTo(request: { id: number; method: string }, body: unknown): Answer {
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    throw new ChainError(`${request.method}: no answer`);
  }
  const answer = body as Record<string, unknown>;
  const error = answer.error as { code: number; message: string; data?: unknown } | undefined;
  if (error !== undefined && error !== null) {
    return { error: new JsonRpcError(request.method, error.code, error.message, error.data) };
  }
  return { result: answer.result };
}

export function quantity(value: unknown): bigint {
  return BigInt(hexOf(value));
}

export function hexOf(value: unknown): Hex {
  if (typeof value !== "string" || !value.startsWith("0x")) {
    throw new ChainError(`not hex: ${JSON.stringify(value)}`);
  }
  return value as Hex;
}

export function addressOf(word: Uint8Array): Address {
  return checksum(hex(word.slice(12, 32)));
}
