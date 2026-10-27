/** The relay's HTTP API on one chain, and the error every refusal of it is. */

import type { Hex } from "./types.ts";

export class BrunoError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(message: string, status: number, body: unknown) {
    super(message);
    this.status = status;
    this.body = body;
  }
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
