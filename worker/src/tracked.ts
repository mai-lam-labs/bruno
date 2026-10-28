/** A submission taken, and what's become of it; and the JSON it's stored as. */

import type { TransferAuthorization } from "./eip3009.ts";
import { bytes, checksum, type Hex, hex } from "./evm.ts";
import type { Submission } from "./submission.ts";
import { authorizationJson } from "./transactions.ts";

/** How many of a submission's latest transactions are looked for: past a node lagging a block. */
export const SENT_KEPT = 4;
/** The most of a failed simulation's revert data kept with a rejected submission. */
export const MAX_REVERT_BYTES = 1_024;

export type Status = "pending" | "included" | "expired" | "rejected";

/** Where a submission is, finer than its status: taken but not yet sent; offered to the
 * builders; included, short of finality; its block finalized; rejected or expired. */
export type Stage = "waiting" | "sending" | "confirming" | "done" | "failed";

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
  readonly reason: string | null;
  readonly revertData: Hex | null;
  /** When it finished (Date.now()): it's forgotten `retentionSeconds` after. */
  readonly finishedAt: number;
  readonly userOperation: Hex | null;
}

/** `tracked` finished, as `status`: what's kept of it. */
export function finish(tracked: Tracked, status: Status, reason: string | null = null): Finished {
  tracked.status = status;
  return {
    id: tracked.id,
    status,
    attempts: tracked.attempts,
    transactionHash: tracked.transactionHash,
    blockNumber: tracked.blockNumber,
    reason,
    revertData: tracked.revertData,
    finishedAt: Date.now(),
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
    finishedAt: raw.finishedAt,
    userOperation: raw.userOperation ?? null,
  };
}

export function trackedToJson(t: Tracked): string {
  const s = t.submission;
  return JSON.stringify({
    ...t,
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

function trackedFromJson(text: string): Tracked {
  const raw = JSON.parse(text);
  const s = raw.submission;
  return {
    ...raw,
    userOperation: raw.userOperation ?? null,
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
