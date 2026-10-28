/**
 * A tracked submission as the Durable Object stores it and reads it back after an eviction:
 * everything its transaction is planned from survives, its payment's transfer and its
 * deployment among them.
 */

import { assertEquals } from "@std/assert";
import { checksum } from "../src/evm.ts";
import { type Tracked, trackedFromJson, trackedToJson } from "../src/tracked.ts";

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
    gasLimit: 400_000,
    size: 900,
    status: "pending",
    sent: [],
    attempts: 0,
    transactionHash: null,
    blockNumber: null,
    reason: null,
    revertData: null,
    finishedAt: null,
  };
  assertEquals(trackedFromJson(trackedToJson(tracked)), tracked);
});
