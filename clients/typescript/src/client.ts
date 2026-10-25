/**
 * A client of a Bruno relay (SPEC.md): its terms, a call's quote, a submission paid by the call
 * itself, a submission's status, and a call paid over x402 with any signer of typed data.
 */

import type { Authorization, Call, Hex, Quote, Signer, Stage, Status, Terms } from "./types.ts";
import {
  chooseRequirement,
  decodePaymentRequired,
  decodeSettlement,
  pay,
  type PaymentRequired,
  type Requirement,
  transferFor,
} from "./x402.ts";

/** Bruno's own deployment. */
export const DEFAULT_URL = "https://bruno.mailam.app";
/** How much longer than its maxTimeoutSeconds a paid request is waited for: its answer's way
 * back. */
const HELD_SLACK_SECONDS = 15;
/** How often a submission's status is read while waiting for it. */
const POLL_SECONDS = 4;

export class BrunoError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly body: unknown,
  ) {
    super(message);
  }
}

/** An x402 payment refused: why (x402's error reason), and nothing was spent. */
export class PaymentRefused extends BrunoError {
  constructor(
    readonly reason: string,
    body: unknown,
  ) {
    super(`payment refused: ${reason}`, 402, body);
  }
}

/** A call paid over x402, settled: its transaction, and where its status is. */
export interface Paid {
  readonly transaction: Hex;
  readonly payer: Hex | null;
  readonly statusUrl: string | null;
  readonly amount: bigint;
}

export interface BrunoOptions {
  /** The relay, `DEFAULT_URL` if not given. */
  readonly url?: string;
  /** The chain, by name ("ethereum", "sepolia") or id. */
  readonly chain: string | number;
  /** `fetch`, if not the global one. */
  readonly fetch?: typeof fetch;
}

export interface PayOptions {
  /** The token to pay in, if the relay takes more than one. */
  readonly asset?: Hex;
  /** The most to pay, in the token's base units: a price over it is refused unsigned. */
  readonly maxAmount?: bigint;
  /** Paid on top of the price, ranking the call ahead of others when the queue is full. */
  readonly tip?: bigint;
}

export class Bruno {
  readonly url: string;
  readonly chain: string;
  private readonly fetcher: typeof fetch;

  constructor(options: BrunoOptions) {
    this.url = (options.url ?? DEFAULT_URL).replace(/\/$/, "");
    this.chain = String(options.chain);
    this.fetcher = options.fetch ?? fetch;
  }

  /** The relay's terms on this chain. */
  async terms(): Promise<Terms> {
    const t = await this.json("GET", "/v1/config") as Record<string, string | string[]>;
    return {
      payee: t.payee as Hex,
      tokens: t.tokens as Hex[],
      maxGas: BigInt(t.maxGas as string),
      maxCallBytes: Number(t.maxCallBytes),
    };
  }

  /** What a call given `gas`, of `callBytes` bytes (with an authorization from `authority`, if
   * any), costs in each of `tokens`. */
  async quote(
    gas: bigint | number,
    callBytes: number,
    tokens: readonly Hex[],
    authority?: Hex,
  ): Promise<Quote> {
    const query = new URLSearchParams({ gas: hex(gas), callBytes: hex(callBytes) });
    for (const t of tokens) query.append("tokens", t);
    if (authority !== undefined) query.set("authority", authority);
    const q = await this.json("GET", `/v1/quote?${query}`) as {
      validUntilBlock: string;
      quotes: { token: Hex; amount: string }[];
    };
    return {
      validUntilBlock: BigInt(q.validUntilBlock),
      amounts: new Map(q.quotes.map((x) => [x.token, BigInt(x.amount)])),
    };
  }

  /** Submit a call that pays the relay itself (its payee gains `amount` of `token` during
   * it), included by `maxBlockNumber` or not at all: its submission's id. */
  async submit(submission: {
    call: Call;
    gas: bigint | number;
    token: Hex;
    amount: bigint;
    maxBlockNumber: bigint | number;
    authorizationList?: readonly Authorization[];
  }): Promise<Hex> {
    const { payee } = await this.terms();
    const sent = await this.json("POST", "/v1/submissions", {
      call: submission.call,
      gas: hex(submission.gas),
      payment: { token: submission.token, payee, amount: hex(submission.amount) },
      maxBlockNumber: hex(submission.maxBlockNumber),
      authorizationList: submission.authorizationList ?? [],
    }) as { id: Hex };
    return sent.id;
  }

  /** A submission's status. */
  async status(id: Hex): Promise<Status> {
    return statusOf(await this.json("GET", `/v1/submissions/${id}`));
  }

  /** A submission's status once it's `done` or `failed` (or included, with `until`
   * "confirming"), read every few seconds. */
  async wait(id: Hex, until: Stage = "done", timeoutSeconds = 600): Promise<Status> {
    const deadline = Date.now() + timeoutSeconds * 1000;
    while (true) {
      const status = await this.status(id);
      if (status.stage === "failed" || status.stage === until || status.stage === "done") {
        return status;
      }
      if (Date.now() > deadline) throw new BrunoError(`${id} still ${status.stage}`, 0, status);
      await new Promise((r) => setTimeout(r, POLL_SECONDS * 1000));
    }
  }

  /**
   * A call paid over x402 by `signer`, holding only the token: asked its price (the 402),
   * signed for, and sent paid, held by the relay until its transaction is included. The payment
   * moves only in that transaction: a call that fails or never lands costs nothing.
   */
  async payAndSend(
    request: X402Request,
    signer: Signer,
    options: PayOptions = {},
  ): Promise<Paid> {
    const { required, requirement } = await this.price(request, options.asset);
    const body = x402Body(request);
    const amount = BigInt(requirement.amount) + (options.tip ?? 0n);
    if (options.maxAmount !== undefined && amount > options.maxAmount) {
      throw new BrunoError(`the price, ${amount}, is over ${options.maxAmount}`, 402, required);
    }
    const transfer = transferFor(requirement, signer.address, amount);
    const signature = await pay(required, requirement, transfer, signer);
    const paid = await this.send("POST", "/v1/x402", body, {
      "PAYMENT-SIGNATURE": signature,
    }, (requirement.maxTimeoutSeconds + HELD_SLACK_SECONDS) * 1000);
    const response = paid.headers.get("PAYMENT-RESPONSE");
    const settlement = response === null ? null : decodeSettlement(response);
    if (paid.status === 402) {
      throw new PaymentRefused(settlement?.errorReason ?? "unknown", await read(paid));
    }
    if (paid.status !== 200 || settlement === null || settlement.transaction === "") {
      throw new BrunoError(`not settled: ${paid.status}`, paid.status, await read(paid));
    }
    const location = paid.headers.get("Location");
    return {
      transaction: settlement.transaction,
      payer: settlement.payer ?? null,
      statusUrl: location === null ? null : new URL(location, this.url).toString(),
      amount,
    };
  }

  /** What a call costs over x402, asked without paying: the 402's requirements, and the one
   * this client pays by (in `asset`, if given). */
  async price(
    request: X402Request,
    asset?: Hex,
  ): Promise<{ required: PaymentRequired; requirement: Requirement }> {
    const asked = await this.send("POST", "/v1/x402", x402Body(request));
    if (asked.status !== 402) {
      throw new BrunoError(`expected a 402, got ${asked.status}`, asked.status, await read(asked));
    }
    const header = asked.headers.get("PAYMENT-REQUIRED");
    if (header === null) throw new BrunoError("a 402 without PAYMENT-REQUIRED", 402, null);
    const required = decodePaymentRequired(header);
    return { required, requirement: chooseRequirement(required, asset) };
  }

  private async json(method: string, path: string, body?: unknown): Promise<unknown> {
    const response = await this.send(method, path, body);
    const parsed = await read(response);
    if (!response.ok) {
      throw new BrunoError(`${method} ${path}: ${response.status}`, response.status, parsed);
    }
    return parsed;
  }

  private send(
    method: string,
    path: string,
    body?: unknown,
    headers: Record<string, string> = {},
    timeoutMs = 30_000,
  ): Promise<Response> {
    return this.fetcher(`${this.url}/${this.chain}${path}`, {
      method,
      headers: body === undefined ? headers : { "content-type": "application/json", ...headers },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      signal: AbortSignal.timeout(timeoutMs),
    });
  }
}

/** A call to pay for over x402: the call, its gas, and any EIP-7702 authorizations. */
export interface X402Request {
  readonly call: Call;
  readonly gas: bigint | number;
  readonly authorizationList?: readonly Authorization[];
}

function x402Body(request: X402Request): unknown {
  return {
    call: request.call,
    gas: hex(request.gas),
    authorizationList: request.authorizationList ?? [],
  };
}

/** `n` as a JSON-RPC quantity. */
export function hex(n: bigint | number): Hex {
  return `0x${BigInt(n).toString(16)}`;
}

async function read(response: Response): Promise<unknown> {
  const text = await response.text();
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function statusOf(raw: unknown): Status {
  const s = raw as Record<string, string | null>;
  const quantity = (
    v: string | null | undefined,
  ) => (v === null || v === undefined ? null : BigInt(v));
  return {
    status: s.status as string,
    stage: s.stage as Stage,
    attempts: Number(s.attempts),
    transactionHash: (s.transactionHash ?? null) as Hex | null,
    blockNumber: quantity(s.blockNumber),
    confirmations: quantity(s.confirmations),
    reason: s.reason ?? null,
    revertData: (s.revertData ?? null) as Hex | null,
  };
}
