/**
 * Signing the relay's transactions: EIP-1559 (type 2), or EIP-7702 (type 4) when a submission
 * carries authorizations; a drain is EIP-2930 (type 1), paying exactly gas x gasPrice. Each is
 * an RLP list after its type byte, signed over keccak(type ‖ rlp(fields)). And reading back a
 * transaction another account signed (decoded): a sponsorship's, which the relay sends as it
 * is. As bruno/transactions.py's: the same bytes.
 */

import { secp256k1 } from "@noble/curves/secp256k1.js";
import { concatBytes } from "@noble/hashes/utils.js";
import { beyondGas, type DataFee } from "./datafee.ts";
import { type Address, bytes, checksum, hex, hexQuantity, keccak, SECP256K1_N } from "./evm.ts";

const ACCESS_LIST = 0x01;
const DYNAMIC_FEE = 0x02;
const SET_CODE = 0x04;
/** EIP-7702's authorization magic. */
const AUTHORIZATION_MAGIC = 0x05;
/** The widest a field not known yet can be: a nonce (EIP-2681) and gas limit 64 bits; a fee,
 * a value, a signature's r and s 256. */
const WIDEST_64 = 2n ** 64n - 1n;
const WIDEST_256 = 2n ** 256n - 1n;
/** The largest nonce or gas limit a decoded transaction may have: the largest integer a float64
 * holds exactly, read alike by both relays. */
const MOST_EXACT = BigInt(Number.MAX_SAFE_INTEGER);

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
function unsigned(kind: number, fields: readonly Rlp[]): Uint8Array {
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

/** An access list's entry (EIP-2930): an address and its storage keys. */
export interface Access {
  readonly address: Address;
  readonly storageKeys: readonly Uint8Array[];
}

/**
 * A transaction another account signed (EIP-1559 or EIP-7702), read back from its bytes:
 * `signed`, as the relay holds any signed transaction (its sender recovered from the signature),
 * and the fields it's simulated by; `to` null for a contract creation.
 */
export interface Decoded {
  readonly signed: SignedTransaction;
  readonly chainId: bigint;
  readonly maxPriorityFeePerGas: bigint;
  readonly to: Address | null;
  readonly data: Uint8Array;
  readonly accessList: readonly Access[];
  readonly authorizations: readonly SignedAuthorization[];
}

/** The Yellow Paper's RLP items: integers, byte strings, and lists of them. */
export type Rlp = number | bigint | Uint8Array | readonly Rlp[];
/** What RLP decodes to: a byte string, or a list of them. */
export type Item = Uint8Array | Item[];

/** An EIP-7702 authorization in a transaction's JSON. */
export function authorizationJson(a: SignedAuthorization): Record<string, string> {
  return {
    chainId: hexQuantity(a.chainId),
    address: a.address,
    nonce: hexQuantity(a.nonce),
    yParity: hexQuantity(a.yParity),
    r: hexQuantity(a.r),
    s: hexQuantity(a.s),
  };
}

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

function rlpLength(length: number, offset: number): Uint8Array {
  if (length < 56) {
    return Uint8Array.of(offset + length);
  }
  const encoded = minimalBytes(BigInt(length));
  return concatBytes(Uint8Array.of(offset + 55 + encoded.length), encoded);
}

/**
 * The signed EIP-1559 or EIP-7702 transaction `raw` is, its sender recovered: TransactionError
 * if it isn't one, strictly (its RLP canonical, every field in range, its signature's s low,
 * EIP-2), so `raw` is the only encoding of what's returned.
 */
export function decoded(raw: Uint8Array): Decoded {
  const kind = raw[0];
  if (kind !== DYNAMIC_FEE && kind !== SET_CODE) {
    throw new TransactionError("not an EIP-1559 (type 2) or EIP-7702 (type 4) transaction");
  }
  const fields = unrlp(raw.subarray(1));
  const count = kind === SET_CODE ? 13 : 12;
  if (!Array.isArray(fields) || fields.length !== count) {
    throw new TransactionError(`a type-${kind} transaction is a list of ${count} fields`);
  }
  const field = (index: number): Item => fields[index] as Item;
  const chainId = uint(field(0), WIDEST_256, "chainId");
  const nonce = uint(field(1), MOST_EXACT, "nonce");
  const tip = uint(field(2), WIDEST_256, "maxPriorityFeePerGas");
  const maxFee = uint(field(3), WIDEST_256, "maxFeePerGas");
  const gas = uint(field(4), MOST_EXACT, "gas");
  const value = uint(field(6), WIDEST_256, "value");
  const to = field(5);
  const data = field(7);
  const lengths = kind === SET_CODE ? [20] : [0, 20];
  if (!(to instanceof Uint8Array) || !lengths.includes(to.length)) {
    throw new TransactionError("to isn't an address");
  }
  if (!(data instanceof Uint8Array)) {
    throw new TransactionError("data isn't a byte string");
  }
  const accessList = accessListOf(field(8));
  let authorizations: SignedAuthorization[] = [];
  if (kind === SET_CODE) {
    const listed = field(9);
    if (!Array.isArray(listed) || listed.length === 0) {
      throw new TransactionError("a type-4 transaction lists at least one authorization");
    }
    authorizations = listed.map(authorizationOf);
  }
  const yParity = uint(field(count - 3), WIDEST_256, "y");
  const r = uint(field(count - 2), WIDEST_256, "r");
  const s = uint(field(count - 1), WIDEST_256, "s");
  const signature = signatureOf(yParity, r, s);
  let sender: Address;
  try {
    const point = secp256k1.Signature.fromBytes(signature, "recovered").recoverPublicKey(
      keccak(unsigned(kind, fields.slice(0, count - 3))),
    );
    sender = keyAddress(point.toBytes(false));
  } catch (e) {
    throw new TransactionError(`its signer can't be recovered: ${(e as Error).message}`);
  }
  return {
    signed: {
      raw,
      hash: keccak(raw),
      sender,
      nonce: Number(nonce),
      gas: Number(gas),
      maxFeePerGas: maxFee,
      value,
      beyondGas: 0n,
    },
    chainId,
    maxPriorityFeePerGas: tip,
    to: to.length === 0 ? null : checksum(hex(to)),
    data,
    accessList,
    authorizations,
  };
}

/**
 * A signature whose yParity is 0 or 1, its r and s in range and its s low (EIP-2), as its
 * recoverable bytes: TransactionError otherwise.
 */
function signatureOf(yParity: bigint, r: bigint, s: bigint): Uint8Array {
  if (yParity !== 0n && yParity !== 1n) {
    throw new TransactionError(`yParity is 0 or 1, not ${yParity}`);
  }
  if (!(0n < r && r < SECP256K1_N && 0n < s && s <= SECP256K1_N / 2n)) {
    throw new TransactionError("r and s aren't a signature's, s in the lower half (EIP-2)");
  }
  return concatBytes(Uint8Array.of(Number(yParity)), bytesOf(r, 32), bytesOf(s, 32));
}

/** An access list (EIP-2930) as decoded: each entry an address and its 32-byte keys. */
function accessListOf(item: Item): Access[] {
  if (!Array.isArray(item)) {
    throw new TransactionError("the access list isn't a list");
  }
  return item.map((entry) => {
    const wrong = "an access list's entry is an address and its storage keys";
    if (!Array.isArray(entry) || entry.length !== 2) throw new TransactionError(wrong);
    const [address, keys] = entry as [Item, Item];
    if (!(address instanceof Uint8Array) || address.length !== 20 || !Array.isArray(keys)) {
      throw new TransactionError(wrong);
    }
    const storageKeys = keys.map((key) => {
      if (!(key instanceof Uint8Array) || key.length !== 32) {
        throw new TransactionError("a storage key is 32 bytes");
      }
      return key;
    });
    return { address: checksum(hex(address)), storageKeys };
  });
}

/**
 * An authorization tuple as EIP-7702 lists it, decoded: chain_id, address, nonce, y_parity, r,
 * s.
 */
function authorizationOf(item: Item): SignedAuthorization {
  if (!Array.isArray(item) || item.length !== 6) {
    throw new TransactionError("an authorization is a list of 6 fields");
  }
  const [chain, address, count, y, r, s] = item as [Item, Item, Item, Item, Item, Item];
  if (!(address instanceof Uint8Array) || address.length !== 20) {
    throw new TransactionError("an authorization's address isn't one");
  }
  const chainId = uint(chain, WIDEST_256, "chainId");
  const nonce = uint(count, MOST_EXACT, "nonce");
  const yParity = uint(y, WIDEST_256, "y");
  const signed = { r: uint(r, WIDEST_256, "r"), s: uint(s, WIDEST_256, "s") };
  signatureOf(yParity, signed.r, signed.s);
  return {
    chainId,
    address: checksum(hex(address)),
    nonce,
    yParity: Number(yParity),
    r: signed.r,
    s: signed.s,
  };
}

/** An integer as RLP has it, its minimal big-endian bytes, at most `most`. */
function uint(item: Item, most: bigint, name: string): bigint {
  if (!(item instanceof Uint8Array) || item[0] === 0 || item.length > 32) {
    throw new TransactionError(`${name} isn't an integer, minimally encoded`);
  }
  const value = bigIntOf(item);
  if (value > most) {
    throw new TransactionError(`${name} is over ${most}`);
  }
  return value;
}

/**
 * The item `data` is the RLP of, and only that: TransactionError unless it's canonical (every
 * length minimal, a single byte under 0x80 as itself) with nothing after it.
 */
export function unrlp(data: Uint8Array): Item {
  const [item, end] = unrlpAt(data, 0);
  if (end !== data.length) {
    throw new TransactionError("bytes after the RLP item");
  }
  return item;
}

/** The RLP item starting at `at`, and where it ends. */
function unrlpAt(data: Uint8Array, at: number): [Item, number] {
  const first = data[at];
  if (first === undefined) {
    throw new TransactionError("the RLP ends early");
  }
  if (first < 0x80) {
    return [data.subarray(at, at + 1), at + 1];
  }
  const isList = first >= 0xc0;
  const [payload, length] = rlpPayload(data, at, isList ? 0xc0 : 0x80);
  const end = payload + length;
  if (end > data.length) {
    throw new TransactionError("the RLP ends early");
  }
  if (!isList) {
    if (length === 1 && (data[payload] as number) < 0x80) {
      throw new TransactionError("a single byte under 0x80 is its own RLP");
    }
    return [data.subarray(payload, end), end];
  }
  const items: Item[] = [];
  const within = data.subarray(0, end);
  let start = payload;
  while (start < end) {
    const [item, next] = unrlpAt(within, start);
    items.push(item);
    start = next;
  }
  return [items, end];
}

/** Where the payload of the string or list at `at` starts, and its length. */
function rlpPayload(data: Uint8Array, at: number, offset: number): [number, number] {
  const short = (data[at] as number) - offset;
  if (short < 56) {
    return [at + 1, short];
  }
  const width = short - 55;
  const encoded = data.subarray(at + 1, at + 1 + width);
  if (encoded.length !== width || encoded[0] === 0) {
    throw new TransactionError("an RLP length isn't minimal");
  }
  const length = bigIntOf(encoded);
  if (length < 56n) {
    throw new TransactionError("an RLP length isn't minimal");
  }
  if (length > BigInt(data.length)) {
    throw new TransactionError("the RLP ends early");
  }
  return [at + 1 + width, Number(length)];
}
