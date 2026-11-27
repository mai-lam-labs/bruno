/**
 * The client as npm, pnpm and Bun install it from its packed tarball (`make npm-test`): a
 * stand-in relay, one call paid over x402 by a viem account, the payment's signature checked,
 * and a price over the most allowed refused with nothing signed. Run as it is by Node and Bun,
 * and type-checked by tsc against the client's generated declarations.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { Bruno, NoPayment, tokenOf, viemSigner } from "@mailam/bruno";
import { type Hex, keccak256, recoverTypedDataAddress, toHex } from "viem";
import { privateKeyToAccount } from "viem/accounts";
import { sepolia } from "viem/chains";

/** Whether `T` is `any`: what a declaration that failed to resolve becomes. */
type IsAny<T> = 0 extends 1 & T ? true : false;
const typed: [
  IsAny<Bruno>,
  IsAny<Bruno["x402"]>,
  IsAny<ReturnType<typeof tokenOf>>,
  IsAny<ReturnType<typeof viemSigner>>,
] = [false, false, false, false];
assert.deepEqual(typed, [false, false, false, false]);

const account = privateKeyToAccount(keccak256(toHex("the npm tests' wallet")));
const USDC = tokenOf(sepolia.id, "USDC").address;
const PAYEE: Hex = "0xfBC0cF0d2C09C57222C871B1cbF9b153bfFA0049";
const RECIPIENT: Hex = "0x000000000000000000000000000000000000bEEF";
const TRANSACTION: Hex = `0x${"cd".repeat(32)}`;
const REQUIREMENT = {
  scheme: "exact",
  network: `eip155:${sepolia.id}`,
  amount: "250000",
  asset: USDC,
  payTo: PAYEE,
  maxTimeoutSeconds: 120,
  extra: { name: "USDC", version: "2" },
};
const DOMAIN = { name: "USDC", version: "2", chainId: sepolia.id, verifyingContract: USDC };
const TYPES = {
  TransferWithAuthorization: [
    { name: "from", type: "address" },
    { name: "to", type: "address" },
    { name: "value", type: "uint256" },
    { name: "validAfter", type: "uint256" },
    { name: "validBefore", type: "uint256" },
    { name: "nonce", type: "bytes32" },
  ],
} as const;

interface Sent {
  readonly url: string;
  readonly body: unknown;
  readonly payment: string | null;
}

/** A relay: 402 until paid, then settled; every request kept. */
function relay(sent: Sent[]): typeof fetch {
  return (input, init) => {
    const payment = new Headers(init?.headers).get("PAYMENT-SIGNATURE");
    sent.push({ url: String(input), body: JSON.parse(String(init?.body)), payment });
    if (payment === null) {
      const required = btoa(JSON.stringify({ x402Version: 2, accepts: [REQUIREMENT] }));
      return Promise.resolve(
        new Response("{}", { status: 402, headers: { "PAYMENT-REQUIRED": required } }),
      );
    }
    const settled = { success: true, transaction: TRANSACTION, network: REQUIREMENT.network };
    return Promise.resolve(
      new Response("{}", {
        status: 200,
        headers: { "PAYMENT-RESPONSE": btoa(JSON.stringify(settled)) },
      }),
    );
  };
}

// the client's own dependencies: none
const client: { dependencies?: Record<string, string> } = JSON.parse(
  readFileSync("node_modules/@mailam/bruno/package.json", "utf8"),
);
assert.deepEqual(Object.keys(client.dependencies ?? {}), []);

// its errors are Errors, carrying their fields
const none = new NoPayment(["USDC: not offered"]);
assert.ok(none instanceof Error);
assert.deepEqual(none.reasons, ["USDC: not offered"]);
assert.equal(new Bruno({ chain: "sepolia" }).chainId, sepolia.id);

const sent: Sent[] = [];
const bruno = new Bruno({ chain: "sepolia", url: "https://relay.test", fetch: relay(sent) });
const calls = [{ to: RECIPIENT, data: "0x" as Hex, gas: 21_000 }];

// a call's price, asked without paying
const { offers } = await bruno.x402.price({ calls });
assert.deepEqual(offers.map((o) => o.price.decimal), ["0.25"]);

// the call paid: the price asked, a payment signed for it, and the call sent with it
const paid = await bruno.x402.payAndSend({ calls }, viemSigner(account), {
  pay: [{ token: "USDC", max: { decimal: "1" } }],
});
assert.equal(paid.transaction, TRANSACTION);
assert.equal(paid.paid.decimal, "0.25");
assert.ok(sent.every((s) => s.url.startsWith("https://relay.test/sepolia/v1/x402")));
const paying = sent.filter((s) => s.payment !== null);
assert.equal(paying.length, 1);
// the call as given, the payer named with it
const body = paying[0]!.body as { calls: unknown; payer: unknown };
assert.deepEqual(body.calls, [{ to: RECIPIENT, data: "0x", gas: "0x5208" }]);
assert.equal(body.payer, account.address);

// Bruno's payment: a transfer from the account to the payee, of the price, the account's own
// signature
const payment = JSON.parse(atob(paying[0]!.payment!));
const a = payment.payload.authorization;
assert.deepEqual([a.from, a.to, a.value], [account.address, PAYEE, "250000"]);
const payer = await recoverTypedDataAddress({
  domain: DOMAIN,
  types: TYPES,
  primaryType: "TransferWithAuthorization",
  message: {
    ...a,
    value: BigInt(a.value),
    validAfter: BigInt(a.validAfter),
    validBefore: BigInt(a.validBefore),
  },
  signature: payment.payload.signature,
});
assert.equal(payer, account.address);

// a price over the most allowed: refused, nothing signed for Bruno
const before = sent.length;
await assert.rejects(
  bruno.x402.payAndSend({ calls }, viemSigner(account), {
    pay: [{ token: "USDC", max: { decimal: "0.1" } }],
  }),
  NoPayment,
);
assert.equal(sent.slice(before).filter((s) => s.payment !== null).length, 0);

console.log(
  "ok: the packed client under",
  process.versions.bun ? `Bun ${process.versions.bun}` : `Node ${process.version}`,
);
