/** A submission taken, what's become of it, and the queue of them; and the JSON it's stored as. */

import type { TransferAuthorization } from "./eip3009.ts";
import { bytes, checksum, type Hex, hex } from "./evm.ts";
import type { Store } from "./store.ts";
import type { Submission } from "./submission.ts";
import { authorizationJson } from "./transactions.ts";
import { entryPointAt } from "./userops.ts";
import {
  ByEntryPoint,
  MeasuredByRunner,
  parseProof,
  type Proof,
  type StoredProof,
} from "./plan.ts";
import { Millis } from "./units.ts";
import { message } from "./refusals.ts";

/** How many of a submission's latest transactions are looked for: past a node lagging a block. */
export const SENT_KEPT = 4;
/** The most of a failed simulation's revert data kept with a rejected submission. */
export const MAX_REVERT_BYTES = 1_024;

export type Status = "pending" | "included" | "expired" | "rejected";

/** Where a submission is, finer than its status: taken but not yet sent; offered to the
 * builders; included, short of finality; its block finalized; rejected or expired. */
export type Stage = "waiting" | "sending" | "confirming" | "done" | "failed";

/** Why a submission was rejected, its status' reason: outbid from a full queue, no longer
 * simulating, or its transaction reverted on-chain (bruno/tracked.py's Rejection). */
export type Rejection = "outbid" | "simulation failed" | "reverted on-chain";

/**
 * A submission taken and pending, and what's become of it so far. Once it finishes, only a
 * Finished record of it is kept: its `status` then says so, to anything still holding it.
 */
export interface Tracked {
  readonly id: Hex;
  readonly submission: Submission;
  /** Its transaction's gas limit and most bytes unsigned, as last planned. */
  gasLimit: number;
  size: number;
  status: Status;
  /** Its latest transactions, each looked for until it's SENT_KEPT sends old. */
  sent: Hex[];
  attempts: number;
  transactionHash: Hex | null;
  blockNumber: number | null;
  revertData: Hex | null;
  /** The hash of the UserOp it sends, if it's one. */
  readonly userOperation: Hex | null;
  /** How the relay knows it pays. */
  readonly proof: Proof;
}

/** What `t` pays in a block of `baseFee`, as its proof has it. */
export function paying(t: Tracked, baseFee: bigint): bigint {
  return t.proof.paying(t.submission, baseFee);
}

/**
 * A submission no longer pending, as much of it as its status needs: what became of it, not
 * what it was. Its call and payment are dropped as it finishes, so what's stored of finished
 * submissions stays small however many there are (bruno/tracked.py's Finished).
 */
export interface Finished {
  readonly id: Hex;
  readonly status: Status;
  readonly attempts: number;
  readonly transactionHash: Hex | null;
  readonly blockNumber: number | null;
  readonly reason: Rejection | null;
  readonly revertData: Hex | null;
  /** When it finished, by the relay's clock: it's forgotten `retention` after. */
  readonly finishedAt: Millis;
  readonly userOperation: Hex | null;
}

/** `tracked` finished, as `status`: what's kept of it. */
export function finish(
  tracked: Tracked,
  status: Status,
  reason: Rejection | null = null,
): Finished {
  tracked.status = status;
  return {
    id: tracked.id,
    status,
    attempts: tracked.attempts,
    transactionHash: tracked.transactionHash,
    blockNumber: tracked.blockNumber,
    reason,
    revertData: tracked.revertData,
    finishedAt: Millis.unix(),
    userOperation: tracked.userOperation,
  };
}

/** Its stage, the finalized block being `finalized` (null: not read, as for one not
 * included). */
export function stage(t: Tracked | Finished, finalized: number | null): Stage {
  if ("submission" in t) return t.sent.length > 0 ? "sending" : "waiting";
  if (t.status !== "included") return "failed";
  return finalized !== null && t.blockNumber !== null && t.blockNumber <= finalized
    ? "done"
    : "confirming";
}

export function finishedToJson(f: Finished): string {
  return JSON.stringify(f);
}

/** A stored submission: pending, with its submission; or finished, without. A finished one the
 * deployed version (v0.4.1) stored whole, its submission in it, is read as finished all the
 * same: such rows last only until they're forgotten, `retentionSeconds` after they finished. */
export function storedFromJson(text: string): Tracked | Finished {
  const raw = JSON.parse(text);
  if (raw.status === "pending") return trackedFromJson(text);
  return {
    id: raw.id,
    status: raw.status,
    attempts: raw.attempts,
    transactionHash: raw.transactionHash,
    blockNumber: raw.blockNumber,
    reason: raw.reason,
    revertData: raw.revertData,
    finishedAt: Millis(raw.finishedAt),
    userOperation: raw.userOperation ?? null,
  };
}

export function trackedToJson(t: Tracked): string {
  const s = t.submission;
  return JSON.stringify({
    ...t,
    proof: t.proof.stored(),
    submission: {
      call: { to: s.call.to, data: hex(s.call.data) },
      authorizations: s.authorizations.map(authorizationJson),
      gas: s.gas,
      token: s.token,
      payee: s.payee,
      amount: s.amount.toString(),
      maxBlockNumber: s.maxBlockNumber,
      transfer: s.transfer === undefined ? undefined : transferToJson(s.transfer),
      deployment: s.deployment === undefined ? undefined : {
        account: s.deployment.account,
        factory: s.deployment.factory,
        factoryData: hex(s.deployment.factoryData),
      },
    },
  });
}

function transferToJson(t: TransferAuthorization): Record<string, string> {
  return {
    payer: t.payer,
    to: t.to,
    value: t.value.toString(),
    validAfter: t.validAfter.toString(),
    validBefore: t.validBefore.toString(),
    nonce: hex(t.nonce),
    signature: hex(t.signature),
  };
}

function transferFromJson(t: Record<string, string | number>): TransferAuthorization {
  return {
    payer: checksum(String(t.payer)),
    to: checksum(String(t.to)),
    value: BigInt(t.value as string),
    validAfter: BigInt(t.validAfter as string),
    validBefore: BigInt(t.validBefore as string),
    nonce: bytes(t.nonce as Hex),
    signature: bytes(t.signature as Hex),
  };
}

/** The proof of a submission 0.5.4 stored, which had none: an op paying by its fees, its fee
 * payment kept, by its EntryPoint's rules; anything else, by its runner. Only while 0.5.4's
 * pending submissions may still be stored, their deadlines minutes away. */
function storedBefore060(raw: {
  readonly submission: { readonly call: { readonly to: string } };
  readonly feePayment?: Extract<StoredProof, { kind: "entryPoint" }>["feePayment"] | null;
}): Proof {
  const fees = raw.feePayment;
  if (fees === undefined || fees === null) return new MeasuredByRunner();
  return new ByEntryPoint(entryPointAt(checksum(raw.submission.call.to)), {
    gas: BigInt(fees.gas),
    maxFeePerGas: BigInt(fees.maxFeePerGas),
    maxPriorityFeePerGas: BigInt(fees.maxPriorityFeePerGas),
  });
}

function trackedFromJson(text: string): Tracked {
  const raw = JSON.parse(text);
  const s = raw.submission;
  return {
    ...raw,
    userOperation: raw.userOperation ?? null,
    proof: raw.proof !== undefined ? parseProof(raw.proof) : storedBefore060(raw),
    submission: {
      call: { to: checksum(s.call.to), data: bytes(s.call.data), value: 0n },
      authorizations: s.authorizations.map((a: Record<string, string>) => ({
        chainId: BigInt(a.chainId as string),
        address: checksum(a.address as string),
        nonce: BigInt(a.nonce as string),
        yParity: Number(a.yParity),
        r: BigInt(a.r as string),
        s: BigInt(a.s as string),
      })),
      gas: s.gas,
      token: checksum(s.token),
      payee: checksum(s.payee),
      amount: BigInt(s.amount),
      maxBlockNumber: s.maxBlockNumber,
      ...(s.transfer === undefined ? {} : { transfer: transferFromJson(s.transfer) }),
      ...(s.deployment === undefined ? {} : {
        deployment: {
          account: checksum(s.deployment.account),
          factory: checksum(s.deployment.factory),
          factoryData: bytes(s.deployment.factoryData),
        },
      }),
    },
  };
}

export class UnknownSubmissionError extends Error {}

/** What a status asked of an unknown id says. */
function unknownSubmission(id: string): string {
  return message("unknown-submission", { id });
}

/**
 * The submissions taken: pending, and finished, each kept `retention` once it is
 * (Finished), all in the Store; the UserOps among them, by hash; and the requests held for one,
 * answered as it finishes. Submissions are taken one at a time (`queued`): a duplicate or a full
 * queue is decided once.
 */
export class Queue {
  private readonly pendingById = new Map<Hex, Tracked>();
  private readonly finished = new Map<Hex, Finished>();
  /** The UserOps sent as submissions, pending or kept finished: each one's hash to its id,
   * rebuilt from what's stored. */
  private readonly operations = new Map<Hex, Hex>();
  /** Each held request's submission: called once it's no longer pending. */
  private readonly outcomes = new Map<Hex, (() => void)[]>();
  private queueing: Promise<unknown> = Promise.resolve();

  constructor(private readonly store: Store, private readonly retention: Millis) {
    for (const value of store.list("submission:")) {
      const stored = storedFromJson(value);
      if ("submission" in stored) {
        this.pendingById.set(stored.id, stored);
      } else {
        this.finished.set(stored.id, stored);
      }
      if (stored.userOperation !== null) this.operations.set(stored.userOperation, stored.id);
    }
  }

  pending(): Tracked[] {
    return [...this.pendingById.values()];
  }

  /** The submission `id`, if it's pending. */
  pendingOf(id: Hex): Tracked | null {
    return this.pendingById.get(id) ?? null;
  }

  /** `work` run once every earlier submission's has finished. */
  queued<T>(work: () => Promise<T>): Promise<T> {
    const run = this.queueing.then(work, work);
    this.queueing = run.catch(() => undefined);
    return run;
  }

  /** `tracked` taken: pending, stored, and found by its UserOp's hash if it sends one. */
  add(tracked: Tracked): void {
    this.pendingById.set(tracked.id, tracked);
    if (tracked.userOperation !== null) this.operations.set(tracked.userOperation, tracked.id);
    this.save(tracked);
  }

  /** `tracked` stored as it is now. */
  save(tracked: Tracked): void {
    this.store.put(`submission:${tracked.id}`, trackedToJson(tracked));
  }

  /** Whether the submission `id` was taken: pending, or finished and still kept. */
  known(id: Hex): boolean {
    return this.pendingById.has(id) || this.finished.has(id);
  }

  /** The id of the submission carrying the UserOp `opHash`, if the relay took it and hasn't
   * forgotten it. */
  operation(opHash: Hex): Hex | null {
    return this.operations.get(opHash) ?? null;
  }

  /** The submission `id`, pending or finished: UnknownSubmissionError if it's neither. */
  status(id: Hex): Tracked | Finished {
    const tracked = this.pendingById.get(id) ?? this.finished.get(id);
    if (tracked === undefined) {
      throw new UnknownSubmissionError(unknownSubmission(id));
    }
    return tracked;
  }

  /** The submission once it's no longer pending: included, rejected or expired. Waiting reads
   * nothing: it's the reconciling the relay does each block that ends it. */
  async outcome(id: Hex): Promise<Finished> {
    if ("submission" in this.status(id)) {
      await new Promise<void>((resolve) => {
        this.outcomes.set(id, [...(this.outcomes.get(id) ?? []), resolve]);
      });
    }
    const finished = this.finished.get(id);
    if (finished === undefined) throw new UnknownSubmissionError(unknownSubmission(id));
    return finished;
  }

  /** A pending x402 submission of the same call by the same payer (its token, call, gas and
   * authorizations), paid by another authorization: a client that timed out and signed again.
   * null if there's none. */
  pendingLike(submission: Submission): Tracked | null {
    const transfer = submission.transfer;
    if (transfer === undefined) return null;
    const key = (s: Submission): string =>
      JSON.stringify([
        s.token,
        s.call.to,
        hex(s.call.data),
        s.gas,
        s.authorizations.map(authorizationJson),
      ]);
    return this.pending().find((t) =>
      t.submission.transfer?.payer === transfer.payer && key(t.submission) === key(submission)
    ) ?? null;
  }

  /** `tracked` finished, as `status`, and stored as only its status needs: a request held for
   * it is answered. */
  finish(tracked: Tracked, status: Status, reason: Rejection | null = null): void {
    const finished = finish(tracked, status, reason);
    this.pendingById.delete(tracked.id);
    this.finished.set(tracked.id, finished);
    this.store.put(`submission:${tracked.id}`, finishedToJson(finished));
    for (const resolve of this.outcomes.get(tracked.id) ?? []) resolve();
    this.outcomes.delete(tracked.id);
  }

  /** Forget the submissions finished more than `retention` ago. */
  prune(): void {
    const cutoff = Millis.unix() - this.retention;
    for (const [id, f] of this.finished) {
      if (f.finishedAt <= cutoff) {
        this.finished.delete(id);
        this.store.delete(`submission:${id}`);
        if (f.userOperation !== null) this.operations.delete(f.userOperation);
      }
    }
  }

  /** When the next finished submission is due to be forgotten: null if none is. */
  forgetAt(): Millis | null {
    let first: Millis | null = null;
    for (const f of this.finished.values()) {
      if (first === null || f.finishedAt < first) first = f.finishedAt;
    }
    return first === null ? null : Millis(first + this.retention);
  }
}
