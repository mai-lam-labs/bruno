/**
 * What a refusal says (docs/development/design/refusals.md): bruno/refusals.py's templates, as
 * exported (make vectors), so both relays say the same. A template's `{name}`s are its values; a
 * value it doesn't name is an error, as one it names and isn't given.
 */

import templates from "./refusals.json" with { type: "json" };

/** A refusal's template, by name. */
export type Refusal = keyof typeof templates;

const NAMES = /\{(\w+)\}/g;

/** `refusal`'s template with `values`. */
export function message(
  refusal: Refusal,
  values: Readonly<Record<string, string | number | bigint>> = {},
): string {
  const template: string = templates[refusal];
  const named = new Set([...template.matchAll(NAMES)].map((m) => m[1]));
  const given = Object.keys(values);
  if (named.size !== given.length || given.some((name) => !named.has(name))) {
    throw new Error(`${refusal}: names ${[...named].sort()}, given ${given.sort()}`);
  }
  return template.replace(NAMES, (_, name: string) => String(values[name]));
}
