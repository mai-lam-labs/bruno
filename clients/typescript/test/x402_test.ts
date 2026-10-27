/**
 * The client's payments against the relay's own code: the Worker's PAYMENT-SIGNATURE parser
 * and EIP-3009 recovery must read what the client sends as the signer's payment, and the typed
 * data, hashed by EIP-712's rules here, must be the digest the token checks.
 */

import { assert, assertEquals, assertRejects } from "@std/assert";
import { calldata, digest, signerOf } from "../../../worker/src/eip3009.ts";
import { bytes, checksum, hex } from "../../../worker/src/evm.ts";
import { payloadOf } from "../../../worker/src/x402.ts";
import { Bruno } from "../src/client.ts";
import { transferCalldata } from "../src/eip3009.ts";
import type { Hex } from "../src/types.ts";
import { transferFor, typedData, usableRequirements } from "../src/x402.ts";
import { NoPayment, PaymentRefused } from "../src/x402_client.ts";
import {
  asking,
  CALL,
  fakeRelay,
  PAY,
  PAYEE,
  paying,
  relayAt,
  REQUIRED,
  REQUIREMENT,
  settled,
  TRANSACTION,
  USDC,
} from "./fake_relay.ts";
import { counting, digestOf, PAYER, signer } from "./payer.ts";

Deno.test("typed data: EIP-712's digest of it is the one the token checks", async () => {
  const transfer = transferFor(REQUIREMENT, PAYER, 86_991_964n, 1_792_764_000);
  const td = typedData(REQUIREMENT, transfer);
  const expected = digest(
    { name: "USDC", version: "2" },
    11_155_111,
    checksum(USDC),
    checksum(PAYER),
    checksum(PAYEE),
    86_991_964n,
    0n,
    transfer.validBefore,
    bytes(transfer.nonce),
  );
  assertEquals(hex(digestOf(td)), hex(expected));
  assertEquals(transfer.validBefore, 1_792_764_120n);
  assert((await signer.signTypedData(td)).length === 132);
});

Deno.test("PAYMENT-SIGNATURE: the relay reads it as the signer's payment, a tip included", async () => {
  const { relay, sent } = fakeRelay((request) =>
    paying(request)
      ? settled({ payer: PAYER }, { Location: "/sepolia/v1/submissions/0x01" })
      : asking(REQUIRED)
  );
  const bruno = new Bruno({ chain: "sepolia", fetch: relay });
  const call = { to: PAYEE, data: "0x" as Hex };
  const paid = await bruno.x402.payAndSend({ call, gas: 100_000n }, signer, {
    pay: [
      { token: "WETH", max: { decimal: 1 } },
      { token: "usdc", max: { decimal: "87" }, tip: { baseUnits: 8_036n } },
    ],
  });
  assertEquals(paid.transaction, TRANSACTION);
  assertEquals(paid.paid.baseUnits, 87_000_000n);
  assertEquals(paid.paid.decimal, "87");
  assertEquals(paid.paid.token.symbol, "USDC");
  assertEquals(paid.statusUrl, "https://bruno.mailam.app/sepolia/v1/submissions/0x01");
  assertEquals(JSON.parse(sent[0]!.body), {
    call,
    gas: "0x186a0",
    authorizationList: [],
    payer: PAYER,
  });

  const header = sent[1]!.headers.get("PAYMENT-SIGNATURE") as string;
  const payment = payloadOf(header);
  const binary = Uint8Array.from(atob(header), (c) => c.charCodeAt(0));
  assertEquals(JSON.parse(new TextDecoder().decode(binary)).resource, REQUIRED.resource);
  assertEquals(payment.accepted.amount, "87000000");
  assertEquals(payment.authorization.value, 87_000_000n);
  assertEquals(payment.authorization.from.toLowerCase(), PAYER);
  const recovered = signerOf({ name: "USDC", version: "2" }, 11_155_111, checksum(USDC), {
    payer: payment.authorization.from,
    to: payment.authorization.to,
    value: payment.authorization.value,
    validAfter: payment.authorization.validAfter,
    validBefore: payment.authorization.validBefore,
    nonce: payment.authorization.nonce,
    signature: payment.signature,
  });
  assertEquals(recovered?.toLowerCase(), PAYER);
});

Deno.test("no token within its max is refused, unsigned, each for its reason; a refusal says why", async () => {
  const noted = { signed: false };
  const bruno = new Bruno({ chain: "sepolia", fetch: fakeRelay(() => asking(REQUIRED)).relay });
  const request = { call: { to: PAYEE, data: "0x" as Hex }, gas: 50_000 };
  const none = await assertRejects(
    () =>
      bruno.x402.payAndSend(request, counting(signer, noted), {
        pay: [
          { token: "ETH", max: { decimal: "0.1" } },
          { token: "USDC", max: { decimal: 86.99 }, tip: { decimal: "0.001" } },
        ],
      }),
    NoPayment,
  );
  assertEquals(none.reasons, ["ETH: not offered", "USDC: 86.992964 is over its max, 86.99"]);
  assertEquals(noted.signed, false);

  const { relay: refusing } = fakeRelay((request) =>
    paying(request)
      ? asking(REQUIRED, {
        success: false,
        transaction: "",
        network: REQUIREMENT.network,
        errorReason: "insufficient_funds",
      })
      : asking(REQUIRED)
  );
  const refused = await assertRejects(
    () =>
      new Bruno({ chain: "sepolia", fetch: refusing }).x402.payAndSend(request, signer, {
        pay: [{ token: "USDC", max: { decimal: 100 } }],
      }),
    PaymentRefused,
  );
  assertEquals(refused.reason, "insufficient_funds");
});

Deno.test("a requirement on another chain isn't offered, nor signed for", async () => {
  const noted = { signed: false };
  // Sepolia's relay asking to be paid on mainnet, at the same address
  const elsewhere = { ...REQUIRED, accepts: [{ ...REQUIREMENT, network: "eip155:1" }] };
  const bruno = new Bruno({ chain: "sepolia", fetch: fakeRelay(() => asking(elsewhere)).relay });
  const request = { call: { to: PAYEE, data: "0x" as Hex }, gas: 50_000 };
  assertEquals((await bruno.x402.price(request)).offers, []);
  const none = await assertRejects(
    () =>
      bruno.x402.payAndSend(request, counting(signer, noted), {
        pay: [{ token: USDC, max: { baseUnits: 10n ** 12n } }],
      }),
    NoPayment,
  );
  assertEquals(none.reasons, [`${USDC}: not offered`]);
  assertEquals(noted.signed, false);
});

Deno.test("requirements: exact, EIP-3009, settled with the work; none other taken", () => {
  assertEquals(usableRequirements(REQUIRED), [REQUIREMENT]);
  const permit2 = {
    ...REQUIREMENT,
    extra: { ...REQUIREMENT.extra, assetTransferMethod: "permit2" },
  };
  const upfront = { ...REQUIREMENT, extra: { ...REQUIREMENT.extra, paymentFlow: "upfront" } };
  assertEquals(usableRequirements({ ...REQUIRED, accepts: [permit2, upfront] }), []);
});

Deno.test("a payment refused as short of its own price is paid again, at it, once", async () => {
  const { relay, sent } = relayAt(["1000"], "1500");
  const paid = await new Bruno({ chain: "sepolia", fetch: relay }).x402.payAndSend(CALL, signer, {
    pay: PAY,
  });
  assertEquals(paid.paid.baseUnits, 1500n);
  assertEquals(sent.length, 3);
});

Deno.test("transferCalldata: the token call the relay's own port makes, for a key and a contract", async () => {
  const transfer = transferFor(REQUIREMENT, PAYER, 1_234n, 1_800_000_000, `0x${"5a".repeat(32)}`);
  const contract: Hex = `0x${"c0".repeat(224)}`;
  for (
    const signature of [await signer.signTypedData(typedData(REQUIREMENT, transfer)), contract]
  ) {
    const expected = calldata({
      payer: checksum(transfer.from),
      to: checksum(transfer.to),
      value: transfer.value,
      validAfter: transfer.validAfter,
      validBefore: transfer.validBefore,
      nonce: bytes(transfer.nonce),
      signature: bytes(signature),
    });
    assertEquals(transferCalldata(transfer, signature), hex(expected));
  }
});
