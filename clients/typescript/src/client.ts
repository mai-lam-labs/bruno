/**
 * A client of a Bruno relay (SPEC.md): its terms, a call's quote, a submission paid by the call
 * itself, a submission's status, and each other way of paying (`x402`).
 */

import type { Authorization, Call, Hex, Quote, Signer, Stage, Status, Terms } from "./types.ts";
import {
  type Amount,
  baseUnitsOf,
  CHAIN_IDS,
  type PayWith,
  type Token,
  tokenOf,
  type Value,
  valueOf,
} from "./tokens.ts";
import {
  decodePaymentRequired,
  decodeSettlement,
  pay,
  type PaymentRequired,
  type Requirement,
  transferFor,
  usableRequirements,
} from "./x402.ts";

/** Bruno's own deployment. */
export const DEFAULT_URL = "https://bruno.mailam.app";
/** How much longer than its maxTimeoutSeconds a paid request is waited for: its answer's way
 * back. */
const HELD_SLACK_SECONDS = 15;
/** How often a submission's status is read while waiting for it. */
const POLL_SECONDS = 4;

export class BrunoError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(message: string, status: number, body: unknown) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

/** A payment refused by the relay: why (x402's error reason), and nothing was spent. */
export class PaymentRefused extends BrunoError {
  readonly reason: string;

  constructor(reason: string, body: unknown) {
    super(`payment refused: ${reason}`, 402, body);
    this.reason = reason;
  }
}

/** None of the tokens a client will pay in can pay, each for its reason: nothing was signed. */
export class NoPayment extends BrunoError {
  readonly reasons: readonly string[];

  constructor(reasons: readonly string[]) {
    super(`no token to pay in: ${reasons.join("; ")}`, 402, null);
    this.reasons = reasons;
  }
}

/** A call paid for, settled: its transaction, what was paid, and where its status is. */
export interface Paid {
  readonly transaction: Hex;
  readonly payer: Hex | null;
  readonly statusUrl: string | null;
  readonly paid: Value;
}

export interface BrunoOptions {
  /** The relay, `DEFAULT_URL` if not given. */
  readonly url?: string;
  /** The chain, by name ("ethereum", "sepolia") or id. */
  readonly chain: string | number;
  /** `fetch`, if not the global one. */
  readonly fetch?: typeof fetch;
  /** The chain's tokens beyond its built-in aliases (Ethereum's and Sepolia's): each one's
   * address and decimals, and the alias it's named by. */
  readonly tokens?: readonly Token[];
}

/** A call to pay for: the call, its gas, and any EIP-7702 authorizations. */
export interface PaidRequest {
  readonly call: Call;
  readonly gas: bigint | number;
  readonly authorizationList?: readonly Authorization[];
}

/** The relay's HTTP API, on one chain. */
class Relay {
  readonly url: string;
  readonly path: string;
  readonly #fetcher: typeof fetch;

  constructor(url: string, path: string, fetcher: typeof fetch) {
    this.url = url;
    this.path = path;
    this.#fetcher = fetcher;
  }

  async json(method: string, path: string, body?: unknown): Promise<unknown> {
    const response = await this.send(method, path, body);
    const parsed = await read(response);
    if (!response.ok) {
      throw new BrunoError(`${method} ${path}: ${response.status}`, response.status, parsed);
    }
    return parsed;
  }

  send(
    method: string,
    path: string,
    body?: unknown,
    headers: Record<string, string> = {},
    timeoutMs = 30_000,
  ): Promise<Response> {
    return this.#fetcher(`${this.url}/${this.path}${path}`, {
      method,
      headers: body === undefined ? headers : { "content-type": "application/json", ...headers },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      signal: AbortSignal.timeout(timeoutMs),
    });
  }
}

export class Bruno {
  readonly url: string;
  /** The chain's path segment: its name, else its id. */
  readonly chain: string;
  readonly chainId: number;
  /** Paying over x402: an EIP-3009 token's signed transfer, the call run from the relay's
   * contract. */
  readonly x402: X402;
  private readonly relay: Relay;
  readonly #tokens: readonly Token[];

  constructor(options: BrunoOptions) {
    this.url = (options.url ?? DEFAULT_URL).replace(/\/$/, "");
    this.chainId = chainIdOf(options.chain);
    this.chain = Object.keys(CHAIN_IDS).find((n) => CHAIN_IDS[n] === this.chainId) ??
      String(this.chainId);
    this.#tokens = options.tokens ?? [];
    this.relay = new Relay(this.url, this.chain, options.fetch ?? fetch);
    this.x402 = new X402(this.relay, this.chainId, (name) => this.token(name));
  }

  /** The token `name` names on this chain: an alias (built in, or given in `tokens`) or an
   * address. */
  token(name: string): Token {
    return tokenOf(this.chainId, name, this.#tokens);
  }

  /** The relay's terms on this chain. */
  async terms(): Promise<Terms> {
    const t = await this.relay.json("GET", "/v1/config") as Record<string, string | string[]>;
    return {
      payee: t.payee as Hex,
      tokens: t.tokens as Hex[],
      maxGas: BigInt(t.maxGas as string),
      maxCallBytes: Number(t.maxCallBytes),
    };
  }

  /** What a call given `gas`, of `callBytes` bytes (with an authorization from `authority`, if
   * any), costs in each of `tokens` (aliases or addresses). */
  async quote(
    gas: bigint | number,
    callBytes: number,
    tokens: readonly string[],
    authority?: Hex,
  ): Promise<Quote> {
    const named = tokens.map((t) => this.token(t));
    const query = new URLSearchParams({ gas: hex(gas), callBytes: hex(callBytes) });
    for (const t of named) query.append("tokens", t.address);
    if (authority !== undefined) query.set("authority", authority);
    const q = await this.relay.json("GET", `/v1/quote?${query}`) as {
      validUntilBlock: string;
      quotes: { token: Hex; amount: string }[];
    };
    return {
      validUntilBlock: BigInt(q.validUntilBlock),
      prices: q.quotes.map((x) => valueOf(this.token(x.token), BigInt(x.amount))),
    };
  }

  /** Submit a call that pays the relay itself (its payee gains `amount` of `token`, an alias
   * or address, during it), included by `maxBlockNumber` or not at all: its submission's id. */
  async submit(submission: {
    call: Call;
    gas: bigint | number;
    token: string;
    amount: Amount;
    maxBlockNumber: bigint | number;
    authorizationList?: readonly Authorization[];
  }): Promise<Hex> {
    const token = this.token(submission.token);
    const amount = baseUnitsOf(submission.amount, token);
    const { payee } = await this.terms();
    const sent = await this.relay.json("POST", "/v1/submissions", {
      call: submission.call,
      gas: hex(submission.gas),
      payment: { token: token.address, payee, amount: hex(amount) },
      maxBlockNumber: hex(submission.maxBlockNumber),
      authorizationList: submission.authorizationList ?? [],
    }) as { id: Hex };
    return sent.id;
  }

  /** A submission's status. */
  async status(id: Hex): Promise<Status> {
    return statusOf(await this.relay.json("GET", `/v1/submissions/${id}`));
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
}

/** One way to pay over x402: a requirement, and its price. */
export interface Offer {
  readonly requirement: Requirement;
  readonly price: Value;
}

/** A call's price over x402: the 402's requirements, and those this client pays by. */
export interface X402Price {
  readonly required: PaymentRequired;
  readonly offers: readonly Offer[];
}

/**
 * Paying over x402 (`exact`, EIP-3009): the payer signs a transfer of the price, which the
 * relay's contract makes in the call's transaction, only if the call succeeds. The call runs
 * from that contract, not the payer's account; only EIP-3009 tokens (USDC) pay this way.
 */
export class X402 {
  readonly #relay: Relay;
  readonly #chainId: number;
  readonly #token: (name: string) => Token;

  constructor(relay: Relay, chainId: number, token: (name: string) => Token) {
    this.#relay = relay;
    this.#chainId = chainId;
    this.#token = token;
  }

  /** What a call costs over x402, asked without paying: the 402, and each way to pay it. */
  async price(request: PaidRequest): Promise<X402Price> {
    const asked = await this.#relay.send("POST", "/v1/x402", x402Body(request));
    if (asked.status !== 402) {
      throw new BrunoError(`expected a 402, got ${asked.status}`, asked.status, await read(asked));
    }
    const header = asked.headers.get("PAYMENT-REQUIRED");
    if (header === null) throw new BrunoError("a 402 without PAYMENT-REQUIRED", 402, null);
    const required = decodePaymentRequired(header);
    // only this chain's: a requirement on another would have the payer sign a transfer there
    const network = `eip155:${this.#chainId}`;
    const usable = usableRequirements(required).filter((r) => r.network === network);
    const offers = usable.map((requirement) => ({
      requirement,
      price: valueOf(this.#token(requirement.asset), BigInt(requirement.amount)),
    }));
    return { required, offers };
  }

  /**
   * A call paid over x402 by `signer`, in the first of `pay`'s tokens offered within its `max`:
   * asked its price (the 402), signed for, and sent paid, held by the relay until its
   * transaction is included. The payment moves only in that transaction: a call that fails or
   * never lands costs nothing. If no token is within its `max`, nothing is signed (`NoPayment`).
   */
  async payAndSend(
    request: PaidRequest,
    signer: Signer,
    options: { readonly pay: readonly PayWith[] },
  ): Promise<Paid> {
    const { required, offers } = await this.price(request);
    const { offer, value } = chosen(this.#token, offers, options.pay);
    const transfer = transferFor(offer.requirement, signer.address, value.baseUnits);
    const signature = await pay(required, offer.requirement, transfer, signer);
    const paid = await this.#relay.send("POST", "/v1/x402", x402Body(request), {
      "PAYMENT-SIGNATURE": signature,
    }, (offer.requirement.maxTimeoutSeconds + HELD_SLACK_SECONDS) * 1000);
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
      statusUrl: location === null ? null : new URL(location, this.#relay.url).toString(),
      paid: value,
    };
  }
}

/** The first of `pay` an offer pays within its `max`, and what it pays (the price and its
 * tip); none, each entry's reason. */
function chosen(
  named: (name: string) => Token,
  offers: readonly Offer[],
  pay: readonly PayWith[],
): { offer: Offer; value: Value } {
  const reasons: string[] = [];
  for (const entry of pay) {
    const token = named(entry.token);
    const offer = offers.find((o) =>
      o.requirement.asset.toLowerCase() === token.address.toLowerCase()
    );
    if (offer === undefined) {
      reasons.push(`${entry.token}: not offered`);
      continue;
    }
    const total = offer.price.baseUnits +
      (entry.tip === undefined ? 0n : baseUnitsOf(entry.tip, token));
    const max = baseUnitsOf(entry.max, token);
    if (total > max) {
      const shown = (v: bigint) => valueOf(token, v).decimal ?? `${v} base units`;
      reasons.push(`${entry.token}: ${shown(total)} is over its max, ${shown(max)}`);
      continue;
    }
    return { offer, value: valueOf(token, total) };
  }
  throw new NoPayment(reasons.length === 0 ? ["no tokens given"] : reasons);
}

/** A chain by name or id: its id. */
function chainIdOf(chain: string | number): number {
  if (typeof chain === "number") return chain;
  if (/^[1-9][0-9]*$/.test(chain)) return Number(chain);
  const id = CHAIN_IDS[chain];
  if (id === undefined) {
    throw new BrunoError(`no chain ${chain} (${Object.keys(CHAIN_IDS).join(", ")})`, 0, null);
  }
  return id;
}

function x402Body(request: PaidRequest): unknown {
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
