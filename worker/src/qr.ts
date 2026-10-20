/**
 * QR codes as the Python reference makes them with segno (bruno/server/front.py, qr): byte
 * mode, error correction level M, the smallest of versions 1 to 10 that holds the text, and the
 * mask segno's penalty rules choose. Each step follows segno's, including its choices where
 * ISO/IEC 18004 leaves room: a zero byte after the terminator, and masks scored before the
 * format and version information is written. Checked against segno's codes (make vectors).
 */

export class QrError extends Error {}

/** Each version's blocks at level M: [blocks, codewords a block, data codewords a block]. */
const BLOCKS: readonly (readonly (readonly [number, number, number])[])[] = [
  [],
  [[1, 26, 16]],
  [[1, 44, 28]],
  [[1, 70, 44]],
  [[2, 50, 32]],
  [[2, 67, 43]],
  [[4, 43, 27]],
  [[4, 49, 31]],
  [[2, 60, 38], [2, 61, 39]],
  [[3, 58, 36], [2, 59, 37]],
  [[4, 69, 43], [1, 70, 44]],
];
/** Each version's alignment pattern centres, on both axes. */
const ALIGNMENT: readonly (readonly number[])[] = [
  [],
  [],
  [6, 18],
  [6, 22],
  [6, 26],
  [6, 30],
  [6, 34],
  [6, 22, 38],
  [6, 24, 42],
  [6, 26, 46],
  [6, 28, 50],
];
const MAX_VERSION = 10;
/** Level M's format information, a mask: its 15 bits, masked. */
const FORMAT_INFO = [0x5412, 0x5125, 0x5e7c, 0x5b4b, 0x45f9, 0x40ce, 0x4f97, 0x4aa0];
/** A finder pattern, with the separator around it. */
const FINDER = [
  [0, 0, 0, 0, 0, 0, 0, 0, 0],
  [0, 1, 1, 1, 1, 1, 1, 1, 0],
  [0, 1, 0, 0, 0, 0, 0, 1, 0],
  [0, 1, 0, 1, 1, 1, 0, 1, 0],
  [0, 1, 0, 1, 1, 1, 0, 1, 0],
  [0, 1, 0, 1, 1, 1, 0, 1, 0],
  [0, 1, 0, 0, 0, 0, 0, 1, 0],
  [0, 1, 1, 1, 1, 1, 1, 1, 0],
  [0, 0, 0, 0, 0, 0, 0, 0, 0],
];
const ALIGNMENT_PATTERN = [
  [1, 1, 1, 1, 1],
  [1, 0, 0, 0, 1],
  [1, 0, 1, 0, 1],
  [1, 0, 0, 0, 1],
  [1, 1, 1, 1, 1],
];
/** A module not yet set: the encoding region, before the codewords are placed. */
const UNSET = 2;
const MASKS: readonly ((i: number, j: number) => boolean)[] = [
  (i, j) => ((i + j) & 1) === 0,
  (i) => (i & 1) === 0,
  (_, j) => j % 3 === 0,
  (i, j) => (i + j) % 3 === 0,
  (i, j) => ((Math.floor(i / 2) + Math.floor(j / 3)) & 1) === 0,
  (i, j) => ((i * j) & 1) + (i * j) % 3 === 0,
  (i, j) => ((((i * j) & 1) + (i * j) % 3) & 1) === 0,
  (i, j) => ((((i + j) & 1) + (i * j) % 3) & 1) === 0,
];
const N3_PATTERN = [1, 0, 1, 1, 1, 0, 1];

/** `text` (ASCII) as a QR code's modules, row by row, 1 dark. */
export function qr(text: string): Uint8Array[] {
  const data = Uint8Array.from(text, (c) => {
    const code = c.charCodeAt(0);
    if (code > 0x7f) throw new QrError("only ASCII is encoded");
    return code;
  });
  const version = versionFor(data.length);
  const size = 17 + 4 * version;
  const matrix = blank(size, version);
  addCodewords(matrix, finalMessage(version, codewords(version, data)));
  const [mask, masked] = bestMask(matrix, version);
  addFormatInfo(masked, mask);
  addVersionInfo(masked, version);
  return masked;
}

function capacity(version: number): number {
  return (BLOCKS[version] ?? []).reduce((sum, [n, , d]) => sum + n * d, 0) * 8;
}

function countBits(version: number): number {
  return version < 10 ? 8 : 16;
}

/** The smallest version whose capacity holds the mode, the count and the bytes. */
function versionFor(length: number): number {
  for (let version = 1; version <= MAX_VERSION; version++) {
    if (4 + countBits(version) + 8 * length <= capacity(version)) return version;
  }
  throw new QrError(`${length} bytes: over version ${MAX_VERSION}'s capacity`);
}

/** The data codewords: mode, count, bytes, terminator, a zero byte, then pad codewords. */
function codewords(version: number, data: Uint8Array): number[] {
  const bits: number[] = [];
  const push = (value: number, length: number): void => {
    for (let i = length - 1; i >= 0; i--) bits.push((value >>> i) & 1);
  };
  push(0b0100, 4);
  push(data.length, countBits(version));
  for (const byte of data) push(byte, 8);
  const cap = capacity(version);
  push(0, Math.min(cap - bits.length, 4));
  push(0, 8 - (bits.length % 8));
  const pads = cap / 8 - Math.floor(bits.length / 8);
  for (let i = 0; i < pads; i++) push(i % 2 === 0 ? 0xec : 0x11, 8);
  const bytes: number[] = [];
  for (let i = 0; i < cap; i += 8) {
    bytes.push(bits.slice(i, i + 8).reduce((byte, bit) => (byte << 1) | bit, 0));
  }
  return bytes;
}

/** The data and error correction codewords, each interleaved across the blocks, as bits,
 * with the version's remainder bits. */
function finalMessage(version: number, data: readonly number[]): number[] {
  const dataBlocks: number[][] = [];
  const errorBlocks: number[][] = [];
  let at = 0;
  for (const [count, total, length] of BLOCKS[version] ?? []) {
    const divisor = generator(total - length);
    for (let i = 0; i < count; i++) {
      const block = data.slice(at, at + length);
      at += length;
      dataBlocks.push(block);
      errorBlocks.push(remainder(block, divisor));
    }
  }
  const bits: number[] = [];
  for (const blocks of [dataBlocks, errorBlocks]) {
    const longest = Math.max(...blocks.map((b) => b.length));
    for (let i = 0; i < longest; i++) {
      for (const block of blocks) {
        const byte = block[i];
        if (byte === undefined) continue;
        for (let k = 7; k >= 0; k--) bits.push((byte >>> k) & 1);
      }
    }
  }
  const remainderBits = version >= 2 && version <= 6 ? 7 : 0;
  for (let i = 0; i < remainderBits; i++) bits.push(0);
  return bits;
}

/** GF(2^8) product, modulo x^8 + x^4 + x^3 + x^2 + 1. */
function multiply(x: number, y: number): number {
  let z = 0;
  for (let i = 7; i >= 0; i--) {
    z = (z << 1) ^ ((z >>> 7) * 0x11d);
    z ^= ((y >>> i) & 1) * x;
  }
  return z;
}

/** The Reed-Solomon generator of `degree`: (x - a^0) ... (x - a^(degree - 1)), its leading 1
 * dropped. */
function generator(degree: number): number[] {
  const result = new Array<number>(degree).fill(0);
  result[degree - 1] = 1;
  let root = 1;
  for (let i = 0; i < degree; i++) {
    for (let j = 0; j < degree; j++) {
      result[j] = multiply(result[j] as number, root) ^ (result[j + 1] ?? 0);
    }
    root = multiply(root, 0x02);
  }
  return result;
}

/** `data`'s error correction codewords: its remainder by `divisor`. */
function remainder(data: readonly number[], divisor: readonly number[]): number[] {
  const result = new Array<number>(divisor.length).fill(0);
  for (const byte of data) {
    const factor = byte ^ (result.shift() as number);
    result.push(0);
    divisor.forEach((coefficient, i) => {
      result[i] = (result[i] as number) ^ multiply(coefficient, factor);
    });
  }
  return result;
}

/** The matrix with its function patterns, the format and version areas 0, the rest unset. */
function blank(size: number, version: number): Uint8Array[] {
  const m = Array.from({ length: size }, () => new Uint8Array(size).fill(UNSET));
  const at = (i: number): Uint8Array => m[(i + size) % size] as Uint8Array;
  if (version >= 7) {
    for (let i = 0; i < 6; i++) {
      for (const k of [11, 10, 9]) {
        at(i)[size - k] = 0;
        at(size - k)[i] = 0;
      }
    }
  }
  for (let i = 0; i < 9; i++) {
    at(i)[8] = 0;
    at(8)[i] = 0;
    at(-i)[8] = 0;
    at(8)[(size - i) % size] = 0;
  }
  let bit = 1;
  for (let i = 8; i < size - 8; i++) {
    at(i)[6] = bit;
    at(6)[i] = bit;
    bit ^= 1;
  }
  for (const [i, j] of [[0, 0], [0, size - 8], [size - 8, 0]] as const) {
    const offset = i === 0 ? 1 : 0;
    const separator = j !== 0 ? 0 : 1;
    for (let r = 0; r < 8; r++) {
      at(i + r).set((FINDER[offset + r] as number[]).slice(separator, separator + 8), j);
    }
  }
  const positions = ALIGNMENT[version] ?? [];
  const low = positions[0];
  const high = positions[positions.length - 1];
  for (const x of positions) {
    for (const y of positions) {
      if ((x === low && y === low) || (x === low && y === high) || (x === high && y === low)) {
        continue;
      }
      for (let r = 0; r < 5; r++) at(x - 2 + r).set(ALIGNMENT_PATTERN[r] as number[], y - 2);
    }
  }
  return m;
}

/** The bits into the unset modules, in two-module columns from the right, up and down. */
function addCodewords(m: Uint8Array[], bits: readonly number[]): void {
  const size = m.length;
  let idx = 0;
  for (let right = size - 1; right > 0; right -= 2) {
    const column = right <= 6 ? right - 1 : right;
    for (let vertical = 0; vertical < size; vertical++) {
      for (let z = 0; z < 2; z++) {
        const j = column - z;
        const upwards = ((column & 2) === 0) !== j < 6;
        const row = m[upwards ? size - 1 - vertical : vertical] as Uint8Array;
        if (row[j] === UNSET && idx < bits.length) {
          row[j] = bits[idx] as number;
          idx++;
        }
      }
    }
  }
  if (idx !== bits.length) throw new QrError(`placed ${idx} of ${bits.length} bits`);
}

/** The mask with the lowest penalty (the first, on a tie), and the matrix under it. */
function bestMask(matrix: Uint8Array[], version: number): [number, Uint8Array[]] {
  const size = matrix.length;
  const functions = blank(size, version);
  (functions[size - 8] as Uint8Array)[8] = 1;
  let best: [number, Uint8Array[]] | null = null;
  let bestScore = Infinity;
  MASKS.forEach((mask, k) => {
    const m = matrix.map((row) => row.slice());
    for (let i = 0; i < size; i++) {
      for (let j = 0; j < size; j++) {
        if ((functions[i] as Uint8Array)[j] === UNSET && mask(i, j)) {
          (m[i] as Uint8Array)[j] = ((m[i] as Uint8Array)[j] as number) ^ 1;
        }
      }
    }
    const s = penalty(m);
    if (s < bestScore) {
      bestScore = s;
      best = [k, m];
    }
  });
  if (best === null) throw new QrError("no mask");
  return best;
}

/** segno's penalty score: N1 runs, N2 blocks, N3 finder-like patterns, N4 balance. */
function penalty(m: readonly Uint8Array[]): number {
  const size = m.length;
  let n1 = 0;
  let n2 = 0;
  let n3 = 0;
  let dark = 0;
  const column = new Uint8Array(size);
  let lastRow: Uint8Array | null = null;
  for (let i = 0; i < size; i++) {
    const row = m[i] as Uint8Array;
    let rowPrev = -1;
    let colPrev = -1;
    let rowRun = 0;
    let colRun = 0;
    for (let j = 0; j < size; j++) {
      const r = row[j] as number;
      const c = (m[j] as Uint8Array)[i] as number;
      column[j] = c;
      dark += r;
      if (r === rowPrev) {
        rowRun++;
      } else {
        if (rowRun >= 5) n1 += rowRun - 2;
        rowRun = 1;
      }
      if (c === colPrev) {
        colRun++;
      } else {
        if (colRun >= 5) n1 += colRun - 2;
        colRun = 1;
      }
      if (
        lastRow !== null && j > 0 && r === rowPrev && rowPrev === lastRow[j] &&
        lastRow[j] === lastRow[j - 1]
      ) {
        n2 += 3;
      }
      rowPrev = r;
      colPrev = c;
    }
    lastRow = row;
    n3 += patterns(row) + patterns(column);
    if (rowRun >= 5) n1 += rowRun - 2;
    if (colRun >= 5) n1 += colRun - 2;
  }
  const n4 = 10 * Math.trunc(Math.abs((dark / (size * size)) * 100 - 50) / 5);
  return n1 + n2 + n3 + n4;
}

/** N3: each 1:1:3:1:1 pattern at an edge, or with four light modules (as many as there are)
 * on one side. */
function patterns(seq: Uint8Array): number {
  const size = seq.length;
  let count = 0;
  let idx = find(seq, 0);
  while (idx !== -1) {
    let offset = idx + 7;
    if (
      idx === 0 || idx === size - 7 || !anyDark(seq, Math.max(idx - 4, 0), Math.min(idx, size)) ||
      !anyDark(seq, offset, Math.min(offset + 4, size))
    ) {
      count += 40;
    } else {
      offset = idx + 4;
    }
    idx = find(seq, offset);
  }
  return count;
}

function find(seq: Uint8Array, from: number): number {
  for (let i = from; i + N3_PATTERN.length <= seq.length; i++) {
    if (N3_PATTERN.every((v, k) => seq[i + k] === v)) return i;
  }
  return -1;
}

function anyDark(seq: Uint8Array, from: number, to: number): boolean {
  for (let i = from; i < to; i++) if (seq[i] !== 0) return true;
  return false;
}

/** Level M's format information for `mask`, both copies, and the dark module. */
function addFormatInfo(m: Uint8Array[], mask: number): void {
  const size = m.length;
  const info = FORMAT_INFO[mask] as number;
  const row8 = m[8] as Uint8Array;
  let voffset = 0;
  let hoffset = 0;
  for (let i = 0; i < 8; i++) {
    const vbit = (info >>> i) & 1;
    const hbit = (info >>> (14 - i)) & 1;
    if (i === 6) {
      voffset += 1;
      hoffset = 1;
    }
    (m[i + voffset] as Uint8Array)[8] = vbit;
    row8[i + hoffset] = hbit;
    row8[size - 1 - i] = vbit;
    (m[size - 1 - i] as Uint8Array)[8] = hbit;
  }
  (m[size - 8] as Uint8Array)[8] = 1;
}

/** The version information, from version 7: its 18 bits, BCH-coded, in both corners. */
function addVersionInfo(m: Uint8Array[], version: number): void {
  if (version < 7) return;
  let rem = version;
  for (let i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1f25);
  const info = (version << 12) | rem;
  const size = m.length;
  for (let i = 0; i < 6; i++) {
    for (let k = 0; k < 3; k++) {
      const bit = (info >>> (3 * i + k)) & 1;
      (m[size - 11 + k] as Uint8Array)[i] = bit;
      (m[i] as Uint8Array)[size - 11 + k] = bit;
    }
  }
}

/** A QR code as SVG: its dark modules one path, each row's runs a rectangle, inside the
 * four-module light border a reader needs. As the Python reference renders it. */
export function svg(rows: readonly Uint8Array[]): string {
  const size = rows.length + 8;
  const runs: string[] = [];
  rows.forEach((row, y) => {
    let x = 0;
    while (x < row.length) {
      const start = x;
      while (x < row.length && row[x]) x++;
      if (x > start) runs.push(`M${start + 4} ${y + 4}h${x - start}v1h-${x - start}z`);
      x++;
    }
  });
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${size} ${size}" ` +
    `width="${size * 5}" height="${size * 5}" shape-rendering="crispEdges">` +
    `<rect width="${size}" height="${size}" fill="#fff"/>` +
    `<path fill="#000" d="${runs.join("")}"/></svg>`;
}
