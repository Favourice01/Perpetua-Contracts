/**
 * Pure recovery helpers for `useStream`: archival detection, RPC ledger-skew
 * guarding and error classification. See docs/soroban-rpc-read-skew.md.
 */

export type StreamFailureKind = 'archived' | 'retryable' | 'fatal';

export class StreamArchivedError extends Error {
  readonly kind = 'archived' as const;
  constructor(readonly streamId: bigint) {
    super(
      `Stream ${streamId} is archived: submit a RestoreFootprint transaction for DataKey::Stream(${streamId}) and refresh.`,
    );
    this.name = 'StreamArchivedError';
  }
}

/** Archived = not live, but the id is below the monotonic stream count. */
export function isArchived(streamExists: boolean, streamCount: bigint, streamId: bigint): boolean {
  return streamExists === false && streamId < streamCount;
}

export function classifyStreamError(err: unknown): StreamFailureKind {
  if (err instanceof StreamArchivedError) return 'archived';
  const m = (err instanceof Error ? err.message : String(err)).toLowerCase();
  if (m.includes('archived') || m.includes('restore')) return 'archived';
  if (m.includes('try_again_later') || m.includes('timeout') || m.includes('timed out') || m.includes('network')) {
    return 'retryable';
  }
  return 'fatal';
}

/** True when a read comes from a ledger older than one already applied. */
export function isStaleRead(readLedger: number | undefined, appliedLedger: number): boolean {
  return readLedger !== undefined && readLedger < appliedLedger;
}
