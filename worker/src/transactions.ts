/**
 * Signing the relay's transactions: EIP-1559 (type 2), or EIP-7702 (type 4) when a submission
 * carries authorizations; a drain is EIP-2930 (type 1), paying exactly gas x gasPrice. Each is
 * an RLP list after its type byte, signed over keccak(type ‖ rlp(fields)). A port of the
 * Python reference (bruno/transactions.py): the same bytes.
 */

import { secp256k1 } from "@noble/curves/secp256k1.js";
import { concatBytes } from "@noble/hashes/utils.js";
import { beyondGas, type DataFee } from "./datafee.ts";
import { type Address, bytes, checksum, hex, keccak } from "./evm.ts";

const ACCESS_LIST = 0x01;
const DYNAMIC_FEE = 0x02;
const SET_CODE = 0x04;
/** EIP-7702's authorization magic. */
const AUTHORIZATION_MAGIC = 0x05;
/** The widest a field not known yet can be: a nonce (EIP-2681) and gas limit 64 bits; a fee,
 * a value, a signature's r and s 256. */
const WIDEST_64 = 2n ** 64n - 1n;
const WIDEST_256 = 2n ** 256n - 1n;

export class TransactionError extends Error {}

/** A key that signs, and its address. */
export interface Signer {
  readonly privateKey: Uint8Array;
  readonly address: Address;
}

/** EIP-1559's two fees per gas, in wei; and, on an OP-stack chain, the inputs of what a
 * transaction pays beyond gas, as high as they can be while these fees are (lastingFees). */
export interface Fees {
  readonly maxFeePerGas: bigint;
  readonly maxPriorityFeePerGas: bigint;
  readonly dataFee?: DataFee;
}

/** The most a transaction given `gas`, of `unsignedSize` bytes unsigned, sending `value`, costs
 * its sender at `fees`: its gas at the max fee, its value, and what's charged beyond gas. */
export function cost(fees: Fees, gas: number, unsignedSize: number, value = 0n): bigint {
  const beyond = fees.dataFee === undefined ? 0n : beyondGas(fees.dataFee, unsignedSize, gas);
  return BigInt(gas) * fees.maxFeePerGas + value + beyond;
}

/** An EIP-7702 authorization, signed by its authority. */
export interface SignedAuthorization {
  readonly chainId: bigint;
  readonly address: Address;
  readonly nonce: bigint;
  readonly yParity: number;
  readonly r: bigint;
  readonly s: bigint;
}

/** A signed transaction, and what the relay needs to know of it. */
export interface SignedTransaction {
  readonly raw: Uint8Array;
  readonly hash: Uint8Array;
  readonly sender: Address;
  readonly nonce: number;
  readonly gas: number;
  readonly maxFeePerGas: bigint;
  readonly value: bigint;
  /** What it pays beyond gas at most (an OP-stack chain's fees), from its own size. */
  readonly beyondGas: bigint;
}

/** The balance its sender needs for it to be valid: all its gas at the max fee, its value, and
 * what it pays beyond gas. */
export function maxCost(tx: SignedTransaction): bigint {
  return BigInt(tx.gas) * tx.maxFeePerGas + tx.value + tx.beyondGas;
}

/** The account a 32-byte private key controls. */
export function signer(privateKey: Uint8Array): Signer {
  return { privateKey, address: keyAddress(secp256k1.getPublicKey(privateKey, false)) };
}

/** The address of an uncompressed public key. */
export function keyAddress(uncompressed: Uint8Array): Address {
  return checksum(hex(keccak(uncompressed.slice(1)).slice(12)));
}

export function sign(
  account: Signer,
  chainId: number,
  nonce: number,
  fees: Fees,
  gas: number,
  to: Address | null,
  data: Uint8Array,
  value = 0n,
  authorizations: readonly SignedAuthorization[] = [],
): SignedTransaction {
  if (authorizations.length > 0 && to === null) {
    throw new TransactionError("a type-4 transaction can't create a contract (EIP-7702)");
  }
  const fields: Rlp[] = [
    chainId,
    nonce,
    fees.maxPriorityFeePerGas,
    fees.maxFeePerGas,
    gas,
    to === null ? new Uint8Array() : bytes(to),
    value,
    data,
    [], // access list
  ];
  let kind = DYNAMIC_FEE;
  if (authorizations.length > 0) {
    kind = SET_CODE;
    fields.push(authorizations.map(authorizationFields));
  }
  const beyond = fees.dataFee === undefined
    ? 0n
    : beyondGas(fees.dataFee, unsigned(kind, fields).length, gas);
  return signed(account, kind, fields, nonce, gas, fees.maxFeePerGas, value, beyond);
}

/** The most bytes a transaction to `to` carrying `data` (and `authorizations`) can take
 * unsigned, before its nonce, fees, gas and authorizations are known: each at its widest. */
export function unsignedSize(
  chainId: number,
  to: Address | null,
  data: Uint8Array,
  value = 0n,
  authorizations = 0,
): number {
  const fields: Rlp[] = [
    chainId,
    WIDEST_64,
    WIDEST_256,
    WIDEST_256,
    WIDEST_64,
    to === null ? new Uint8Array() : bytes(to),
    value > 0n ? WIDEST_256 : 0,
    data,
    [],
  ];
  let kind = DYNAMIC_FEE;
  if (authorizations > 0) {
    kind = SET_CODE;
    const widest: Rlp[] = [WIDEST_256, new Uint8Array(20), WIDEST_64, 1, WIDEST_256, WIDEST_256];
    fields.push(Array.from({ length: authorizations }, () => widest));
  }
  return unsigned(kind, fields).length;
}

/**
 * A type-1 transfer of all `balance` to `to`: given `gas`, exactly what the transfer uses, it
 * pays exactly gas x `gasPrice`, so the account ends at 0. On an OP-stack chain (`dataFee`)
 * what it pays beyond gas is only bounded: the account ends with the bound's slack. Null if
 * `balance` doesn't cover it.
 */
export function signDrain(
  account: Signer,
  chainId: number,
  nonce: number,
  balance: bigint,
  gasPrice: bigint,
  to: Address,
  gas: number,
  dataFee?: DataFee,
): SignedTransaction | null {
  const fields: Rlp[] = [
    chainId,
    nonce,
    gasPrice,
    gas,
    bytes(to),
    WIDEST_256,
    new Uint8Array(),
    [],
  ];
  const beyond = dataFee === undefined
    ? 0n
    : beyondGas(dataFee, unsigned(ACCESS_LIST, fields).length, gas);
  const value = balance - BigInt(gas) * gasPrice - beyond;
  if (value <= 0n) {
    return null;
  }
  fields[5] = value;
  return signed(account, ACCESS_LIST, fields, nonce, gas, gasPrice, value, beyond);
}

function signed(
  account: Signer,
  kind: number,
  fields: Rlp[],
  nonce: number,
  gas: number,
  maxFeePerGas: bigint,
  value: bigint,
  beyond = 0n,
): SignedTransaction {
  const digest = keccak(unsigned(kind, fields));
  const signature = secp256k1.sign(digest, account.privateKey, {
    prehash: false,
    format: "recovered",
  });
  const yParity = signature[0] as number;
  const r = bigIntOf(signature.slice(1, 33));
  const s = bigIntOf(signature.slice(33, 65));
  const raw = concatBytes(Uint8Array.of(kind), rlp([...fields, yParity, r, s]));
  return {
    raw,
    hash: keccak(raw),
    sender: account.address,
    nonce,
    gas,
    maxFeePerGas,
    value,
    beyondGas: beyond,
  };
}

/** A typed transaction unsigned: its type byte and its fields' RLP, what's signed. */
function unsigned(kind: number, fields: Rlp[]): Uint8Array {
  return concatBytes(Uint8Array.of(kind), rlp(fields));
}

/** The hash an authorization's authority signs (EIP-7702). */
export function authorizationHash(auth: SignedAuthorization): Uint8Array {
  return keccak(
    concatBytes(
      Uint8Array.of(AUTHORIZATION_MAGIC),
      rlp([auth.chainId, bytes(auth.address), auth.nonce]),
    ),
  );
}

/** The account that signed `auth`. */
export function authority(auth: SignedAuthorization): Address {
  if (auth.yParity !== 0 && auth.yParity !== 1) {
    throw new TransactionError(`yParity is 0 or 1, not ${auth.yParity}`);
  }
  const signature = concatBytes(
    Uint8Array.of(auth.yParity),
    bytesOf(auth.r, 32),
    bytesOf(auth.s, 32),
  );
  const point = secp256k1.Signature.fromBytes(signature, "recovered").recoverPublicKey(
    authorizationHash(auth),
  );
  return keyAddress(point.toBytes(false));
}

/** An authorization tuple as EIP-7702 lists it: chain_id, address, nonce, y_parity, r, s. */
function authorizationFields(auth: SignedAuthorization): Rlp[] {
  return [auth.chainId, bytes(auth.address), auth.nonce, auth.yParity, auth.r, auth.s];
}

/** The Yellow Paper's RLP items: integers, byte strings, and lists of them. */
export type Rlp = number | bigint | Uint8Array | Rlp[];

/**
 * RLP (Yellow Paper, appendix B): an integer as its minimal big-endian bytes (0 as none), a
 * byte string under 0x80 as itself, else a length-prefixed string or list.
 */
export function rlp(item: Rlp): Uint8Array {
  if (typeof item === "number" || typeof item === "bigint") {
    return rlp(minimalBytes(BigInt(item)));
  }
  if (item instanceof Uint8Array) {
    if (item.length === 1 && (item[0] as number) < 0x80) {
      return item;
    }
    return concatBytes(rlpLength(item.length, 0x80), item);
  }
  const payload = concatBytes(...item.map(rlp));
  return concatBytes(rlpLength(payload.length, 0xc0), payload);
}

function rlpLength(length: number, offset: number): Uint8Array {
  if (length < 56) {
    return Uint8Array.of(offset + length);
  }
  const encoded = minimalBytes(BigInt(length));
  return concatBytes(Uint8Array.of(offset + 55 + encoded.length), encoded);
}

function minimalBytes(value: bigint): Uint8Array {
  if (value < 0n) {
    throw new TransactionError(`RLP has no negative integers: ${value}`);
  }
  const out: number[] = [];
  for (let v = value; v > 0n; v >>= 8n) {
    out.unshift(Number(v & 0xffn));
  }
  return Uint8Array.from(out);
}

export function bigIntOf(data: Uint8Array): bigint {
  let v = 0n;
  for (const b of data) {
    v = (v << 8n) | BigInt(b);
  }
  return v;
}

/** `value` big-endian in exactly `width` bytes; one that doesn't fit, or is negative, throws. */
export function bytesOf(value: bigint, width: number): Uint8Array {
  if (value < 0n || value >= 1n << BigInt(8 * width)) {
    throw new TransactionError(`${value} doesn't fit in ${width} bytes`);
  }
  const out = new Uint8Array(width);
  let v = value;
  for (let i = width - 1; i >= 0; i--) {
    out[i] = Number(v & 0xffn);
    v >>= 8n;
  }
  return out;
}

/** An EIP-7702 authorization in a transaction's JSON. */
export function authorizationJson(a: SignedAuthorization): Record<string, string> {
  const q = (v: bigint | number): string => `0x${v.toString(16)}`;
  return {
    chainId: q(a.chainId),
    address: a.address,
    nonce: q(a.nonce),
    yParity: q(a.yParity),
    r: q(a.r),
    s: q(a.s),
  };
}
