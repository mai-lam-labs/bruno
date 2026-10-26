/**
 * The client's declarations, checked strictly (tsconfig.client.json: no skipLibCheck): a
 * project using @mailam/bruno alone, which has no dependencies, so nothing of anyone else's is
 * checked with it. Type-checked only; smoke.ts runs it.
 */

import {
  type Amount,
  Bruno,
  BrunoError,
  type Hex,
  NoPayment,
  type Paid,
  type PayWith,
  type Quote,
  type Signer,
  type Status,
  type Token,
  tokenOf,
  type TypedData,
  type Value,
  viemSigner,
  type X402Price,
} from "@mailam/bruno";

/** A browser wallet's signer, as the README has one: EIP-712 JSON to eth_signTypedData_v4. */
export function walletSigner(
  address: Hex,
  request: (args: { method: string; params: unknown[] }) => Promise<unknown>,
): Signer {
  return {
    address,
    signTypedData: async (typedData: TypedData) =>
      (await request({
        method: "eth_signTypedData_v4",
        params: [address, JSON.stringify(typedData)],
      })) as Hex,
  };
}

const local: Token = { symbol: "USDC", address: tokenOf(1, "USDC").address, decimals: 6 };
const bruno = new Bruno({ chain: 31_337, url: "http://127.0.0.1:8000", tokens: [local] });
const max: Amount = { decimal: 2.5 };
const pay: readonly PayWith[] = [
  { token: "USDC", max, tip: { baseUnits: 1_000n } },
  { token: "ETH", max: { decimal: "0.001" } },
];

export async function paying(signer: Signer, to: Hex, data: Hex): Promise<string | null> {
  const price: X402Price = await bruno.x402.price({ call: { to, data }, gas: 100_000n });
  const offered: readonly Value[] = price.offers.map((o) => o.price);
  try {
    const paid: Paid = await bruno.x402.payAndSend({ call: { to, data }, gas: 100_000 }, signer, {
      pay,
    });
    return `${paid.transaction}: ${paid.paid.decimal ?? paid.paid.baseUnits} of ${offered.length}`;
  } catch (e) {
    if (e instanceof NoPayment) return e.reasons.join("; ");
    if (e instanceof BrunoError) return `${e.status}`;
    throw e;
  }
}

export async function selfPaying(call: { to: Hex; data: Hex }): Promise<Status> {
  const quote: Quote = await bruno.quote(60_000n, 100, ["USDC", "ETH"]);
  const [price] = quote.prices;
  if (price === undefined) throw new Error("no price");
  const id: Hex = await bruno.submit({
    call,
    gas: 60_000n,
    token: "USDC",
    amount: { baseUnits: price.baseUnits },
    maxBlockNumber: quote.validUntilBlock,
  });
  return bruno.wait(id);
}

export const fromViem: (account: Parameters<typeof viemSigner>[0]) => Signer = viemSigner;
