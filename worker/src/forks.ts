/**
 * Gas schedules, per fork, for what the relay emits and sends; and which fork each network
 * runs when. The figures and their sources are the Python reference's (bruno/forks.py).
 */

/** Calldata's per byte, both forks (EIP-2028). */
const ZERO_BYTE_GAS = 4;
const NONZERO_BYTE_GAS = 16;
/** Initcode's per word (EIP-3860), both forks. */
const INITCODE_WORD_GAS = 2;

export interface GasSchedule {
  readonly name: string;
  /** In-EVM: an account's first access, and CALL's charge for sending value. */
  readonly coldAccountAccess: number;
  readonly callValue: number;
  /** Sending value to an empty account creates it: from a CALL, and from a transaction. */
  readonly newAccount: number;
  readonly txNewAccount: number;
  /** CREATE2, or a creation transaction, to an address with nothing at it. */
  readonly create: number;
  /** A transaction's base, its recipient's access, sending value, and the floor's per byte. */
  readonly txBase: number;
  readonly txRecipient: number;
  readonly txValue: number;
  readonly floorZeroByte: number;
  readonly floorNonzeroByte: number;
  /** An EIP-7702 authorization's intrinsic, a new delegation's, an empty authority's. */
  readonly authorization: number;
  readonly newDelegation: number;
  readonly emptyAuthority: number;
}

export interface IntrinsicOptions {
  readonly value?: boolean;
  readonly toSelf?: boolean;
  readonly creation?: boolean;
  readonly authorizations?: number;
}

/**
 * A transaction's intrinsic gas, and the floor on what it's charged (EIP-7623, EIP-7976): its
 * gas limit must cover both, the first with its execution added. For a creation, `calldata` is
 * the initcode.
 */
export function intrinsic(
  schedule: GasSchedule,
  calldata: Uint8Array,
  options: IntrinsicOptions = {},
): { standard: number; floor: number } {
  const zeros = calldata.reduce((n, b) => n + (b === 0 ? 1 : 0), 0);
  const nonzeros = calldata.length - zeros;
  let base = schedule.txBase;
  if (!options.creation && !options.toSelf) {
    base += schedule.txRecipient + (options.value ? schedule.txValue : 0);
  }
  let standard = base + ZERO_BYTE_GAS * zeros + NONZERO_BYTE_GAS * nonzeros;
  standard += schedule.authorization * (options.authorizations ?? 0);
  if (options.creation) {
    standard += schedule.create + INITCODE_WORD_GAS * Math.floor((calldata.length + 31) / 32);
  }
  const floor = base + schedule.floorZeroByte * zeros + schedule.floorNonzeroByte * nonzeros;
  return { standard, floor };
}

/** A plain transfer of value to another account: the gas it uses, exactly. */
export function transfer(schedule: GasSchedule, newRecipient: boolean): number {
  const { standard, floor } = intrinsic(schedule, new Uint8Array(), { value: true });
  return Math.max(standard, floor) + (newRecipient ? schedule.txNewAccount : 0);
}

/** What `authorizations` cost in execution, at most, `emptyAuthorities` of them empty. */
export function authorizationsGas(
  schedule: GasSchedule,
  emptyAuthorities: number,
  authorizations: number,
): number {
  return schedule.newDelegation * authorizations + schedule.emptyAuthority * emptyAuthorities;
}

export const PRAGUE: GasSchedule = {
  name: "prague",
  coldAccountAccess: 2_600,
  callValue: 9_000,
  newAccount: 25_000,
  txNewAccount: 0,
  create: 32_000,
  txBase: 21_000,
  txRecipient: 0,
  txValue: 0,
  floorZeroByte: 10,
  floorNonzeroByte: 40,
  authorization: 25_000,
  newDelegation: 0,
  emptyAuthority: 0,
};

export const AMSTERDAM: GasSchedule = {
  name: "amsterdam",
  coldAccountAccess: 3_000,
  callValue: 11_300,
  newAccount: 183_600,
  txNewAccount: 183_600,
  create: 12_000 + 183_600,
  txBase: 12_000,
  txRecipient: 3_000,
  txValue: 6_000,
  floorZeroByte: 64,
  floorNonzeroByte: 64,
  authorization: 7_816,
  newDelegation: 9_000 + 35_190,
  emptyAuthority: 183_600,
};

export const SCHEDULES: Readonly<Record<string, GasSchedule>> = {
  prague: PRAGUE,
  amsterdam: AMSTERDAM,
};

/** A network's forks that change the schedule: from a timestamp, in order. */
export type Forks = readonly (readonly [number, GasSchedule])[];

export const NETWORK_FORKS: Readonly<Record<number, Forks>> = {
  1: [[0, PRAGUE]],
  11_155_111: [[0, PRAGUE], [1_791_294_816, AMSTERDAM]],
  560_048: [[0, PRAGUE], [1_793_036_568, AMSTERDAM]],
};

/** The networks' names, each taking its chain id's place in the relay's URLs; anvil's, for local
 * runs, as well. */
export const NETWORK_NAMES: Readonly<Record<number, string>> = {
  1: "ethereum",
  11_155_111: "sepolia",
  560_048: "hoodi",
  31_337: "anvil",
};

/** The schedule of the last of `forks` active at `timestamp`. */
export function scheduleAt(forks: Forks, timestamp: number): GasSchedule {
  const active = forks.filter(([start]) => start <= timestamp);
  const last = active[active.length - 1];
  if (last === undefined) {
    throw new Error(`no fork active at ${timestamp}`);
  }
  return last[1];
}
