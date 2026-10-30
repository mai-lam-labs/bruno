/**
 * ZeroDev's Kernel v3.2 as AgentKit's ZeroDevWalletProvider.configureWithWallet makes it
 * (@zerodev/sdk, the same options): Kernel v3.2, ZeroDev's ECDSA validator its sudo, EntryPoint
 * v0.7, the intent executor installed. Used by agent.ts.
 */

import { type Chain, createPublicClient, http } from "viem";
import type { LocalAccount } from "viem/accounts";
import { createKernelAccount } from "@zerodev/sdk";
import { getEntryPoint, KERNEL_V3_2 } from "@zerodev/sdk/constants";
import { signerToEcdsaValidator } from "@zerodev/ecdsa-validator";
import { installIntentExecutor, INTENT_V0_3 } from "@zerodev/intent";

/** The Kernel account `owner` owns on `chain`, read through `rpc`. */
export async function kernelAccount(rpc: string, chain: Chain, owner: LocalAccount) {
  const client = createPublicClient({ chain, transport: http(rpc) });
  const entryPoint = getEntryPoint("0.7");
  const sudo = await signerToEcdsaValidator(client, {
    signer: owner,
    entryPoint,
    kernelVersion: KERNEL_V3_2,
  });
  return await createKernelAccount(client, {
    plugins: { sudo },
    entryPoint,
    kernelVersion: KERNEL_V3_2,
    initConfig: [installIntentExecutor(INTENT_V0_3)],
  });
}
