/**
 * A submission paid by its payer's signed transfer (`payAndSubmit`): quoted for the payer, and
 * signed as the relay's own port recovers it.
 */

import { assertEquals } from "@std/assert";
import { signerOf } from "../../../worker/src/eip3009.ts";
import { bytes, checksum } from "../../../worker/src/evm.ts";
import { Bruno } from "../src/client.ts";
import { CALL, PAYEE, submitting, USDC } from "./fake_relay.ts";
import { PAYER, signer } from "./payer.ts";

Deno.test("a submission paid by a signed transfer: the relay recovers its payer", async () => {
  const { relay, sent } = submitting("0x3e8");
  const bruno = new Bruno({ chain: "sepolia", fetch: relay });
  const { id, paid } = await bruno.payAndSubmit(CALL, signer, {
    token: "USDC",
    max: { decimal: "1" },
    deadlineBlocks: 5,
  });
  assertEquals(id, `0x${"cd".repeat(32)}`);
  assertEquals(paid.baseUnits, 1000n);
  const quoted = new URL(sent.find((s) => s.url.includes("/v1/quote"))!.url);
  assertEquals(quoted.searchParams.get("payer"), PAYER);
  const submitted = JSON.parse(sent[sent.length - 1]!.body);
  assertEquals(submitted.maxBlockNumber, "0x69");
  const t = submitted.payment.transfer;
  const recovered = signerOf({ name: "USDC", version: "2" }, 11_155_111, checksum(USDC), {
    payer: checksum(t.from),
    to: checksum(PAYEE),
    value: 1000n,
    validAfter: BigInt(t.validAfter),
    validBefore: BigInt(t.validBefore),
    nonce: bytes(t.nonce),
    signature: bytes(t.signature),
  });
  assertEquals(recovered?.toLowerCase(), PAYER);
});
