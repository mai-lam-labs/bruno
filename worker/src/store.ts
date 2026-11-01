/**
 * Where the relay keeps its state, so it outlives the object: the Durable Object's storage. The
 * Python relay keeps its in memory, so has no counterpart.
 */

/** Where the relay keeps its state: synchronous, as a Durable Object's SQLite storage is. */
export interface Store {
  get(key: string): string | undefined;
  put(key: string, value: string): void;
  delete(key: string): void;
  list(prefix: string): string[];
}
