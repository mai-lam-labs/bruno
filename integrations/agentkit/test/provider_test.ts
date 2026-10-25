/**
 * The provider against a stand-in relay and a wallet of a test key: what it sends is the
 * wallet's own USDC transfer, signed by it, and Bruno is paid by another, as x402 asks.
 */

import { assertEquals, assertStringIncludes } from "@std/assert";
import {
  decodeFunctionData,
  type Hex,
  keccak256,
  parseAbi,
  recoverTypedDataAddress,
  toHex,
} from "viem";
import { privateKeyToAccount } from "viem/accounts";
import type { EvmWalletProvider } from "@coinbase/agentkit";
import { BrunoActionProvider } from "../mod.ts";

const account = privateKeyToAccount(keccak256(toHex("the AgentKit tests' wallet")));
const USDC: Hex = "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238";
const PAYEE: Hex = "0xfBC0cF0d2C09C57222C871B1cbF9b153bfFA0049";
const RECIPIENT: Hex = "0x000000000000000000000000000000000000bEEF";
const REQUIREMENT = {
  scheme: "exact",
  network: "eip155:11155111",
  amount: "250000",
  asset: USDC,
  payTo: PAYEE,
  maxTimeoutSeconds: 120,
  extra: { name: "USDC", version: "2" },
};
const DOMAIN = { name: "USDC", version: "2", chainId: 11_155_111, verifyingContract: USDC };
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

/** What of a wallet provider the actions use: a Sepolia wallet of the test key. */
const wallet = {
  getName: () => "test",
  getNetwork: () => ({ protocolFamily: "evm", chainId: "11155111", networkId: "ethereum-sepolia" }),
  getAddress: () => account.address,
  toSigner: () => account,
  getPublicClient: () => ({ estimateGas: () => Promise.resolve(60_000n) }),
  readContract: () => Promise.resolve(6),
} as unknown as EvmWalletProvider;

/** A relay: 402 until paid, then settled; every request kept. */
function relay(sent: { body: unknown; payment: string | null }[]): typeof fetch {
  return (_input, init) => {
    const payment = new Headers(init?.headers).get("PAYMENT-SIGNATURE");
    sent.push({ body: JSON.parse(String(init?.body)), payment });
    if (payment === null) {
      const required = btoa(JSON.stringify({ x402Version: 2, accepts: [REQUIREMENT] }));
      return Promise.resolve(
        new Response("{}", { status: 402, headers: { "PAYMENT-REQUIRED": required } }),
      );
    }
    const settled = {
      success: true,
      transaction: `0x${"cd".repeat(32)}`,
      network: REQUIREMENT.network,
    };
    return Promise.resolve(
      new Response("{}", {
        status: 200,
        headers: { "PAYMENT-RESPONSE": btoa(JSON.stringify(settled)) },
      }),
    );
  };
}

Deno.test("send_usdc_gasless: the wallet's own signed transfer, and Bruno paid by another", async () => {
  const sent: { body: unknown; payment: string | null }[] = [];
  const provider = new BrunoActionProvider({ fetch: relay(sent) });
  const said = await provider.sendUsdcGasless(wallet, { recipient: RECIPIENT, amount: "2.5" });
  assertStringIncludes(said, `0x${"cd".repeat(32)}`);
  assertStringIncludes(said, "paying Bruno 0.25 USDC");

  // the paid request's call: USDC's transferWithAuthorization of 2.5 USDC to the recipient
  const paid = sent.find((s) => s.payment !== null)!;
  const { call } = paid.body as { call: { to: Hex; data: Hex } };
  assertEquals(call.to, USDC);
  const { args } = decodeFunctionData({
    abi: parseAbi([
      "function transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,uint8,bytes32,bytes32)",
    ]),
    data: call.data,
  });
  const [from, to, value, validAfter, validBefore, nonce, v, r, s] = args;
  assertEquals([from, to, value], [account.address, RECIPIENT, 2_500_000n]);
  const signer = await recoverTypedDataAddress({
    domain: DOMAIN,
    types: TYPES,
    primaryType: "TransferWithAuthorization",
    message: { from, to, value, validAfter, validBefore, nonce },
    signature: `${r}${s.slice(2)}${v.toString(16)}` as Hex,
  });
  assertEquals(signer, account.address);

  // Bruno's payment: another transfer, from the wallet to the payee, of the price
  const payment = JSON.parse(atob(paid.payment!));
  const a = payment.payload.authorization;
  assertEquals([a.from, a.to, a.value], [account.address, PAYEE, "250000"]);
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
  assertEquals(payer, account.address);
});

Deno.test("a fee over maxFee is refused before anything is paid", async () => {
  const sent: { body: unknown; payment: string | null }[] = [];
  const provider = new BrunoActionProvider({ fetch: relay(sent) });
  const said = await provider.sendUsdcGasless(wallet, {
    recipient: RECIPIENT,
    amount: "1",
    maxFee: "0.1",
  });
  assertStringIncludes(said, "failed");
  assertEquals(sent.filter((s) => s.payment !== null).length, 0);
});

Deno.test("Ethereum and Sepolia only", () => {
  const provider = new BrunoActionProvider();
  assertEquals(provider.supportsNetwork({ protocolFamily: "evm", chainId: "1" }), true);
  assertEquals(provider.supportsNetwork({ protocolFamily: "evm", chainId: "8453" }), false);
});

Deno.test("AgentKit's own registry: three actions, each given the wallet", async () => {
  // nothing but the relay is asked: AgentKit's telemetry (cca-lite.coinbase.com, the wallet's
  // address among it) is its decorator's, which the provider doesn't use
  const global = globalThis.fetch;
  globalThis.fetch = (input) => Promise.reject(new Error(`fetched ${String(input)}`));
  try {
    const sent: { body: unknown; payment: string | null }[] = [];
    const actions = new BrunoActionProvider({ fetch: relay(sent) }).getActions(wallet);
    assertEquals(actions.map((a) => a.name).sort(), [
      "BrunoActionProvider_get_gasless_price",
      "BrunoActionProvider_send_call_gasless",
      "BrunoActionProvider_send_usdc_gasless",
    ]);
    const price = actions.find((a) => a.name.endsWith("get_gasless_price"))!;
    const said = await price.invoke({ to: RECIPIENT, data: "0x" });
    assertStringIncludes(said, "costs 0.25 USDC");
    assertEquals(sent.length, 1);
  } finally {
    globalThis.fetch = global;
  }
});
