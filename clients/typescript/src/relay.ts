/** The relay's HTTP API on one chain, and the error every refusal of it is. */

import { type Refusal, REFUSALS } from "./refusals.ts";
import type { ErrorData, Hex } from "./types.ts";

/**
 * A refusal, by the relay or by the client: the HTTP `status` and `body` the relay answered (0 and
 * the client's own for one of the client's), and the relay's error in it, if it's one: its
 * JSON-RPC `code`, and its `data`, whose `reason` is which refusal it is. Tell refusals apart by
 * `reason`, never by the message.
 */
export class BrunoError extends Error {
  readonly status: number;
  readonly body: unknown;
  readonly code: number | null;
  readonly data: ErrorData | null;

  constructor(message: string, status: number, body: unknown) {
    super(message);
    this.status = status;
    this.body = body;
    const error = (body as { error?: { code?: unknown; data?: unknown } } | null)?.error;
    this.code = typeof error?.code === "number" ? error.code : null;
    this.data = errorData(error?.data);
  }

  /** Which refusal it is, if the relay said: null for the client's own, or a relay's error
   * without one (JSON-RPC's own, an internal error). */
  get reason(): Refusal | null {
    return this.data?.reason ?? null;
  }
}

/** `data` as a relay error's, if it names a refusal this client knows. */
function errorData(data: unknown): ErrorData | null {
  const reason = (data as { reason?: unknown } | null | undefined)?.reason;
  const known: readonly string[] = REFUSALS;
  return typeof reason === "string" && known.includes(reason) ? data as ErrorData : null;
}

/** The relay's HTTP API, on one chain. */
export class Relay {
  readonly url: string;
  readonly path: string;
  readonly #fetcher: typeof fetch;

  constructor(url: string, path: string, fetcher: typeof fetch) {
    this.url = url;
    this.path = path;
    this.#fetcher = fetcher;
  }

  async json(method: string, path: string, body?: unknown): Promise<unknown> {
    const response = await this.send(method, path, body);
    const parsed = await read(response);
    if (!response.ok) {
      throw new BrunoError(`${method} ${path}: ${response.status}`, response.status, parsed);
    }
    return parsed;
  }

  send(
    method: string,
    path: string,
    body?: unknown,
    headers: Record<string, string> = {},
    timeoutMs = 30_000,
  ): Promise<Response> {
    return this.#fetcher(`${this.url}/${this.path}${path}`, {
      method,
      headers: body === undefined ? headers : { "content-type": "application/json", ...headers },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      signal: AbortSignal.timeout(timeoutMs),
    });
  }
}

/** A response's body: its JSON, else its text. */
export async function read(response: Response): Promise<unknown> {
  const text = await response.text();
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

/** `n` as a JSON-RPC quantity. */
export function hex(n: bigint | number): Hex {
  return `0x${BigInt(n).toString(16)}`;
}
