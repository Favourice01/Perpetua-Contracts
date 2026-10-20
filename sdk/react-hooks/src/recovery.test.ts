import { describe, expect, it } from 'vitest';
import { StreamArchivedError, classifyStreamError, isArchived, isStaleRead } from './recovery.js';

describe('recovery', () => {
  it('detects archival', () => {
    expect(isArchived(false, 5n, 2n)).toBe(true);
    expect(isArchived(false, 2n, 2n)).toBe(false);
    expect(isArchived(true, 5n, 2n)).toBe(false);
  });
  it('rejects stale ledger reads', () => {
    expect(isStaleRead(9, 10)).toBe(true);
    expect(isStaleRead(10, 10)).toBe(false);
    expect(isStaleRead(undefined, 10)).toBe(false);
  });
  it('classifies errors', () => {
    expect(classifyStreamError(new StreamArchivedError(1n))).toBe('archived');
    expect(classifyStreamError(new Error('TRY_AGAIN_LATER'))).toBe('retryable');
    expect(classifyStreamError(new Error('boom'))).toBe('fatal');
  });
});
