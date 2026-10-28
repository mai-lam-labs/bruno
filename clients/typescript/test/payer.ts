/**
 * The tests' payer: a key that signs typed data by EIP-712's rules, as a wallet does, and the
 * same key as a smart account not yet deployed, its signature wrapped (ERC-6492).
 */

import { secp256k1 } from "@noble/curves/secp256k1.js";
import { keccak_256 } from "@noble/hashes/sha3.js";
import { concatBytes } from "@noble/hashes/utils.js";
import { bytes, hex } from "../../../worker/src/evm.ts";
import { bytesOf } from "../../../worker/src/transactions.ts";
import type { Hex, Signer, TypedData } from "../src/types.ts";

const KEY = keccak_256(new TextEncoder().encode("the client tests' payer"));

export const PAYER: Hex = hex(keccak_256(secp256k1.getPublicKey(KEY, false).slice(1)).slice(12));

/** `n` as one 32-byte word. */
export function word(n: bigint): Uint8Array {
  return bytesOf(n, 32);
}

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
    if (type === "bytes32") return bytes(String(value) as Hex);
    return word(BigInt(value));
  });
  return keccak_256(concatBytes(keccak_256(new TextEncoder().encode(typeString)), ...encoded));
}

/** EIP-712's digest of `td`, as a wallet signs it. */
export function digestOf(td: TypedData): Uint8Array {
  return keccak_256(concatBytes(
    Uint8Array.of(0x19, 0x01),
    hashStruct(td.types, "EIP712Domain", td.domain),
    hashStruct(td.types, td.primaryType, td.message),
  ));
}

/** The payer's key, signing as a wallet does. */
export const signer: Signer = {
  address: PAYER,
  signTypedData(td: TypedData): Promise<Hex> {
    const signature = secp256k1.sign(digestOf(td), KEY, { prehash: false, format: "recovered" });
    // r ‖ s ‖ v, v 27 or 28
    return Promise.resolve(
      hex(concatBytes(signature.slice(1), Uint8Array.of((signature[0] as number) + 27))),
    );
  },
};

/** `base` noting in `signed` whether it signed anything. */
export function counting(base: Signer, signed: { signed: boolean }): Signer {
  return {
    ...base,
    signTypedData: (td) => ((signed.signed = true), base.signTypedData(td)),
  };
}

export const FACTORY: Hex = "0x0BA5ED0c6AA8c49038F819E587E2633c4A9F428a";
export const FACTORY_DATA: Hex = `0x3ffba36f${"00".repeat(64)}`;

/** `signature` wrapped as an undeployed smart account's (ERC-6492): the factory and its data
 * around it. */
export function wrappedSignature(signature: Hex): Hex {
  const padded = (data: Uint8Array) =>
    concatBytes(word(BigInt(data.length)), data, new Uint8Array((32 - (data.length % 32)) % 32));
  const factoryData = padded(bytes(FACTORY_DATA));
  const inner = padded(bytes(signature));
  return hex(concatBytes(
    concatBytes(new Uint8Array(12), bytes(FACTORY)),
    word(96n),
    word(BigInt(96 + factoryData.length)),
    factoryData,
    inner,
    bytes(`0x${"6492".repeat(16)}`),
  ));
}
