/**
 * A client of a Bruno relay (SPEC.md): its terms, a call's quote, a submission paid by the call
 * itself or by its payer's signed transfer (`payAndSubmit`), an account's own transactions
 * sponsored (`sponsor`), a submission's status, and paying over x402 (`x402`). A smart account that isn't deployed yet is deployed by its first payment:
 * its deployment priced, from its factory or its signature's ERC-6492 wrapper.
 */

import { checkedTransfer, type Transfer, transferOf, transferTypedData } from "./eip3009.ts";
import { signingOf, withWrapper } from "./erc6492.ts";
import { type Rejection, REJECTIONS } from "./refusals.ts";
import { BrunoError, hex, Relay } from "./relay.ts";
import {
  type Amount,
  baseUnitsOf,
  CHAIN_IDS,
  type Token,
  tokenOf,
  type Value,
  valueOf,
} from "./tokens.ts";
import type {
  Authorization,
  Breakdown,
  Call,
  Deployment,
  Hex,
  PaidRequest,
  Part,
  Quote,
  QuoteRequest,
  Signer,
  Signing,
  Stage,
  Status,
  Terms,
  TypedData,
} from "./types.ts";
import {
  checkedSponsored,
  type SponsoredCall,
  type SponsoredQuote,
  type ToSignJson,
  toSignOf,
  type TransactionSigner,
} from "./sponsored.ts";
import { NoPayment, X402 } from "./x402_client.ts";

/** Bruno's own deployment. */
export const DEFAULT_URL = "https://bruno.mailam.app";
/** How often a submission's status is read while waiting for it. */
const POLL_SECONDS = 4;
/** How long a submission's signed transfer is valid for: past any deadline it's given. */
const TRANSFER_SECONDS = 3_600;

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
      forwarder: t.forwarder as Hex,
      tokens: t.tokens as Hex[],
      maxGas: BigInt(t.maxGas as string),
      maxCallDataLength: Number(t.maxCallDataLength),
    };
  }

  /** What `request` costs in each of `tokens` (aliases or addresses), and the gas it's priced
   * for (the relay's estimate of the call, unless given); paid by a signed transfer if `signing`
   * names its payer. */
  async quote(
    request: QuoteRequest,
    tokens: readonly string[],
    signing: Signing = {},
  ): Promise<Quote> {
    const q = await this.relay.json("POST", "/v1/quote", {
      tokens: tokens.map((t) => this.token(t).address),
      ...(request.call === undefined ? {} : { call: request.call }),
      ...(request.callDataLength === undefined
        ? {}
        : { callDataLength: hex(request.callDataLength) }),
      ...(request.gas === undefined ? {} : { gas: hex(request.gas) }),
      ...(request.authorizationList === undefined
        ? {}
        : { authorizationList: request.authorizationList }),
      ...(request.authority === undefined ? {} : { authority: request.authority }),
      ...(signing.payer === undefined ? {} : { payer: signing.payer }),
      ...(signing.verifyGas === undefined ? {} : { verifyGas: hex(signing.verifyGas) }),
      ...(signing.deployment === undefined ? {} : { deployment: signing.deployment }),
    }) as {
      validUntilBlock: string;
      quotes: {
        token: Hex;
        amount: string;
        breakdown?: Record<keyof Breakdown, { gas: string; amount: string }>;
        typedData?: TypedData;
      }[];
      gas: string;
    };
    const part = (p: { gas: string; amount: string }): Part => ({
      gas: BigInt(p.gas),
      amount: BigInt(p.amount),
    });
    return {
      validUntilBlock: BigInt(q.validUntilBlock),
      prices: q.quotes.map((x) => valueOf(this.token(x.token), BigInt(x.amount))),
      gas: BigInt(q.gas),
      breakdowns: q.quotes.map((x) =>
        x.breakdown === undefined ? null : {
          setup: part(x.breakdown.setup),
          call: part(x.breakdown.call),
          payment: part(x.breakdown.payment),
          relay: part(x.breakdown.relay),
        }
      ),
      transfers: q.quotes.map((x) => x.typedData ?? null),
    };
  }

  /** Submit a call that pays the relay itself (its payee gains `amount` of `token`, an alias
   * or address, during it), included by `maxBlockNumber` or not at all: its submission's id. An
   * account named in `deployment` is deployed first, if it has no code. */
  async submit(submission: {
    call: Call;
    gas: bigint | number;
    token: string;
    amount: Amount;
    maxBlockNumber: bigint | number;
    authorizationList?: readonly Authorization[];
    deployment?: Deployment;
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
      ...(submission.deployment === undefined ? {} : { deployment: submission.deployment }),
    }) as { id: Hex };
    return sent.id;
  }

  /**
   * Submit a call paid by `signer`'s signed transfer of `token` (an EIP-3009 token: USDC), the
   * relay's contract making it in the call's transaction, before the call: quoted for `signer`
   * as its payer (its deployment too, while a smart account is undeployed), signed at that
   * price if within `max`, and sent, included within `deadlineBlocks` of the quote's block or
   * not at all. Answered underpaid (the price risen since the quote), signed once more at what
   * the relay requires, if within `max`, and sent again, as an x402 payment is. Its
   * submission's id, and what it pays.
   */
  async payAndSubmit(
    request: PaidRequest,
    signer: Signer,
    options: { readonly token: string; readonly max: Amount; readonly deadlineBlocks?: number },
  ): Promise<{ id: Hex; paid: Value }> {
    const token = this.token(options.token);
    const { payee } = await this.terms();
    let signing = await signingOf(request, signer);
    for (let tries = 0;; tries++) {
      const quote = await this.quote(
        {
          call: request.call,
          ...(request.gas === undefined ? {} : { gas: request.gas }),
          ...(request.authorizationList === undefined
            ? {}
            : { authorizationList: request.authorizationList }),
        },
        [token.address],
        signing,
      );
      const price = quote.prices[0];
      if (price === undefined) throw new BrunoError("no price", 0, quote);
      if (price.baseUnits > baseUnitsOf(options.max, token)) {
        throw new NoPayment([
          `${options.token}: ${price.decimal ?? price.baseUnits} is over its max`,
        ]);
      }
      // the relay's transfer to sign, checked to be this payment; its domain where the client
      // knows none
      const given = quote.transfers[0] ?? null;
      const domain = token.eip3009 ?? given?.domain;
      if (domain === undefined) {
        throw new BrunoError(`${options.token}: no EIP-3009 domain known to sign for`, 0, null);
      }
      const sign = async (amount: bigint) => {
        const transfer = transferOf(signer.address, payee, amount, TRANSFER_SECONDS);
        const signature = await signer.signTypedData(
          transferTypedData(this.chainId, token.address, domain, transfer),
        );
        return { transfer, signature };
      };
      const signGiven = async (typed: TypedData) => {
        const transfer = checkedTransfer(typed, {
          chainId: this.chainId,
          token: token.address,
          from: signer.address,
          to: payee,
          value: price.baseUnits,
        });
        return { transfer, signature: await signer.signTypedData(typed) };
      };
      const first = given === null ? await sign(price.baseUnits) : await signGiven(given);
      const declared = withWrapper(signing, signer.address, first.signature);
      if (declared !== signing && tries === 0) { // its deployment, unpriced: priced, signed again
        signing = declared;
        continue;
      }
      const send = async (paid: Value, signed: { transfer: Transfer; signature: Hex }) =>
        await this.relay.json("POST", "/v1/submissions", {
          call: request.call,
          gas: hex(quote.gas),
          authorizationList: request.authorizationList ?? [],
          payment: {
            token: token.address,
            payee,
            amount: hex(paid.baseUnits),
            transfer: transferJson(signed.transfer, signed.signature),
          },
          maxBlockNumber: hex(quote.validUntilBlock + BigInt(options.deadlineBlocks ?? 0)),
          ...(signing.deployment === undefined ? {} : { deployment: signing.deployment }),
        }) as { id: Hex };
      try {
        return { id: (await send(price, first)).id, paid: price };
      } catch (e) {
        const required = underpaidBy(e, token.address);
        if (required === null) throw e;
        if (required > baseUnitsOf(options.max, token)) {
          throw new NoPayment([`${options.token}: ${required} is over its max`]);
        }
        const asked = valueOf(token, required);
        return { id: (await send(asked, await sign(required))).id, paid: asked };
      }
    }
  }

  /**
   * What sponsoring `calls`, each a transaction of `from`'s own, costs in each of `tokens`
   * (aliases or addresses): each filled in for `from` to sign, and the payment in each token to
   * sign after them, to land by `maxBlockNumber` (the furthest the relay takes, if not given: a
   * nearer one costs less, the fees it must allow for rising with each block). An answer that
   * isn't in the tokens asked for, in their order, or that's to land later than was asked, is
   * refused. Check what it gives to sign before signing it (checkedSponsored); `sponsor` does.
   */
  async quoteSponsored(
    from: Hex,
    calls: readonly SponsoredCall[],
    tokens: readonly string[],
    options: { readonly maxBlockNumber?: bigint } = {},
  ): Promise<SponsoredQuote> {
    const asked = tokens.map((t) => this.token(t));
    const q = await this.relay.json("POST", "/v1/quote", {
      tokens: asked.map((t) => t.address),
      ...(options.maxBlockNumber === undefined
        ? {}
        : { maxBlockNumber: hex(options.maxBlockNumber) }),
      sponsored: {
        transactions: calls.map((call) => ({
          from,
          ...(call.to === undefined ? {} : { to: call.to }),
          data: call.data ?? "0x",
          value: hex(call.value ?? 0n),
          ...(call.gas === undefined ? {} : { gas: hex(call.gas) }),
        })),
      },
    }) as {
      validUntilBlock: string;
      maxBlockNumber: string;
      forwarder: Hex;
      sponsored: { transactions: (ToSignJson | null)[] };
      quotes: { token: Hex; amount: string; payment?: ToSignJson }[];
    };
    const payments = q.quotes.map((x) => x.payment);
    const transactions = q.sponsored.transactions;
    if (payments.includes(undefined) || transactions.includes(null)) {
      throw new BrunoError("a sponsorship's quote without what to sign", 0, q);
    }
    // a quote in each token asked for, in their order, and no other: a price is compared with
    // the most the account pays of the token it named, and its payment signed in that token
    const quoted = q.quotes.map((x) => x.token.toLowerCase());
    if (quoted.join() !== asked.map((t) => t.address.toLowerCase()).join()) {
      throw new BrunoError("a sponsorship's quote isn't in the tokens asked for", 0, q);
    }
    const maxBlockNumber = BigInt(q.maxBlockNumber);
    if (options.maxBlockNumber !== undefined && maxBlockNumber > options.maxBlockNumber) {
      throw new BrunoError("a sponsorship's quote is to land later than was asked", 0, q);
    }
    return {
      validUntilBlock: BigInt(q.validUntilBlock),
      maxBlockNumber,
      forwarder: q.forwarder,
      transactions: transactions.flatMap((t) => t === null ? [] : [toSignOf(t)]),
      prices: q.quotes.flatMap((x, i) => {
        const token = asked[i];
        return token === undefined ? [] : [valueOf(token, BigInt(x.amount))];
      }),
      payments: payments.flatMap((p) => p === undefined ? [] : [toSignOf(p)]),
    };
  }

  /**
   * Have `calls` sent as `signer`'s own transactions, the relay paying their gas and paid for it
   * in `token` (an alias or address), at most `max`, by a transfer `signer` signs after them: its
   * submission's id, and what it pays. Quoted, checked to be the calls given and a payment of
   * the price to the relay's forwarder and nothing else, signed without being sent, and
   * submitted. The relay sends them with its own as one bundle, for a builder to land whole or
   * not at all, and stops if one lands only part of it (the status's reason `bundle-broken`):
   * that's the builder's to keep, not the chain's, and a signed transaction stays valid while
   * its nonce is unused, whatever `maxBlockNumber` says (the README's "What whole or not at all
   * rests on").
   */
  async sponsor(
    calls: readonly SponsoredCall[],
    signer: TransactionSigner,
    options: { readonly token: string; readonly max: Amount; readonly maxBlockNumber?: bigint },
  ): Promise<{ id: Hex; paid: Value }> {
    const token = this.token(options.token);
    const quote = await this.quoteSponsored(signer.address, calls, [token.address], {
      ...(options.maxBlockNumber === undefined ? {} : { maxBlockNumber: options.maxBlockNumber }),
    });
    const price = quote.prices[0];
    if (price === undefined) throw new BrunoError("no price", 0, quote);
    if (price.baseUnits > baseUnitsOf(options.max, token)) {
      throw new NoPayment([
        `${options.token}: ${price.decimal ?? price.baseUnits} is over its max`,
      ]);
    }
    const signing = checkedSponsored(quote, calls, this.chainId, 0);
    const signed: Hex[] = [];
    for (const transaction of signing) signed.push(await signer.signTransaction(transaction));
    const taken = await this.relay.json("POST", "/v1/submissions", {
      maxBlockNumber: hex(quote.maxBlockNumber),
      payment: { token: token.address, payee: quote.forwarder, amount: hex(price.baseUnits) },
      sponsored: { transactions: signed },
    }) as { id: Hex };
    return { id: taken.id, paid: price };
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

/** A submission's signed transfer, as its payment carries it. */
function transferJson(transfer: Transfer, signature: Hex): unknown {
  return {
    from: transfer.from,
    validAfter: hex(transfer.validAfter),
    validBefore: hex(transfer.validBefore),
    nonce: transfer.nonce,
    signature,
  };
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
    reason: rejectionOf(s.reason),
    revertData: (s.revertData ?? null) as Hex | null,
  };
}

/** What the relay requires in `token`, if `e` is its refusal of a submission as underpaid
 * (`reason` underpaid, its data's `token` and `required`); else null. */
function underpaidBy(e: unknown, token: Hex): bigint | null {
  if (!(e instanceof BrunoError) || e.reason !== "underpaid") return null;
  const data = e.data;
  if (data?.token === undefined || data.token.toLowerCase() !== token.toLowerCase()) return null;
  return data.required === undefined ? null : BigInt(data.required);
}

/** A status' reason, if it's a rejection this client knows. */
function rejectionOf(reason: string | null | undefined): Rejection | null {
  const known: readonly string[] = REJECTIONS;
  return typeof reason === "string" && known.includes(reason) ? reason as Rejection : null;
}
