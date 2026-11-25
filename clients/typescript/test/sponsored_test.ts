/**
 * A sponsorship (`sponsor`): quoted, checked to be what was asked, signed by a wallet that
 * doesn't send, and submitted; what's asked and what's submitted read back as the relay reads
 * them (the Worker's own decoding, which refuses a field it doesn't know).
 */

import { assertEquals, assertRejects, assertThrows } from "@std/assert";
import { keccak_256 } from "@noble/hashes/sha3.js";
import { bytes, checksum, hex as hexOf } from "../../../worker/src/evm.ts";
import { isSigned } from "../../../worker/src/sponsoring.ts";
import { sign, signer as keySigner } from "../../../worker/src/transactions.ts";
import { askedOf, sponsorship } from "../../../worker/src/wire.ts";
import { Bruno } from "../src/client.ts";
import { BrunoError } from "../src/relay.ts";
import {
  checkedSponsored,
  type SponsorshipQuote,
  toSignOf,
  type TransactionSigner,
  type TransactionToSign,
} from "../src/sponsored.ts";
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
  /** The gas its first transaction is given for the account's code: the account has some. */
  readonly gas?: string;
}

/** A relay quoting a sponsorship of the calls it's asked about at AMOUNT of USDC, and taking
 * what's submitted; what it was sent. */
function sponsoring(quoting: Quoting = {}): { relay: typeof fetch; sent: Sent[] } {
  const fill = quoting.filled ?? ((t) => t);
  return fakeRelay(({ url, body }) => {
    if (url.endsWith("/v1/sponsorships/quote")) {
      const asked = JSON.parse(body).transactions as Record<string, string>[];
      const fees = { maxFeePerGas: "0x77359400", maxPriorityFeePerGas: "0x3b9aca00" };
      const at = (nonce: number) => ({ type: "0x2", chainId: "0xaa36a7", nonce: `0x${nonce}` });
      const paid = `0xa9059cbb${word(quoting.pays ?? FORWARDER)}${word(quoting.paying ?? AMOUNT)}`;
      const token = quoting.token ?? USDC;
      const payment = {
        ...at(asked.length),
        to: token,
        data: paid,
        value: "0x0",
        gas: "0x11170",
        ...fees,
      };
      // no payment to sign with no transaction of the account's: its code pays
      const quote = {
        token,
        amount: `0x${AMOUNT.toString(16)}`,
        ...(asked.length === 0 ? {} : { payment }),
      };
      return Response.json({
        validUntilBlock: "0x64",
        maxBlockNumber: quoting.deadline ?? "0x6e",
        forwarder: FORWARDER,
        ...(quoting.gas === undefined ? {} : { gas: quoting.gas }),
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
  assertEquals(sent.map((s) => new URL(s.url).pathname), [
    "/sepolia/v1/sponsorships/quote",
    "/sepolia/v1/sponsorships",
  ]);
  // what was asked, as the relay reads it: the account, and its call to sign
  const asked = askedOf(JSON.parse(sent[0]!.body));
  assertEquals([asked.account, asked.maxBlockNumber], [KEY.address, 110]);
  const [unsigned] = asked.transactions;
  assertEquals(unsigned !== undefined && !isSigned(unsigned) && unsigned.to, checksum(TARGET));
  // and what was submitted: the account's, in order, the call and then the transfer of the price
  const taken = sponsorship(JSON.parse(sent[1]!.body));
  assertEquals([taken.account, taken.maxBlockNumber], [KEY.address, 110]);
  assertEquals([taken.token, taken.payee, taken.amount], [checksum(USDC), FORWARDER, AMOUNT]);
  const [acting, paying] = taken.transactions;
  assertEquals([acting!.signed.sender, paying!.signed.sender], [KEY.address, KEY.address]);
  assertEquals([acting!.signed.nonce, paying!.signed.nonce], [0, 1]);
  assertEquals([acting!.to, hexOf(acting!.data)], [checksum(TARGET), "0xabcdef"]);
  assertEquals(paying!.to, checksum(USDC));
  assertEquals(hexOf(paying!.data), `0xa9059cbb${word(FORWARDER)}${word(AMOUNT)}`);
  assertEquals(acting!.chainId, BigInt(SEPOLIA));
  assertEquals(signed.length, 2);
});

Deno.test("an account with code: the gas the quote gives for it is submitted", async () => {
  const { relay, sent } = sponsoring({ gas: "0x1d4c" });
  const bruno = new Bruno({ chain: "sepolia", fetch: relay });

  await bruno.sponsor([CALL], wallet().signer, { token: "USDC", max: { decimal: "3" } });

  assertEquals(sponsorship(JSON.parse(sent[1]!.body)).gas, 0x1d4c);
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

Deno.test("what's signed pays the token named, however the quote was come by", async () => {
  // a quote whose only price, and payment, is in another token: the check is told which token
  // the account pays in, and signs a payment in no other
  const { relay } = sponsoring({ token: OTHER_TOKEN });
  const answered = await relay("https://relay.example/sepolia/v1/sponsorships/quote", {
    method: "POST",
    body: JSON.stringify({ transactions: [{ ...CALL, value: "0x0" }] }),
  });
  const raw = await answered.json();
  const quote: SponsorshipQuote = {
    validUntilBlock: BigInt(raw.validUntilBlock),
    maxBlockNumber: BigInt(raw.maxBlockNumber),
    forwarder: raw.forwarder,
    gas: null,
    transactions: raw.transactions.map(toSignOf),
    prices: [{
      token: { address: OTHER_TOKEN, symbol: "WETH", decimals: 18 },
      baseUnits: AMOUNT,
      decimal: null,
    }],
    payments: [toSignOf(raw.quotes[0].payment)],
  };

  assertEquals(checkedSponsored(quote, [CALL], SEPOLIA, OTHER_TOKEN).length, 2);
  assertThrows(() => checkedSponsored(quote, [CALL], SEPOLIA, USDC), BrunoError);
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

Deno.test("an account whose code pays: quoted with no payment to sign, submitted as signed", async () => {
  const { relay, sent } = sponsoring({ gas: "0x1d4c" });
  const bruno = new Bruno({ chain: "sepolia", fetch: relay });
  // the account's delegation, signed, and the call into it that pays
  const authorization = {
    chainId: "0xaa36a7" as Hex,
    address: TARGET,
    nonce: "0x0" as Hex,
    yParity: "0x1" as Hex,
    r: `0x${"11".repeat(32)}` as Hex,
    s: `0x${"22".repeat(32)}` as Hex,
  };
  const call = { to: KEY.address, data: "0xabcdef" as Hex };

  const quote = await bruno.quoteSponsorship(KEY.address, [], ["USDC"], {
    authorizationList: [authorization],
    call,
  });
  const id = await bruno.submitSponsorship({
    account: KEY.address,
    authorizationList: [authorization],
    call,
    gas: quote.gas ?? 0n,
    token: "USDC",
    amount: { baseUnits: quote.prices[0]!.baseUnits },
    forwarder: quote.forwarder,
    maxBlockNumber: quote.maxBlockNumber,
  });

  assertEquals(id, `0x${"cd".repeat(32)}`);
  assertEquals([quote.transactions, quote.payments, quote.gas], [[], [], 0x1d4cn]);
  // each a request the relay reads, no field it doesn't know
  const asked = JSON.parse(sent[0]!.body);
  assertEquals(asked.authorizationList, [authorization]);
  assertEquals([asked.account, asked.call, asked.transactions], [KEY.address, call, []]);
  const submitted = JSON.parse(sent[1]!.body);
  assertEquals(Object.keys(submitted).sort(), [
    "account",
    "authorizationList",
    "call",
    "gas",
    "maxBlockNumber",
    "payment",
    "transactions",
  ]);
  assertEquals([submitted.gas, submitted.maxBlockNumber], ["0x1d4c", "0x6e"]);
  assertEquals(submitted.payment, { token: USDC, payee: FORWARDER, amount: "0x2625a0" });
});
