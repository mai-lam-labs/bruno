/**
 * The client's payments against the relay's own code: the Worker's PAYMENT-SIGNATURE parser
 * and EIP-3009 recovery must read what the client sends as the signer's payment, and the typed
 * data, hashed by EIP-712's rules here, must be the digest the token checks.
 */

import { assert, assertEquals, assertRejects, assertThrows } from "@std/assert";
import { secp256k1 } from "@noble/curves/secp256k1.js";
import { keccak_256 } from "@noble/hashes/sha3.js";
import { digest, signerOf } from "../../../worker/src/eip3009.ts";
import { checksum } from "../../../worker/src/evm.ts";
import { payloadOf } from "../../../worker/src/x402.ts";
import { Bruno, PaymentRefused } from "../src/client.ts";
import type { Hex, Signer, TypedData } from "../src/types.ts";
import {
  chooseRequirement,
  type PaymentRequired,
  type Requirement,
  transferFor,
  typedData,
} from "../src/x402.ts";

const KEY = keccak_256(new TextEncoder().encode("the client tests' payer"));
const USDC: Hex = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48";
const PAYEE: Hex = "0xfBC0cF0d2C09C57222C871B1cbF9b153bfFA0049";

function hexOf(bytes: Uint8Array): Hex {
  return `0x${[...bytes].map((b) => b.toString(16).padStart(2, "0")).join("")}`;
}

function bytesOf(hex: string): Uint8Array {
  return Uint8Array.from(hex.slice(2).match(/../g) ?? [], (b) => parseInt(b, 16));
}

function word(n: bigint): Uint8Array {
  return bytesOf(`0x${n.toString(16).padStart(64, "0")}`);
}

function concat(...parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of parts) {
    out.set(p, at);
    at += p.length;
  }
  return out;
}

const PAYER: Hex = (() => {
  const key = secp256k1.getPublicKey(KEY, false).slice(1);
  return hexOf(keccak_256(key).slice(12));
})();

/** EIP-712's hashStruct for the flat structs EIP-3009's typed data has: string, uint256,
 * address and bytes32 fields. */
function hashStruct(
  types: TypedData["types"],
  name: string,
  values: Readonly<Record<string, string | number>>,
): Uint8Array {
  const fields = types[name] ?? [];
  const typeString = `${name}(${fields.map((f) => `${f.type} ${f.name}`).join(",")})`;
  const encoded = fields.map(({ name: field, type }) => {
    const value = values[field];
    if (value === undefined) throw new Error(`no ${field}`);
    if (type === "string") return keccak_256(new TextEncoder().encode(String(value)));
    if (type === "bytes32") return bytesOf(String(value));
    return word(BigInt(value));
  });
  return keccak_256(concat(keccak_256(new TextEncoder().encode(typeString)), ...encoded));
}

/** A key that signs typed data by EIP-712's rules, as a wallet does. */
const signer: Signer = {
  address: PAYER,
  signTypedData(td: TypedData): Promise<Hex> {
    const hash = keccak_256(concat(
      Uint8Array.of(0x19, 0x01),
      hashStruct(td.types, "EIP712Domain", td.domain),
      hashStruct(td.types, td.primaryType, td.message),
    ));
    const signature = secp256k1.sign(hash, KEY, { prehash: false, format: "recovered" });
    // r ‖ s ‖ v, v 27 or 28
    return Promise.resolve(
      hexOf(concat(signature.slice(1), Uint8Array.of((signature[0] as number) + 27))),
    );
  },
};

const REQUIREMENT: Requirement = {
  scheme: "exact",
  network: "eip155:11155111",
  amount: "86991964",
  asset: USDC,
  payTo: PAYEE,
  maxTimeoutSeconds: 120,
  extra: { name: "USDC", version: "2" },
};
const REQUIRED: PaymentRequired = {
  x402Version: 2,
  resource: { url: "https://bruno.mailam.app/sepolia/v1/x402" },
  accepts: [REQUIREMENT],
};

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
    bytesOf(transfer.nonce),
  );
  const hash = keccak_256(concat(
    Uint8Array.of(0x19, 0x01),
    hashStruct(td.types, "EIP712Domain", td.domain),
    hashStruct(td.types, td.primaryType, td.message),
  ));
  assertEquals(hexOf(hash), hexOf(expected));
  assertEquals(transfer.validBefore, 1_792_764_120n);
  assert((await signer.signTypedData(td)).length === 132);
});

Deno.test("PAYMENT-SIGNATURE: the relay reads it as the signer's payment, a tip included", async () => {
  const sent: { headers: Headers; body: string }[] = [];
  const relay: typeof fetch = (_input, init) => {
    const headers = new Headers(init?.headers);
    sent.push({ headers, body: String(init?.body) });
    if (!headers.has("PAYMENT-SIGNATURE")) {
      return Promise.resolve(
        new Response("{}", {
          status: 402,
          headers: { "PAYMENT-REQUIRED": btoa(JSON.stringify(REQUIRED)) },
        }),
      );
    }
    const settlement = {
      success: true,
      transaction: `0x${"ab".repeat(32)}`,
      network: REQUIREMENT.network,
      payer: PAYER,
    };
    return Promise.resolve(
      new Response("{}", {
        status: 200,
        headers: {
          "PAYMENT-RESPONSE": btoa(JSON.stringify(settlement)),
          Location: "/sepolia/v1/submissions/0x01",
        },
      }),
    );
  };
  const bruno = new Bruno({ chain: "sepolia", fetch: relay });
  const call = { to: PAYEE, data: "0x" as Hex };
  const paid = await bruno.payAndSend({ call, gas: 100_000n }, signer, { tip: 8_036n });
  assertEquals(paid.transaction, `0x${"ab".repeat(32)}`);
  assertEquals(paid.amount, 87_000_000n);
  assertEquals(paid.statusUrl, "https://bruno.mailam.app/sepolia/v1/submissions/0x01");
  assertEquals(JSON.parse(sent[0]!.body), { call, gas: "0x186a0", authorizationList: [] });

  const payment = payloadOf(sent[1]!.headers.get("PAYMENT-SIGNATURE") as string);
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

Deno.test("a price over maxAmount is refused, unsigned; a refusal says why", async () => {
  let signed = false;
  const counting: Signer = {
    ...signer,
    signTypedData: (td) => ((signed = true), signer.signTypedData(td)),
  };
  const asking: typeof fetch = () =>
    Promise.resolve(
      new Response("{}", {
        status: 402,
        headers: { "PAYMENT-REQUIRED": btoa(JSON.stringify(REQUIRED)) },
      }),
    );
  const bruno = new Bruno({ chain: "sepolia", fetch: asking });
  const request = { call: { to: PAYEE, data: "0x" as Hex }, gas: 50_000 };
  await assertRejects(() => bruno.payAndSend(request, counting, { maxAmount: 1_000n }));
  assertEquals(signed, false);

  const refusing: typeof fetch = (_input, init) => {
    const paying = new Headers(init?.headers).has("PAYMENT-SIGNATURE");
    const headers: Record<string, string> = { "PAYMENT-REQUIRED": btoa(JSON.stringify(REQUIRED)) };
    if (paying) {
      headers["PAYMENT-RESPONSE"] = btoa(JSON.stringify({
        success: false,
        transaction: "",
        network: REQUIREMENT.network,
        errorReason: "insufficient_funds",
      }));
    }
    return Promise.resolve(new Response("{}", { status: 402, headers }));
  };
  const refused = await assertRejects(
    () => new Bruno({ chain: "sepolia", fetch: refusing }).payAndSend(request, signer),
    PaymentRefused,
  );
  assertEquals(refused.reason, "insufficient_funds");
});

Deno.test("requirements: exact, EIP-3009, settled with the work; none other taken", () => {
  assertEquals(chooseRequirement(REQUIRED), REQUIREMENT);
  const permit2 = {
    ...REQUIREMENT,
    extra: { ...REQUIREMENT.extra, assetTransferMethod: "permit2" },
  };
  const upfront = { ...REQUIREMENT, extra: { ...REQUIREMENT.extra, paymentFlow: "upfront" } };
  assertThrows(() => chooseRequirement({ ...REQUIRED, accepts: [permit2, upfront] }));
  assertThrows(() => chooseRequirement(REQUIRED, PAYEE));
});
