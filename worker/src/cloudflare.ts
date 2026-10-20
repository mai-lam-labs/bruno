/**
 * The parts of the Workers runtime the relay uses, typed here rather than pulled from npm
 * (developers.cloudflare.com/durable-objects/api/).
 */

export interface SqlCursor {
  toArray(): Record<string, unknown>[];
}

export interface SqlStorage {
  exec(query: string, ...bindings: unknown[]): SqlCursor;
}

export interface DurableObjectStorage {
  readonly sql: SqlStorage;
  setAlarm(scheduledTime: number): Promise<void>;
  getAlarm(): Promise<number | null>;
  deleteAlarm(): Promise<void>;
}

export interface DurableObjectState {
  readonly storage: DurableObjectStorage;
  blockConcurrencyWhile<T>(callback: () => Promise<T>): Promise<T>;
}

export interface DurableObjectId {
  toString(): string;
}

export interface DurableObjectStub {
  fetch(request: Request): Promise<Response>;
}

export interface DurableObjectNamespace {
  idFromName(name: string): DurableObjectId;
  get(id: DurableObjectId): DurableObjectStub;
}
