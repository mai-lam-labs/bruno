/**
 * A chain through its nodes, over JSON-RPC: reads, fees, receipts and sending. Each call, or
 * batch of calls, goes to the first node that answers.
 */

import { type DataFee, GAS_PRICE_ORACLE, read, reading, risenDataFee } from "./datafee.ts";
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
 * TIP_BLOCKS blocks, oldest first; on an OP-stack chain, the inputs of what a transaction pays
 * beyond gas: what an alarm's work is priced and scheduled by, read once. */
export interface Latest {
  readonly number: number;
  readonly timestamp: number;
  readonly baseFeePerGas: bigint;
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

/** An account's balance and nonce. */
export interface AccountState {
  readonly balance: bigint;
  readonly nonce: number;
}

/** How long a node is given to answer one request: past it, the next node is asked. */
export const RPC_TIMEOUT_MS = 10_000;
/** The most calls a batch request carries unless a node caps it lower (Node.maxBatch): nodes cap
 * a batch (geth at 1,000), and a larger batch is sent as several. */
export const MAX_BATCH = 100;

/** A node: its URL, and the most calls it takes in one batch request (a public node may cap it
 * far lower: Unichain's at 10). */
export interface Node {
  readonly url: string;
  readonly maxBatch: number;
}

/** A node that didn't answer: why, naming it by its host. */
class NodeFailure extends Error {}

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
    /** Its nodes: each a URL, or a node with its batch cap. */
    nodes: readonly (string | Node)[],
    /** The chain's block time: the next block is a slot after the latest. */
    readonly slotSeconds: number,
  ) {
    this.configured = nodes.map((n) => typeof n === "string" ? { url: n, maxBatch: MAX_BATCH } : n);
  }

  private readonly configured: readonly Node[];

  /** The highest block `latest` has read: a node behind the others (a load balancer's backend,
   * say) answering an older one doesn't lower it. */
  seen = 0;

  /** `method` at the first node that answers; a node's error answer is raised as it is. */
  async rpc(method: string, params: readonly unknown[]): Promise<unknown> {
    const request = { jsonrpc: "2.0", id: ++ids, method, params };
    const failures: string[] = [];
    for (const node of this.nodes()) {
      try {
        return resultOf(await this.post(node, request, (body) => answerTo(request, body)));
      } catch (e) {
        if (!(e instanceof NodeFailure)) throw e;
        failures.push(e.message);
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
          const requests = chunk.map((c) => ({ jsonrpc: "2.0", id: ++ids, ...c }));
          answers.push(...(await this.post(node, requests, (body) => answersTo(requests, body))));
        }
        return answers;
      } catch (e) {
        if (!(e instanceof NodeFailure)) throw e;
        failures.push(e.message);
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

  /** `payload` at `node`, its answer as `read` takes it; NodeFailure if it doesn't answer. */
  private async post<T>(node: Node, payload: unknown, read: (body: unknown) => T): Promise<T> {
    try {
      const response = await fetch(node.url, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(RPC_TIMEOUT_MS),
      });
      if (!response.ok) throw new NodeFailure(`${host(node.url)}: HTTP ${response.status}`);
      return read(await response.json());
    } catch (e) {
      if (e instanceof NodeFailure) throw e;
      // no answer in time, or none that parses or answers every call: the next node
      throw new NodeFailure(`${host(node.url)}: ${e instanceof Error ? e.name : "failed"}`);
    }
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
   * TIP_BLOCKS blocks' median tips (its 50th percentile), in one request; on an OP-stack chain
   * (`opStack`, as measured) its data fee's inputs with them. A block without a reward (none of
   * its transactions tipped) tips 0. */
  async latest(opStack = false): Promise<Latest> {
    const answers = await this.batch([
      { method: "eth_getBlockByNumber", params: ["latest", false] },
      { method: "eth_feeHistory", params: [`0x${TIP_BLOCKS.toString(16)}`, "latest", [50]] },
      ...(opStack ? reading() : []),
    ]);
    const [block, history] = answers.slice(0, 2).map(resultOf);
    const { number, timestamp, baseFeePerGas } = block as Record<string, unknown>;
    const { baseFeePerGas: fees, reward } = history as {
      baseFeePerGas: unknown[];
      reward?: unknown[][] | null;
    };
    const height = Number(quantity(number));
    this.seen = Math.max(this.seen, height);
    return {
      number: height,
      timestamp: Number(quantity(timestamp)),
      baseFeePerGas: quantity(baseFeePerGas),
      nextBaseFee: quantity(fees[fees.length - 1]),
      tips: (reward ?? []).map((r) => (r.length > 0 ? quantity(r[0]) : 0n)),
      ...(opStack ? { dataFee: read(answers.slice(2)) } : {}),
    };
  }

  /** Whether it's an OP-stack chain: its fee oracle has code. */
  async opStack(): Promise<boolean> {
    const code = await this.rpc("eth_getCode", [GAS_PRICE_ORACLE, "latest"]);
    return code !== "0x" && code !== "0x0";
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
 * and `priority`; what's paid beyond gas, its L1 fees risen for them too. A transaction pays
 * only the base fee of its block, and the tip. */
export function lastingFees(latest: Latest, priority: bigint, blocks: number): Fees {
  return {
    maxFeePerGas: risen(latest.nextBaseFee, blocks) + priority,
    maxPriorityFeePerGas: priority,
    ...(latest.dataFee === undefined ? {} : { dataFee: risenDataFee(latest.dataFee, blocks) }),
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

/** One request's answer: its result, or its error answer; one that's neither is the node's
 * failure. */
function answerTo(request: { id: number; method: string }, body: unknown): Answer {
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    throw new ChainError(`${request.method}: no answer`);
  }
  const answer = body as Record<string, unknown>;
  if (answer.error !== undefined && answer.error !== null) {
    const error = answer.error as Record<string, unknown>;
    if (typeof error !== "object" || !Number.isInteger(error.code)) {
      throw new ChainError(`${request.method}: an error that isn't one`);
    }
    return {
      error: new JsonRpcError(
        request.method,
        error.code as number,
        String(error.message),
        error.data,
      ),
    };
  }
  if (!("result" in answer)) throw new ChainError(`${request.method}: no result`);
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
