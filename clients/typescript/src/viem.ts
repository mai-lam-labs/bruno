/** A viem account (privateKeyToAccount, a wallet client's account, a CDP or Privy account
 * through viem, a smart account) as a Signer: viem takes typed data's integers as bigints, not
 * strings. A smart account's getFactoryArgs is kept, so its deployment is priced first. */

import type { FactoryArgs, Hex, Signer, TypedData } from "./types.ts";

/** What of a viem account signing typed data needs: its address, and signTypedData; and a
 * smart account's getFactoryArgs. */
export interface ViemAccount {
  readonly address: Hex;
  getFactoryArgs?(): Promise<FactoryArgs>;
  signTypedData(parameters: {
    domain: TypedData["domain"];
    types: Record<string, readonly { name: string; type: string }[]>;
    primaryType: string;
    message: Record<string, unknown>;
  }): Promise<Hex>;
}

export function viemSigner(account: ViemAccount): Signer {
  const getFactoryArgs = account.getFactoryArgs?.bind(account);
  return {
    address: account.address,
    ...(getFactoryArgs === undefined ? {} : { getFactoryArgs }),
    signTypedData: (typedData) => {
      const fields = typedData.types[typedData.primaryType] ?? [];
      const message: Record<string, unknown> = { ...typedData.message };
      for (const { name, type } of fields) {
        if (/^u?int[0-9]*$/.test(type) && typeof message[name] === "string") {
          message[name] = BigInt(message[name] as string);
        }
      }
      return account.signTypedData({
        domain: typedData.domain,
        types: { ...typedData.types },
        primaryType: typedData.primaryType,
        message,
      });
    },
  };
}
