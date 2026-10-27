/**
 * A stand-in relay on Sepolia, answering as `fetch` would: its 402s and settlements over x402
 * in the relay's own encoding (the Worker's), and its terms, quotes and submissions; each
 * request it was sent kept.
 */

import { encoded, payloadOf } from "../../../worker/src/x402.ts";
import type { Hex } from "../src/types.ts";
import type { PaymentRequired, Requirement, Settlement } from "../src/x402.ts";

// Sepolia's USDC, as its alias names it
export const USDC: Hex = "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238";
export const PAYEE: Hex = "0xfBC0cF0d2C09C57222C871B1cbF9b153bfFA0049";

export const REQUIREMENT: Requirement = {
  scheme: "exact",
  network: "eip155:11155111",
  amount: "86991964",
  asset: USDC,
  payTo: PAYEE,
  maxTimeoutSeconds: 120,
  extra: { name: "USDC", version: "2" },
};
export const REQUIRED: PaymentRequired = {
  x402Version: 2,
  // not ASCII, as a resource's description may be: x402's headers are base64 of UTF-8
  resource: { url: "https://bruno.mailam.app/sepolia/v1/x402", description: "Mai Làm's relay" },
  accepts: [REQUIREMENT],
};

/** A transaction a settlement names. */
export const TRANSACTION: Hex = `0x${"ab".repeat(32)}`;

/** A request the relay was sent: its URL, headers and body. */
export interface Sent {
  readonly url: string;
  readonly headers: Headers;
  readonly body: string;
}

/** A relay answering each request by `answer`; what it was sent. */
export function fakeRelay(
  answer: (request: Sent) => Response,
): { relay: typeof fetch; sent: Sent[] } {
  const sent: Sent[] = [];
  const relay: typeof fetch = (input, init) => {
    const request = {
      url: String(input),
      headers: new Headers(init?.headers),
      body: String(init?.body),
    };
    sent.push(request);
    return Promise.resolve(answer(request));
  };
  return { relay, sent };
}

/** Whether `request` is paid: it carries a PAYMENT-SIGNATURE. */
export function paying(request: Sent): boolean {
  return request.headers.has("PAYMENT-SIGNATURE");
}

/** A 402 asking for `required`, saying `settlement` of a payment refused, if any. */
export function asking(required: PaymentRequired, settlement?: Settlement): Response {
  const headers: Record<string, string> = { "PAYMENT-REQUIRED": encoded(required) };
  if (settlement !== undefined) headers["PAYMENT-RESPONSE"] = encoded(settlement);
  return new Response("{}", { status: 402, headers });
}

/** A payment settled in `TRANSACTION`, its `extra` fields and `headers` added. */
export function settled(
  extra: Partial<Settlement> = {},
  headers: Record<string, string> = {},
): Response {
  const settlement = {
    success: true,
    transaction: TRANSACTION,
    network: REQUIREMENT.network,
    ...extra,
  };
  return new Response("{}", {
    status: 200,
    headers: { "PAYMENT-RESPONSE": encoded(settlement), ...headers },
  });
}

/** A relay that answers 402 at `amounts` in turn while unpaid (and after a payment refused as
 * not the price), settling the payment for `settles`; what it was sent. */
export function relayAt(
  amounts: readonly string[],
  settles: string,
): { relay: typeof fetch; sent: Sent[] } {
  let asked = 0;
  const required = (amount: string) => ({
    ...REQUIRED,
    accepts: [{ ...REQUIREMENT, amount }],
  });
  return fakeRelay((request) => {
    const header = request.headers.get("PAYMENT-SIGNATURE");
    const amount = amounts[Math.min(asked, amounts.length - 1)] as string;
    if (header === null) {
      asked++;
      return asking(required(amount));
    }
    if (payloadOf(header).accepted.amount !== settles) {
      asked++;
      return asking(required(settles), {
        success: false,
        transaction: "",
        network: REQUIREMENT.network,
        errorReason: "invalid_exact_evm_payload_authorization_value_mismatch",
      });
    }
    return settled();
  });
}

/** A relay's terms, its quote of `amount` (a quantity) of USDC valid until block 100, and the
 * submissions it takes. */
export function submitting(amount: Hex): { relay: typeof fetch; sent: Sent[] } {
  return fakeRelay(({ url }) => {
    if (url.endsWith("/v1/config")) {
      return Response.json({
        payee: PAYEE,
        tokens: [USDC],
        maxGas: "0xf4240",
        maxCallBytes: "0xa000",
      });
    }
    if (url.includes("/v1/quote")) {
      return Response.json({ validUntilBlock: "0x64", quotes: [{ token: USDC, amount }] });
    }
    return Response.json({ id: `0x${"cd".repeat(32)}` }, { status: 202 });
  });
}

export const PAY = [{ token: "USDC", max: { decimal: "100" } }];
export const CALL = { call: { to: PAYEE, data: "0x" as Hex }, gas: 100_000n };
