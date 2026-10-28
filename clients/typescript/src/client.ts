/**
 * A client of a Bruno relay (SPEC.md): its terms, a call's quote, a submission paid by the call
 * itself or by its payer's signed transfer (`payAndSubmit`), a submission's status, and paying
 * over x402 (`x402`). A smart account that isn't deployed yet is deployed by its first payment:
 * its deployment priced, from its factory or its signature's ERC-6492 wrapper.
 */

import { type Transfer, transferOf, transferTypedData } from "./eip3009.ts";
import { signingOf, withWrapper } from "./erc6492.ts";
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
  Call,
  Deployment,
  Hex,
  PaidRequest,
  Quote,
  Signer,
  Signing,
  Stage,
  Status,
  Terms,
} from "./types.ts";
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
      tokens: t.tokens as Hex[],
      maxGas: BigInt(t.maxGas as string),
      maxCallBytes: Number(t.maxCallBytes),
    };
  }

  /** What a call given `gas`, of `callBytes` bytes (with an authorization from `authority`, if
   * any), costs in each of `tokens` (aliases or addresses); paid by a signed transfer if
   * `signing` names its payer. */
  async quote(
    gas: bigint | number,
    callBytes: number,
    tokens: readonly string[],
    authority?: Hex,
    signing: Signing = {},
  ): Promise<Quote> {
    const named = tokens.map((t) => this.token(t));
    const query = new URLSearchParams({ gas: hex(gas), callBytes: hex(callBytes) });
    for (const t of named) query.append("tokens", t.address);
    if (authority !== undefined) query.set("authority", authority);
    if (signing.payer !== undefined) query.set("payer", signing.payer);
    if (signing.verifyGas !== undefined) query.set("verifyGas", hex(signing.verifyGas));
    if (signing.deployment !== undefined) {
      query.set("account", signing.deployment.account);
      query.set("factory", signing.deployment.factory);
      query.set("factoryData", signing.deployment.factoryData);
    }
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
   * not at all. Its submission's id, and what it pays.
   */
  async payAndSubmit(
    request: PaidRequest,
    signer: Signer,
    options: { readonly token: string; readonly max: Amount; readonly deadlineBlocks?: number },
  ): Promise<{ id: Hex; paid: Value }> {
    const token = this.token(options.token);
    const domain = token.eip3009;
    if (domain === undefined) {
      throw new BrunoError(`${options.token}: no EIP-3009 domain known to sign for`, 0, null);
    }
    const { payee } = await this.terms();
    let signing = await signingOf(request, signer);
    const callBytes = (request.call.data.length - 2) / 2;
    for (let tries = 0;; tries++) {
      const quote = await this.quote(
        request.gas,
        callBytes,
        [token.address],
        undefined,
        signing,
      );
      const price = quote.prices[0];
      if (price === undefined) throw new BrunoError("no price", 0, quote);
      if (price.baseUnits > baseUnitsOf(options.max, token)) {
        throw new NoPayment([
          `${options.token}: ${price.decimal ?? price.baseUnits} is over its max`,
        ]);
      }
      const transfer = transferOf(signer.address, payee, price.baseUnits, TRANSFER_SECONDS);
      const signature = await signer.signTypedData(
        transferTypedData(this.chainId, token.address, domain, transfer),
      );
      const declared = withWrapper(signing, signer.address, signature);
      if (declared !== signing && tries === 0) { // its deployment, unpriced: priced, signed again
        signing = declared;
        continue;
      }
      const sent = await this.relay.json("POST", "/v1/submissions", {
        call: request.call,
        gas: hex(request.gas),
        authorizationList: request.authorizationList ?? [],
        payment: {
          token: token.address,
          payee,
          amount: hex(price.baseUnits),
          transfer: transferJson(transfer, signature),
        },
        maxBlockNumber: hex(quote.validUntilBlock + BigInt(options.deadlineBlocks ?? 0)),
        ...(signing.deployment === undefined ? {} : { deployment: signing.deployment }),
      }) as { id: Hex };
      return { id: sent.id, paid: price };
    }
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
    reason: s.reason ?? null,
    revertData: (s.revertData ?? null) as Hex | null,
  };
}
