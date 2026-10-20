import { useCallback, useEffect, useRef, useState } from 'react';
import type { Stream } from './types.js';
import { usePerpetua } from './usePerpetua.js';
import { classifyStreamError, type StreamFailureKind } from './recovery.js';

export interface UseStreamOptions {
  /**
   * Re-poll the stream from the ledger at this cadence. Default off — the
   * stream is fetched once when the id (or `enabled`) changes.
   */
  refreshIntervalMs?: number;
  /** Set `false` to suspend fetching. */
  enabled?: boolean;
}

export interface UseStreamResult {
  /** The latest stream record, or `null` before the first success. */
  stream: Stream | null;
  loading: boolean;
  /** Last error from a fetch, cleared on the next success. */
  error: Error | null;
  /** Classification of `error`: `archived` needs RestoreFootprint; `retryable` is safe to refresh. */
  errorKind: StreamFailureKind | null;
  /** Force a refetch of the current id. */
  refresh: () => Promise<void>;
}

/**
 * Loads a Perpetua stream by id and keeps it fresh on an optional interval.
 *
 * This is the data-fetching primitive; [`useAccruedBalance`] builds the
 * sub-second balance on top of it. Note the stream is **immutable by
 * guarantee**: `cancellable`/`pausable`/`transferable` are fixed at creation
 * (issue #104), so once loaded the capability flags never change.
 */
export function useStream(streamId: bigint, opts: UseStreamOptions = {}): UseStreamResult {
  const client = usePerpetua();
  const { refreshIntervalMs, enabled = true } = opts;
  const [stream, setStream] = useState<Stream | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<Error | null>(null);
  const disposed = useRef(false);
  const [errorKind, setErrorKind] = useState<StreamFailureKind | null>(null);
  const loadingRef = useRef(true);

  const refresh = useCallback(async (): Promise<void> => {
    if (loadingRef.current) return; // avoid stacking concurrent fetches
    loadingRef.current = true;
    if (!disposed.current) setLoading(true);
    try {
      const next = await client.getStream(streamId);
      if (disposed.current) return;
      setStream(next);
      setError(null);
      setErrorKind(null);
    } catch (err) {
      if (disposed.current) return;
      // Keep the last good stream on failure so skewed/archived reads never corrupt state.
      setError(err instanceof Error ? err : new Error(String(err)));
      setErrorKind(classifyStreamError(err));
    } finally {
      loadingRef.current = false;
      if (!disposed.current) setLoading(false);
    }
  }, [client, streamId]);

  useEffect(() => {
    disposed.current = false;
    loadingRef.current = false;
    setLoading(enabled);
    if (enabled) void refresh();
    return () => {
      disposed.current = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refresh, enabled]);

  useEffect(() => {
    if (!refreshIntervalMs || refreshIntervalMs <= 0) return;
    const timer = setInterval(() => void refresh(), refreshIntervalMs);
    return () => clearInterval(timer);
  }, [refresh, refreshIntervalMs]);

  return { stream, loading, error, errorKind, refresh };
}