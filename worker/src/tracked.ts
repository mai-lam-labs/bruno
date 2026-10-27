/** A submission taken, and what's become of it; and the JSON it's stored as. */

import type { TransferAuthorization } from "./eip3009.ts";
import { bytes, checksum, type Hex, hex } from "./evm.ts";
import type { Submission } from "./submission.ts";
import { authorizationJson, bytesOf } from "./transactions.ts";
import { concatBytes } from "@noble/hashes/utils.js";

/** How many of a submission's latest transactions are looked for: past a node lagging a block. */
export const SENT_KEPT = 4;
/** The most of a failed simulation's revert data kept with a rejected submission. */
export const MAX_REVERT_BYTES = 1_024;

export type Status = "pending" | "included" | "expired" | "rejected";

/** Where a submission is, finer than its status: taken but not yet sent; offered to the
 * builders; included, short of finality; its block finalized; rejected or expired. */
export type Stage = "waiting" | "sending" | "confirming" | "done" | "failed";

/** A submission taken, and what's become of it. */
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

/** Its stage, the finalized block being `finalized` (null: not read, as for one not
 * included). */
export function stage(tracked: Tracked, finalized: number | null): Stage {
  switch (tracked.status) {
    case "pending":
      return tracked.sent.length > 0 ? "sending" : "waiting";
    case "included":
      return finalized !== null && tracked.blockNumber !== null && tracked.blockNumber <= finalized
        ? "done"
        : "confirming";
    case "expired":
    case "rejected":
      return "failed";
  }
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
    // stored before signatures were kept whole: a key's, r ‖ s ‖ v
    signature: t.signature === undefined
      ? concatBytes(
        bytesOf(BigInt(t.r as string), 32),
        bytesOf(BigInt(t.s as string), 32),
        Uint8Array.of(Number(t.v)),
      )
      : bytes(t.signature as Hex),
  };
}

export function trackedFromJson(text: string): Tracked {
  const raw = JSON.parse(text);
  const s = raw.submission;
  return {
    ...raw,
    // a submission stored before sizes were kept: re-planned, its size with it, before it's
    // priced, and none was stored on a chain that charges beyond gas
    size: raw.size ?? 0,
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
