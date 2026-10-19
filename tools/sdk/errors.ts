/**
 * Perpetua Stream contract error code to human-readable message parser.
 *
 * Maps raw Soroban contract error discriminants (integers) to stable,
 * actionable messages.  Error codes are the `#[repr(u32)]` discriminants
 * from `contracts/stream/src/error.rs` and are part of the public ABI.
 *
 * @module errors
 */

/** Human-readable message for a known Perpetua stream error code. */
export type StreamErrorCode =
  | 1  // StreamNotFound
  | 2  // InvalidTimeRange
  | 3  // InvalidCliff
  | 4  // InvalidDeposit
  | 5  // DepositRateTooLow
  | 6  // SelfStream
  | 7  // Unauthorized
  | 8  // NotCancellable
  | 9  // NotPausable
  | 10 // NotTransferable
  | 11 // StreamNotActive
  | 12 // StreamNotPaused
  | 13 // StreamAlreadyPaused
  | 14 // StreamTerminated
  | 15 // StreamMatured
  | 16 // InsufficientWithdrawable
  | 17 // NothingToWithdraw
  | 18 // InvalidAmount
  | 19 // BatchTooLarge
  | 20 // EmptyBatch
  | 21 // DuplicateStreamId
  | 22 // Overflow
  | 23 // TopUpTooSmall
  | 24 // StreamIdExhausted
  | 25 // TokenTransferFailed
  | 26 // TokenMissing
  | 27 // DelegateNotPermitted
  | 28 // DelegateExpired
  | 29 // MalformedStreamId
  | 30 // RepeatedTransfer
  | 31 // InvalidTopUp
  | 32 // DepositTooSmall
  | 33 // StreamIdOverflow
  | 34 // BatchSizeExceeded;

/** Mapping from error discriminant to human-readable message. */
const ERROR_MESSAGES: Record<number, string> = {
  1:  "StreamNotFound: no stream exists with the given id.",
  2:  "InvalidTimeRange: end_time must be greater than start_time.",
  3:  "InvalidCliff: cliff_time must be within [start_time, end_time].",
  4:  "InvalidDeposit: deposit must be a positive amount.",
  5:  "DepositRateTooLow: deposit is smaller than the stream duration in seconds.",
  6:  "SelfStream: sender and recipient cannot be the same address.",
  7:  "Unauthorized: caller is not authorized for this action.",
  8:  "NotCancellable: stream was created with cancellable=false.",
  9:  "NotPausable: stream was created with pausable=false.",
  10: "NotTransferable: stream was created with transferable=false.",
  11: "StreamNotActive: action requires an Active stream.",
  12: "StreamNotPaused: stream is not currently paused.",
  13: "StreamAlreadyPaused: stream is already paused.",
  14: "StreamTerminated: stream is cancelled or depleted.",
  15: "StreamMatured: stream accrual clock has reached end_time.",
  16: "InsufficientWithdrawable: requested amount exceeds withdrawable balance.",
  17: "NothingToWithdraw: stream has no currently withdrawable funds.",
  18: "InvalidAmount: amount must be a positive integer.",
  19: "BatchTooLarge: batch size exceeds the maximum allowed.",
  20: "EmptyBatch: no stream ids were provided.",
  21: "DuplicateStreamId: the same stream id appears more than once.",
  22: "Overflow: a checked arithmetic operation overflowed.",
  23: "TopUpTooSmall: top_up amount is too small to extend the schedule.",
  24: "StreamIdExhausted: no further stream ids can be issued.",
  25: "TokenTransferFailed: the token contract rejected the transfer.",
  26: "TokenMissing: the token address has no deployed contract.",
  27: "DelegateNotPermitted: delegate grant does not cover this operation.",
  28: "DelegateExpired: delegate grant has passed its expiry timestamp.",
  29: "MalformedStreamId: a batch element is not a valid stream id.",
  30: "RepeatedTransfer: transfer recipient is the same as the current recipient.",
  31: "InvalidTopUp: top_up amount must be a positive integer.",
  32: "DepositTooSmall: deposit is smaller than the stream duration in seconds.",
  33: "StreamIdOverflow: the next stream id would overflow u64.",
  34: "BatchSizeExceeded: batch size exceeds the maximum allowed.",
};

/**
 * Parse a raw Perpetua stream contract error code into a human-readable
 * message.
 *
 * When a Soroban transaction fails the RPC returns `Error(Contract, #N)`.
 * This helper maps `N` to a stable string like `StreamIsPaused` so frontend
 * applications can display meaningful messages without hardcoding the ABI
 * table.
 *
 * @param errorCode - Raw integer error discriminant from the RPC response.
 * @returns Human-readable error description, or a fallback for unknown codes.
 *
 * @example
 * ```typescript
 * parseStreamError(14); // "StreamTerminated: stream is cancelled or depleted."
 * parseStreamError(99); // "Unknown stream error (code 99)."
 * ```
 */
export function parseStreamError(errorCode: number): string {
  if (Number.isInteger(errorCode) && errorCode > 0 && ERROR_MESSAGES[errorCode]) {
    return ERROR_MESSAGES[errorCode];
  }
  return `Unknown stream error (code ${errorCode}).`;
}

/**
 * Check whether an error code is a known Perpetua stream error.
 *
 * @param errorCode - Raw integer error discriminant.
 * @returns `true` if the code is in the Perpetua stream error table.
 */
export function isKnownStreamError(errorCode: number): boolean {
  return Number.isInteger(errorCode) && errorCode > 0 && Boolean(ERROR_MESSAGES[errorCode]);
}
