/**
 * Units of time as types, so a number of seconds can't be mistaken for milliseconds: a field or
 * constant says its unit by its type, not its name, and turning one unit into another is spelled
 * out where it happens (`millisOf(Seconds(2))`). As bruno/units.py's.
 *
 * They're whole numbers, so arithmetic on them gives a plain number, wrapped again where the
 * result is a duration or an instant (`Millis(a + b)`). A sub-second interval is written in
 * Millis, and so is a configured number of seconds that may be fractional (`Millis.ofSeconds`).
 */

export type Seconds = number & { readonly __seconds: unique symbol };
export type Millis = number & { readonly __millis: unique symbol };

function whole(unit: string, value: number): number {
  if (!Number.isSafeInteger(value)) {
    throw new TypeError(`${unit}: expected an integer, got ${value}`);
  }
  if (value < 0) throw new RangeError(`${unit}: must be non-negative, got ${value}`);
  return value;
}

/** Seconds. */
export function Seconds(value: number): Seconds {
  return whole("Seconds", value) as Seconds;
}

/** Milliseconds: the relay's clock, and its intervals. */
export function Millis(value: number): Millis {
  return whole("Millis", value) as Millis;
}

/** A number of seconds, perhaps fractional (a configured interval), to the millisecond. */
Millis.ofSeconds = (seconds: number): Millis => Millis(Math.round(seconds * 1000));

/** The wall clock, since the epoch: the relay's clock, which outlives the object (Python's is
 * monotonic, in memory), and what a block's timestamp is compared with. */
Millis.unix = (): Millis => Millis(Date.now());

/** `seconds` in milliseconds (Python's `.millis`): what Date.now(), alarms and timeouts take. */
export function millisOf(seconds: Seconds): Millis {
  return Millis(seconds * 1000);
}
