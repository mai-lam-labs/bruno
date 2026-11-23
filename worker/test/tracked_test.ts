/**
 * A tracked submission as the Durable Object stores it and reads it back after an eviction:
 * pending, everything its transaction is planned from survives, its payment's transfer, its
 * deployment and its proof of payment among them, of either kind; finished, only what its
 * status needs is stored.
 */

import { assertEquals } from "@std/assert";
import { checksum } from "../src/evm.ts";
import { ByEntryPoint, MeasuredByRunner } from "../src/plan.ts";
import { ENTRY_POINT_0_7 } from "../src/userops.ts";
import {
  finish,
  finishedToJson,
  storedFromJson,
  type Tracked,
  trackedToJson,
} from "../src/tracked.ts";

const ACCOUNT = checksum("0x00000000000000000000000000000000000000a1");
const FACTORY = checksum("0x0BA5ED0c6AA8c49038F819E587E2633c4A9F428a");
const TOKEN = checksum("0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238");
const PAYEE = checksum("0xfBC0cF0d2C09C57222C871B1cbF9b153bfFA0049");

Deno.test("a submission paid by a transfer, with a deployment, survives being stored", () => {
  const tracked: Tracked = {
    id: `0x${"11".repeat(32)}`,
    submission: {
      call: { to: ACCOUNT, data: Uint8Array.of(1, 2, 3), value: 0n },
      authorizations: [],
      gas: 100_000,
      token: TOKEN,
      payee: PAYEE,
      amount: 44_222_733n,
      maxBlockNumber: 12_000_000,
      transfer: {
        payer: ACCOUNT,
        to: PAYEE,
        value: 44_222_733n,
        validAfter: 0n,
        validBefore: 1_780_000_000n,
        nonce: new Uint8Array(32).fill(7),
        signature: new Uint8Array(224).fill(9),
      },
      deployment: { account: ACCOUNT, factory: FACTORY, factoryData: Uint8Array.of(0xab, 0xcd) },
    },
    basis: [{ gasLimit: 400_000, size: 900, value: 0n }],
    status: "pending",
    sent: [],
    attempts: 0,
    transactionHash: null,
    blockNumber: null,
    revertData: null,
    userOperation: `0x${"33".repeat(32)}`,
    // past 2^53 each, so stored as strings
    proof: new ByEntryPoint(ENTRY_POINT_0_7, {
      gas: 2n ** 60n,
      maxFeePerGas: 3n * 10n ** 18n,
      maxPriorityFeePerGas: 2n ** 64n,
    }),
  };
  assertEquals(storedFromJson(trackedToJson(tracked)), tracked);

  const finished = finish(
    { ...tracked, transactionHash: `0x${"22".repeat(32)}`, blockNumber: 7 },
    "included",
  );
  const stored = finishedToJson(finished);
  assertEquals(storedFromJson(stored), finished);
  assertEquals(stored.includes("submission"), false);
  assertEquals(stored.length < 400, true, `${stored.length} bytes stored`);

  const proof = new MeasuredByRunner();
  assertEquals(storedFromJson(trackedToJson({ ...tracked, proof })), { ...tracked, proof });
});
