/**
 * EVM bytecode assembly with gas bounds: the runner's initcode and the funding initcode are
 * emitted with it, so each knows the most gas its success path can take when it's emitted.
 * A port of the Python reference (bruno/assembler.py): the same bytes, the same bounds.
 */

import { concatBytes } from "@noble/hashes/utils.js";
import { type Address, bytes, memoryGas } from "./evm.ts";

/** The opcodes the runner uses (the Yellow Paper's, and EIP-3855's PUSH0). */
export const Op = {
  ADD: 0x01,
  SUB: 0x03,
  LT: 0x10,
  EQ: 0x14,
  ISZERO: 0x15,
  AND: 0x16,
  NOT: 0x19,
  BALANCE: 0x31,
  ORIGIN: 0x32,
  CODECOPY: 0x39,
  EXTCODESIZE: 0x3b,
  RETURNDATASIZE: 0x3d,
  RETURNDATACOPY: 0x3e,
  POP: 0x50,
  MLOAD: 0x51,
  MSTORE: 0x52,
  JUMPI: 0x57,
  GAS: 0x5a,
  JUMPDEST: 0x5b,
  PUSH0: 0x5f,
  PUSH1: 0x60,
  PUSH2: 0x61,
  PUSH3: 0x62,
  PUSH20: 0x73,
  DUP2: 0x81,
  DUP3: 0x82,
  SWAP1: 0x90,
  CALL: 0xf1,
  STATICCALL: 0xfa,
  REVERT: 0xfd,
  SELFDESTRUCT: 0xff,
} as const;

export type Op = (typeof Op)[keyof typeof Op];

/** Each opcode's gas where it's a constant (Yellow Paper, EIP-3855), memory expansion aside. */
const STATIC_GAS: ReadonlyMap<Op, number> = new Map<Op, number>([
  [Op.ADD, 3],
  [Op.SUB, 3],
  [Op.LT, 3],
  [Op.EQ, 3],
  [Op.ISZERO, 3],
  [Op.AND, 3],
  [Op.NOT, 3],
  [Op.RETURNDATASIZE, 2],
  [Op.POP, 2],
  [Op.JUMPI, 10],
  [Op.GAS, 2],
  [Op.DUP2, 3],
  [Op.DUP3, 3],
  [Op.SWAP1, 3],
  [Op.ORIGIN, 2],
  // its beneficiary the transaction's sender, warm and never dead, and no balance to send: the
  // base cost alone, unchanged by EIP-8038
  [Op.SELFDESTRUCT, 5_000],
]);
const PUSH0_GAS = 2;
const PUSH_GAS = 3;
const JUMPDEST_GAS = 1;
const COPY_WORD_GAS = 3;
/** A label or data offset is pushed in 2 bytes: initcode is under 49,152 bytes (EIP-3860). */
const OFFSET_PUSH_BYTES = 3;
/** A reserve is pushed in 3 bytes: under 2**24 gas, EIP-7825's limit on a transaction. */
const RESERVE_PUSH_BYTES = 4;
const MAX_RESERVE = 2 ** 24 - 1;

type Piece =
  /** Code, its most gas (null: unbounded, only after the success path), and its memory end. */
  | { kind: "code"; code: Uint8Array; gas: number | null; memoryEnd: number }
  | { kind: "label"; name: string }
  /** PUSH2 of a label's offset. */
  | { kind: "jump"; to: string }
  /** PUSH2 of an appended blob's offset in the initcode. */
  | { kind: "data"; index: number }
  /** PUSH3 of call `call`'s R: the success path's gas after its gas read, and `plus`. */
  | { kind: "reserve"; call: number; plus: number }
  /** The GAS that call `call`'s R is measured from. */
  | { kind: "gasRead"; call: number }
  /** The end of the success path: what follows is reverts, never summed. */
  | { kind: "end" };

export class AssemblyError extends Error {}

function bigEndian(value: bigint, width: number): Uint8Array {
  const out = new Uint8Array(width);
  let v = value;
  for (let i = width - 1; i >= 0; i--) {
    out[i] = Number(v & 0xffn);
    v >>= 8n;
  }
  if (v !== 0n) {
    throw new AssemblyError(`${value} doesn't fit in ${width} bytes`);
  }
  return out;
}

function byteWidth(value: bigint): number {
  let width = 0;
  for (let v = value; v > 0n; v >>= 8n) {
    width++;
  }
  return width;
}

/**
 * Bytecode with labels, data appended after the code, and the success path's gas: the most gas
 * the code can take on its way to `end()`, each piece's own worst case summed, memory expansion
 * included. What follows `end()` is reverts, never summed.
 */
export class Assembly {
  private readonly pieces: Piece[] = [];

  private add(piece: Piece): this {
    this.pieces.push(piece);
    return this;
  }

  private code(code: Uint8Array, gas: number | null, memoryEnd = 0): this {
    return this.add({ kind: "code", code, gas, memoryEnd });
  }

  op(...ops: Op[]): this {
    for (const op of ops) {
      this.code(Uint8Array.of(op), STATIC_GAS.get(op) ?? null);
    }
    return this;
  }

  push(value: bigint | number): this {
    const v = BigInt(value);
    if (v === 0n) {
      return this.code(Uint8Array.of(Op.PUSH0), PUSH0_GAS);
    }
    const width = byteWidth(v);
    if (width > 32) {
      throw new AssemblyError(`${value} is over 32 bytes: no PUSH takes it`);
    }
    return this.code(
      concatBytes(Uint8Array.of(Op.PUSH1 + width - 1), bigEndian(v, width)),
      PUSH_GAS,
    );
  }

  pushAddress(address: Address): this {
    return this.code(concatBytes(Uint8Array.of(Op.PUSH20), bytes(address)), PUSH_GAS);
  }

  pushData(index: number): this {
    return this.add({ kind: "data", index });
  }

  /** MSTORE of the value on the stack at a fixed offset. */
  mstoreAt(offset: number): this {
    this.push(offset);
    return this.code(Uint8Array.of(Op.MSTORE), 3, offset + 32);
  }

  mloadAt(offset: number): this {
    this.push(offset);
    return this.code(Uint8Array.of(Op.MLOAD), 3, offset + 32);
  }

  /** CODECOPY of `length` bytes to memory[0:length], its arguments pushed. */
  codecopy(length: number): this {
    return this.code(
      Uint8Array.of(Op.CODECOPY),
      3 + COPY_WORD_GAS * Math.ceil(length / 32),
      length,
    );
  }

  balance(access: number): this {
    return this.code(Uint8Array.of(Op.BALANCE), access);
  }

  extcodesize(access: number): this {
    return this.code(Uint8Array.of(Op.EXTCODESIZE), access);
  }

  /** STATICCALL given `cap` gas: its access, and what EIP-150's 63/64 rule needs left. */
  staticcall(access: number, cap: number): this {
    return this.code(Uint8Array.of(Op.STATICCALL), access + Math.ceil((cap * 64) / 63));
  }

  /** A CALL sending no value, given `cap` gas: its access, and what the 63/64 rule needs. */
  cappedCall(access: number, cap: number): this {
    return this.code(Uint8Array.of(Op.CALL), access + Math.ceil((cap * 64) / 63));
  }

  /** A CALL sending value with no gas but the stipend: `charge` is all it's charged. */
  valueCall(charge: number): this {
    return this.code(Uint8Array.of(Op.CALL), charge);
  }

  /**
   * A CALL to code the client chooses: only its access counts, since it's given whatever's left
   * above its R.
   */
  clientCall(access: number): this {
    return this.code(Uint8Array.of(Op.CALL), access);
  }

  /** PUSH of call `call`'s R, and `plus` kept back besides. */
  reserve(call: number, plus = 0): this {
    return this.add({ kind: "reserve", call, plus });
  }

  gasRead(call: number): this {
    return this.add({ kind: "gasRead", call });
  }

  jumpi(label: string): this {
    this.add({ kind: "jump", to: label });
    return this.op(Op.JUMPI);
  }

  label(name: string): this {
    return this.add({ kind: "label", name });
  }

  end(): this {
    return this.add({ kind: "end" });
  }

  /** Each success-path piece's most gas, its memory expansion included. */
  private successPathGas(): number[] {
    const costs: number[] = [];
    let words = 0;
    for (const piece of this.pieces) {
      switch (piece.kind) {
        case "end":
          return costs;
        case "code": {
          if (piece.gas === null) {
            throw new AssemblyError(`opcode ${piece.code[0]} has no bound on the success path`);
          }
          const grown = Math.max(words, Math.ceil(piece.memoryEnd / 32));
          costs.push(piece.gas + memoryGas(grown) - memoryGas(words));
          words = grown;
          break;
        }
        case "label":
          costs.push(JUMPDEST_GAS);
          break;
        case "jump":
        case "data":
        case "reserve":
          costs.push(PUSH_GAS);
          break;
        case "gasRead":
          costs.push(STATIC_GAS.get(Op.GAS) ?? 0);
          break;
      }
    }
    throw new AssemblyError("the success path has no end");
  }

  /** The initcode, and the success path's most gas. */
  assemble(blobs: readonly Uint8Array[]): { code: Uint8Array; gas: number } {
    const costs = this.successPathGas();
    const reserves = new Map<number, number>();
    this.pieces.slice(0, costs.length).forEach((piece, k) => {
      if (piece.kind === "gasRead") {
        reserves.set(piece.call, costs.slice(k + 1).reduce((a, b) => a + b, 0));
      }
    });
    const labels = new Map<string, number>();
    let position = 0;
    for (const piece of this.pieces) {
      switch (piece.kind) {
        case "code":
          position += piece.code.length;
          break;
        case "label":
          labels.set(piece.name, position);
          position += 1; // its JUMPDEST
          break;
        case "jump":
        case "data":
          position += OFFSET_PUSH_BYTES;
          break;
        case "reserve":
          position += RESERVE_PUSH_BYTES;
          break;
        case "gasRead":
          position += 1;
          break;
        case "end":
          break;
      }
    }
    const offsets: number[] = [];
    for (const blob of blobs) {
      offsets.push(position);
      position += blob.length;
    }
    const parts: Uint8Array[] = [];
    for (const piece of this.pieces) {
      switch (piece.kind) {
        case "code":
          parts.push(piece.code);
          break;
        case "label":
          parts.push(Uint8Array.of(Op.JUMPDEST));
          break;
        case "jump":
          parts.push(
            Uint8Array.of(Op.PUSH2),
            bigEndian(BigInt(required(labels.get(piece.to), `label ${piece.to}`)), 2),
          );
          break;
        case "data":
          parts.push(
            Uint8Array.of(Op.PUSH2),
            bigEndian(BigInt(required(offsets[piece.index], `blob ${piece.index}`)), 2),
          );
          break;
        case "reserve": {
          const reserve = required(reserves.get(piece.call), `reserve ${piece.call}`) +
            piece.plus;
          if (reserve > MAX_RESERVE) {
            throw new AssemblyError(`a reserve of ${reserve} gas is over 2**24`);
          }
          parts.push(Uint8Array.of(Op.PUSH3), bigEndian(BigInt(reserve), 3));
          break;
        }
        case "gasRead":
          parts.push(Uint8Array.of(Op.GAS));
          break;
        case "end":
          break;
      }
    }
    return {
      code: concatBytes(...parts, ...blobs),
      gas: costs.reduce((a, b) => a + b, 0),
    };
  }
}

function required<T>(value: T | undefined, what: string): T {
  if (value === undefined) {
    throw new AssemblyError(`no ${what}`);
  }
  return value;
}
