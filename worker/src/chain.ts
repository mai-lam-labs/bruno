/**
 * A chain through its nodes, over JSON-RPC: reads, fees, receipts and sending. Each call goes
 * to the first node that answers.
 */

import { type Address, bytes, checksum, type Hex, hex } from "./evm.ts";
import { type Forks, type GasSchedule, scheduleAt } from "./forks.ts";
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

/** How long a node is given to answer one call: past it, the next node is asked. */
export const RPC_TIMEOUT_MS = 10_000;

let ids = 0;

export class Chain {
  constructor(
    readonly chainId: number,
    readonly rpcUrls: readonly string[],
    readonly forks: Forks,
    /** The chain's block time: the next block is a slot after the latest. */
    readonly slotSeconds: number,
  ) {}

  /** `method` at the first node that answers; a node's error answer is raised as it is. */
  async rpc(method: string, params: unknown[]): Promise<unknown> {
    const failures: string[] = [];
    for (const url of this.rpcUrls) {
      let body: { result?: unknown; error?: { code: number; message: string; data?: unknown } };
      try {
        const response = await fetch(url, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ jsonrpc: "2.0", id: ++ids, method, params }),
          signal: AbortSignal.timeout(RPC_TIMEOUT_MS),
        });
        if (!response.ok) {
          failures.push(`${url}: HTTP ${response.status}`);
          continue;
        }
        body = await response.json();
      } catch (e) {
        // no answer in time, or none that parses: the next node
        failures.push(`${url}: ${e}`);
        continue;
      }
      if (body.error !== undefined) {
        throw new JsonRpcError(method, body.error.code, body.error.message, body.error.data);
      }
      return body.result;
    }
    throw new ChainError(`${method}: no node answered: ${failures.join("; ")}`);
  }

  async checkChain(): Promise<void> {
    const chainId = Number(quantity(await this.rpc("eth_chainId", [])));
    if (chainId !== this.chainId) {
      throw new ChainError(`the nodes are chain ${chainId}, not ${this.chainId}`);
    }
  }

  async head(): Promise<number> {
    return Number(quantity(await this.rpc("eth_blockNumber", [])));
  }

  async balance(account: Address): Promise<bigint> {
    return quantity(await this.rpc("eth_getBalance", [account, "latest"]));
  }

  async nonce(account: Address): Promise<number> {
    return Number(quantity(await this.rpc("eth_getTransactionCount", [account, "latest"])));
  }

  async code(account: Address): Promise<Hex> {
    return hexOf(await this.rpc("eth_getCode", [account, "latest"]));
  }

  /** eth_call of `data` to `to`, at the latest block. */
  async call(to: Address, data: Uint8Array): Promise<Uint8Array> {
    return bytes(hexOf(await this.rpc("eth_call", [{ to, data: hex(data) }, "latest"])));
  }

  async latest(): Promise<{ number: number; timestamp: number; baseFeePerGas: bigint }> {
    const block = await this.rpc("eth_getBlockByNumber", ["latest", false]);
    const { number, timestamp, baseFeePerGas } = block as Record<string, unknown>;
    return {
      number: Number(quantity(number)),
      timestamp: Number(quantity(timestamp)),
      baseFeePerGas: quantity(baseFeePerGas),
    };
  }

  /** The gas schedule of the next block: the one active a slot after the latest's. */
  async schedule(): Promise<GasSchedule> {
    return scheduleAt(this.forks, (await this.latest()).timestamp + this.slotSeconds);
  }

  /** The next block's base fee: eth_feeHistory's last. */
  async nextBaseFee(): Promise<bigint> {
    const history = await this.rpc("eth_feeHistory", ["0x1", "latest", []]);
    const fees = (history as { baseFeePerGas: unknown[] }).baseFeePerGas;
    return quantity(fees[fees.length - 1]);
  }

  /** Fees valid through several blocks of rising base fee: twice the base fee, and `priority`. */
  async fees(priority: bigint): Promise<Fees> {
    const base = (await this.latest()).baseFeePerGas;
    return { maxFeePerGas: 2n * base + priority, maxPriorityFeePerGas: priority };
  }

  async receipt(hash: Uint8Array): Promise<Receipt | null> {
    const receipt = await this.rpc("eth_getTransactionReceipt", [hex(hash)]);
    if (receipt === null) {
      return null;
    }
    const { status, blockNumber } = receipt as Record<string, unknown>;
    return { status: hexOf(status), blockNumber: Number(quantity(blockNumber)) };
  }

  async sendRaw(raw: Uint8Array): Promise<void> {
    await this.rpc("eth_sendRawTransaction", [hex(raw)]);
  }
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
