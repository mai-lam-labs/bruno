/**
 * Paying a relay over x402: a call's price asked (the 402), the first token offered within its
 * max signed for, and the call sent paid; priced again with a smart account's deployment its
 * signature declares, and paid again, once, at a price its own signature check raised.
 */

import { signingOf, withWrapper } from "./erc6492.ts";
import { BrunoError, hex, read, type Relay } from "./relay.ts";
import { baseUnitsOf, type PayWith, type Token, type Value, valueOf } from "./tokens.ts";
import type { Hex, PaidRequest, Signer } from "./types.ts";
import {
  decodePaymentRequired,
  decodeSettlement,
  type PaymentRequired,
  type Requirement,
  type Settlement,
  signedPayment,
  transferFor,
  usableRequirements,
} from "./x402.ts";

/** How much longer than its maxTimeoutSeconds a paid request is waited for: its answer's way
 * back. */
const HELD_SLACK_SECONDS = 15;
/** x402's reason for a payment that isn't the price: answered with the price it needs. */
const VALUE_MISMATCH = "invalid_exact_evm_payload_authorization_value_mismatch";

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

/** A payment signed for an offer: what it pays, its PAYMENT-SIGNATURE header, and the
 * signature in it. */
interface Payment {
  readonly offer: Offer;
  readonly value: Value;
  readonly header: string;
  readonly signature: Hex;
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
    return { required, offers: this.offersOf(required) };
  }

  /** The 402's requirements this client pays by, priced: only this chain's (a requirement on
   * another would have the payer sign a transfer there). */
  private offersOf(required: PaymentRequired): readonly Offer[] {
    const network = `eip155:${this.#chainId}`;
    return usableRequirements(required).filter((r) => r.network === network).map((requirement) => ({
      requirement,
      price: valueOf(this.#token(requirement.asset), BigInt(requirement.amount)),
    }));
  }

  /** `signer`'s payment of `required`, in the first of `pay`'s tokens offered within its
   * `max`. */
  private async paying(
    required: PaymentRequired,
    signer: Signer,
    pay: readonly PayWith[],
  ): Promise<Payment> {
    const { offer, value } = chosen(this.#token, this.offersOf(required), pay);
    const transfer = transferFor(offer.requirement, signer.address, value.baseUnits);
    const signed = await signedPayment(required, offer.requirement, transfer, signer);
    return { offer, value, ...signed };
  }

  /** `request` sent paid by `payment`, held by the relay until it's answered; and its
   * settlement, if it says one. */
  private async sendPaid(
    request: PaidRequest,
    payment: Payment,
  ): Promise<{ response: Response; settlement: Settlement | null }> {
    const response = await this.#relay.send("POST", "/v1/x402", x402Body(request), {
      "PAYMENT-SIGNATURE": payment.header,
    }, (payment.offer.requirement.maxTimeoutSeconds + HELD_SLACK_SECONDS) * 1000);
    const settled = response.headers.get("PAYMENT-RESPONSE");
    return { response, settlement: settled === null ? null : decodeSettlement(settled) };
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
    let asked: PaidRequest = { ...request, ...(await signingOf(request, signer)) };
    let payment = await this.paying((await this.price(asked)).required, signer, options.pay);
    // a wrapped signature (ERC-6492) whose deployment wasn't declared: priced with it, signed
    // again, so its first payment is enough
    const declared = withWrapper(asked, signer.address, payment.signature);
    if (declared !== asked) {
      asked = declared;
      payment = await this.paying((await this.price(asked)).required, signer, options.pay);
    }
    let { response, settlement } = await this.sendPaid(asked, payment);
    // priced short of what its own signature check costs (a contract's, priced as it runs): the
    // 402's new price paid, once, if within its max
    if (response.status === 402 && settlement?.errorReason === VALUE_MISMATCH) {
      const again = response.headers.get("PAYMENT-REQUIRED");
      if (again !== null) {
        payment = await this.paying(decodePaymentRequired(again), signer, options.pay);
        ({ response, settlement } = await this.sendPaid(asked, payment));
      }
    }
    if (response.status === 402) {
      throw new PaymentRefused(settlement?.errorReason ?? "unknown", await read(response));
    }
    if (response.status !== 200 || settlement === null || settlement.transaction === "") {
      throw new BrunoError(
        `not settled: ${response.status}`,
        response.status,
        await read(response),
      );
    }
    const location = response.headers.get("Location");
    return {
      transaction: settlement.transaction,
      payer: settlement.payer ?? null,
      statusUrl: location === null ? null : new URL(location, this.#relay.url).toString(),
      paid: payment.value,
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

/** `request` as x402's resource takes it: the call, and what's known of its payer. */
function x402Body(request: PaidRequest): unknown {
  return {
    call: request.call,
    gas: hex(request.gas),
    authorizationList: request.authorizationList ?? [],
    ...(request.payer === undefined ? {} : { payer: request.payer }),
    ...(request.deployment === undefined ? {} : { deployment: request.deployment }),
    ...(request.verifyGas === undefined ? {} : { verifyGas: hex(request.verifyGas) }),
  };
}
