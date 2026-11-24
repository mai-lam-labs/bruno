/**
 * A sponsorship (`sponsor`): quoted, checked to be what was asked, signed by a wallet that
 * doesn't send, and submitted; what's submitted read back as the relay reads it (the Worker's
 * own decoding).
 */

import { assertEquals, assertRejects } from "@std/assert";
import { keccak_256 } from "@noble/hashes/sha3.js";
import { bytes, checksum, hex as hexOf } from "../../../worker/src/evm.ts";
import { decoded, sign, signer as keySigner } from "../../../worker/src/transactions.ts";
import { Bruno } from "../src/client.ts";
import { BrunoError } from "../src/relay.ts";
import type { TransactionSigner, TransactionToSign } from "../src/sponsored.ts";
import type { Hex } from "../src/types.ts";
import { NoPayment } from "../src/x402_client.ts";
import { fakeRelay, PAYEE, type Sent, USDC } from "./fake_relay.ts";

const KEY = keySigner(keccak_256(new TextEncoder().encode("the client tests' sponsored account")));
const FORWARDER: Hex = "0x81638Ca590C1082bB348c2f4803977E51ce16e33";
const TARGET: Hex = "0x00000000000000000000000000000000000000a1";
const CALL = { to: TARGET, data: "0xabcdef" as Hex };
// a token the account didn't ask to pay in: Sepolia's WETH
const OTHER_TOKEN: Hex = "0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14";
const AMOUNT = 2_500_000n;
const SEPOLIA = 11_155_111;

/** The account's wallet: it signs a transaction and sends nothing, counting what it signs. */
function wallet(): { signer: TransactionSigner; signed: TransactionToSign[] } {
  const signed: TransactionToSign[] = [];
  const signer: TransactionSigner = {
    address: KEY.address,
    signTransaction(t) {
      signed.push(t);
      const fees = { maxFeePerGas: t.maxFeePerGas, maxPriorityFeePerGas: t.maxPriorityFeePerGas };
      const to = t.to === undefined ? null : checksum(t.to);
      const tx = sign(KEY, t.chainId, t.nonce, fees, Number(t.gas), to, bytes(t.data), t.value);
      return Promise.resolve(hexOf(tx.raw));
    },
  };
  return { signer, signed };
}

function word(value: bigint | string): string {
  const digits = typeof value === "bigint" ? value.toString(16) : value.slice(2);
  return digits.toLowerCase().padStart(64, "0");
}

interface Quoting {
  /** What the relay does to the transaction it fills in for the client's call. */
  readonly filled?: (transaction: Record<string, string>) => Record<string, string>;
  /** Whom its payment pays, and how much. */
  readonly pays?: Hex;
  readonly paying?: bigint;
  /** The token it quotes in, and pays in, whatever was asked. */
  readonly token?: Hex;
  /** The deadline it answers, whatever was asked. */
  readonly deadline?: string;
  /** A quote in a second token, unasked. */
  readonly another?: boolean;
}

/** A relay quoting a sponsorship of the calls it's asked about at AMOUNT of USDC, and taking
 * what's submitted; what it was sent. */
function sponsoring(quoting: Quoting = {}): { relay: typeof fetch; sent: Sent[] } {
  const fill = quoting.filled ?? ((t) => t);
  return fakeRelay(({ url, body }) => {
    if (url.endsWith("/v1/quote")) {
      const asked = JSON.parse(body).sponsored.transactions as Record<string, string>[];
      const fees = { maxFeePerGas: "0x77359400", maxPriorityFeePerGas: "0x3b9aca00" };
      const at = (nonce: number) => ({ type: "0x2", chainId: "0xaa36a7", nonce: `0x${nonce}` });
      const paid = `0xa9059cbb${word(quoting.pays ?? FORWARDER)}${word(quoting.paying ?? AMOUNT)}`;
      const token = quoting.token ?? USDC;
      const quote = {
        token,
        amount: `0x${AMOUNT.toString(16)}`,
        payment: {
          ...at(asked.length),
          to: token,
          data: paid,
          value: "0x0",
          gas: "0x11170",
          ...fees,
        },
      };
      return Response.json({
        validUntilBlock: "0x64",
        maxBlockNumber: quoting.deadline ?? "0x6e",
        forwarder: FORWARDER,
        sponsored: {
          transactions: asked.map((t, i) =>
            fill({
              ...at(i),
              to: t.to as string,
              data: t.data as string,
              value: t.value as string,
              gas: "0xea60",
              ...fees,
            })
          ),
        },
        quotes: quoting.another ? [quote, { ...quote, token: OTHER_TOKEN }] : [quote],
      });
    }
    return Response.json({ id: `0x${"cd".repeat(32)}` }, { status: 202 });
  });
}

Deno.test("a sponsorship: the account's call and its payment, signed as quoted", async () => {
  const { relay, sent } = sponsoring();
  const bruno = new Bruno({ chain: "sepolia", fetch: relay });
  const { signer, signed } = wallet();

  const { id, paid } = await bruno.sponsor([CALL], signer, {
    token: "USDC",
    max: { decimal: "3" },
    maxBlockNumber: 110n,
  });

  assertEquals(id, `0x${"cd".repeat(32)}`);
  assertEquals(paid.baseUnits, AMOUNT);
  const quoted = JSON.parse(sent[0]!.body);
  assertEquals(quoted.maxBlockNumber, "0x6e");
  assertEquals(quoted.sponsored.transactions, [
    { from: KEY.address, to: TARGET, data: "0xabcdef", value: "0x0" },
  ]);
  const submitted = JSON.parse(sent[1]!.body);
  assertEquals(submitted.maxBlockNumber, "0x6e");
  assertEquals(submitted.payment, { token: USDC, payee: FORWARDER, amount: "0x2625a0" });
  // as the relay reads them: the account's, in order, the call and then the transfer of the price
  const [acting, paying] = (submitted.sponsored.transactions as Hex[]).map((raw) =>
    decoded(bytes(raw))
  );
  assertEquals([acting!.signed.sender, paying!.signed.sender], [KEY.address, KEY.address]);
  assertEquals([acting!.signed.nonce, paying!.signed.nonce], [0, 1]);
  assertEquals([acting!.to, hexOf(acting!.data)], [checksum(TARGET), "0xabcdef"]);
  assertEquals(paying!.to, checksum(USDC));
  assertEquals(hexOf(paying!.data), `0xa9059cbb${word(FORWARDER)}${word(AMOUNT)}`);
  assertEquals(acting!.chainId, BigInt(SEPOLIA));
  assertEquals(signed.length, 2);
});

Deno.test("a quote that isn't what was asked is refused, nothing signed or submitted", async () => {
  const changed: Quoting[] = [
    { filled: (t) => ({ ...t, to: PAYEE }) }, // another target
    { filled: (t) => ({ ...t, data: "0xabcdee" }) }, // other data
    { filled: (t) => ({ ...t, value: "0x1" }) }, // value the call didn't send
    { filled: (t) => ({ ...t, chainId: "0x1" }) }, // another chain
    { filled: (t) => ({ ...t, nonce: "0x5" }) }, // a nonce its payment doesn't follow
    { pays: PAYEE }, // a payment to someone other than the quote's forwarder
    { paying: AMOUNT + 1n }, // a payment of more than the price
    { token: OTHER_TOKEN }, // priced and paid in a token that wasn't asked for
    { another: true }, // a quote more than was asked for
    { deadline: "0x6f" }, // a deadline past the one asked for
  ];
  for (const [index, quoting] of changed.entries()) {
    const { relay, sent } = sponsoring(quoting);
    const bruno = new Bruno({ chain: "sepolia", fetch: relay });
    const { signer, signed } = wallet();

    const refused = await assertRejects(
      () =>
        bruno.sponsor([CALL], signer, {
          token: "USDC",
          max: { decimal: "3" },
          maxBlockNumber: 110n,
        }),
      BrunoError,
    );

    assertEquals(refused.status, 0, `case ${index}`);
    assertEquals(signed, []);
    assertEquals(sent.length, 1);
  }
});

Deno.test("a price over the most the account will pay is refused, nothing signed", async () => {
  const { relay, sent } = sponsoring();
  const bruno = new Bruno({ chain: "sepolia", fetch: relay });
  const { signer, signed } = wallet();

  await assertRejects(
    () => bruno.sponsor([CALL], signer, { token: "USDC", max: { decimal: "2.4" } }),
    NoPayment,
  );

  assertEquals(signed, []);
  assertEquals(sent.length, 1);
});
