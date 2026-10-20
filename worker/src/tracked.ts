/** A submission taken, and what's become of it; and the JSON it's stored as. */

import { bytes, checksum, type Hex, hex } from "./evm.ts";
import type { Submission } from "./submission.ts";
import { authorizationJson } from "./transactions.ts";

/** How many of a submission's latest transactions are looked for: past a node lagging a block. */
export const SENT_KEPT = 4;
/** The most of a failed simulation's revert data kept with a rejected submission. */
export const MAX_REVERT_BYTES = 1_024;

export type Status = "pending" | "included" | "expired" | "rejected";

/** A submission taken, and what's become of it. */
export interface Tracked {
  readonly id: Hex;
  readonly submission: Submission;
  gasLimit: number;
  status: Status;
  /** Its latest transactions, each looked for until it's SENT_KEPT sends old. */
  sent: Hex[];
  attempts: number;
  transactionHash: Hex | null;
  blockNumber: number | null;
  reason: string | null;
  revertData: Hex | null;
  /** When it stopped being pending (Date.now()): it's forgotten `retentionSeconds` after. */
  finishedAt: number | null;
}

export function finish(tracked: Tracked, status: Status, reason: string | null = null): void {
  tracked.status = status;
  tracked.reason = reason;
  tracked.finishedAt = Date.now();
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
    },
  });
}

export function trackedFromJson(text: string): Tracked {
  const raw = JSON.parse(text);
  const s = raw.submission;
  return {
    ...raw,
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
    },
  };
}
