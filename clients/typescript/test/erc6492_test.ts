/**
 * A smart account not yet deployed, paying over x402: its deployment priced with its first
 * payment, whether it says how it's deployed or only its wrapped signature does (ERC-6492).
 */

import { assertEquals } from "@std/assert";
import { payloadOf } from "../../../worker/src/x402.ts";
import { Bruno } from "../src/client.ts";
import type { Signer } from "../src/types.ts";
import { CALL, PAY, relayAt } from "./fake_relay.ts";
import { FACTORY, FACTORY_DATA, PAYER, signer, wrappedSignature } from "./payer.ts";

Deno.test("a smart account declares its payer and its deployment while it's undeployed", async () => {
  const { relay, sent } = relayAt(["1000"], "1000");
  const smart: Signer = {
    ...signer,
    getFactoryArgs: () => Promise.resolve({ factory: FACTORY, factoryData: FACTORY_DATA }),
  };
  await new Bruno({ chain: "sepolia", fetch: relay }).x402.payAndSend(CALL, smart, { pay: PAY });
  const asked = JSON.parse(sent[0]!.body);
  assertEquals(asked.payer, PAYER);
  assertEquals(asked.deployment, { account: PAYER, factory: FACTORY, factoryData: FACTORY_DATA });
  assertEquals(sent.length, 2);
});

Deno.test("a wrapped signature not declared is priced with its deployment, and signed again", async () => {
  const { relay, sent } = relayAt(["1000", "3000"], "3000");
  const wrapping: Signer = {
    address: PAYER,
    signTypedData: async (td) => wrappedSignature(await signer.signTypedData(td)),
  };
  const paid = await new Bruno({ chain: "sepolia", fetch: relay }).x402.payAndSend(
    CALL,
    wrapping,
    { pay: PAY },
  );
  assertEquals(JSON.parse(sent[0]!.body).deployment, undefined);
  // read from the wrapper as it is: the relay takes an address in any case
  assertEquals(JSON.parse(sent[1]!.body).deployment, {
    account: PAYER,
    factory: FACTORY.toLowerCase(),
    factoryData: FACTORY_DATA,
  });
  assertEquals(
    payloadOf(sent[2]!.headers.get("PAYMENT-SIGNATURE") as string).accepted.amount,
    "3000",
  );
  assertEquals(paid.paid.baseUnits, 3000n);
});
