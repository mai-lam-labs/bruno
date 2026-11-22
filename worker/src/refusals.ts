/**
 * What a refusal is (docs/development/design/refusals.md): bruno/refusals.py's, as exported (make
 * vectors), so both relays say the same: its name, the `reason` every error carries, who can act
 * on it, and its template; and each wording's, text that's never a reason. A template's
 * `{name}`s are its values; a value it doesn't name is an error, as one it names and isn't given.
 */

import exported from "./refusals.json" with { type: "json" };

const { refusals, wordings } = exported;

/** Who can act on a refusal: the client, trying again later (`retry`) or asking for something
 * else (`request`); or only the relay's operator (`operator`) (bruno/refusals.py's Category). */
export type Category = "retry" | "request" | "operator";

/** Each refusal, by name: an error's `reason`. */
export type Refusal = keyof typeof refusals;

/** Message text that's never an error's reason: a decoder's wording of what's malformed, a
 * category's next step, a failed simulation's frame, x402's own descriptions (bruno/refusals.py's
 * Wording). */
export type Wording = keyof typeof wordings;

/** A template's values. */
export type Values = Readonly<Record<string, string | number | bigint>>;

const NAMES = /\{(\w+)\}/g;

/** Who can act on `refusal` (bruno/refusals.py's Refusal.category). */
export function categoryOf(refusal: Refusal): Category {
  return refusals[refusal].category as Category;
}

/** A refusal as an error carries it: which, and the values its template names. Its text is the
 * template with them (toString). */
export class Refused {
  constructor(readonly refusal: Refusal, readonly values: Values = {}) {
    message(refusal, values);
  }

  toString(): string {
    return message(this.refusal, this.values);
  }
}

/** `refusal` with `values`: an Error, as message throws it, if they aren't its template's. */
export function refused(refusal: Refusal, values: Values = {}): Refused {
  return new Refused(refusal, values);
}

/** `refusal`'s template (or a wording's) with `values`. */
export function message(refusal: Refusal | Wording, values: Values = {}): string {
  const template: string = refusal in refusals
    ? refusals[refusal as Refusal].template
    : wordings[refusal as Wording].template;
  const named = new Set([...template.matchAll(NAMES)].map((m) => m[1]));
  const given = Object.keys(values);
  if (named.size !== given.length || given.some((name) => !named.has(name))) {
    throw new Error(`${refusal}: names ${[...named].sort()}, given ${given.sort()}`);
  }
  return template.replace(NAMES, (_, name: string) => String(values[name]));
}
