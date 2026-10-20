import { parse, buildBatchCreateTransaction, type BatchCreateTransaction, type ParsedBatchStream } from "./sdk/batch_create.ts";

export type CliCommand = "create" | "withdraw" | "inspect" | "extend";

export interface CliContext {
  contractId: string;
  rpcUrl: string;
  networkPassphrase: string;
  sourceSecret?: string;
}

export interface CreateCliInput {
  recipient: string;
  amount: string;
  durationDays: number;
  cliffDays?: number;
  startTime?: number;
  token?: string;
  cancellable?: boolean;
  pausable?: boolean;
  transferable?: boolean;
}

export interface WithdrawCliInput {
  streamId: number;
  amount?: string;
}

export interface InspectCliInput {
  streamId: number;
}

export interface ExtendCliInput {
  streamId: number;
  ttlLedgers?: number;
}

export interface PerpetuaCliResult {
  ok: boolean;
  output: string;
  error?: string;
}

export function buildCreateOperation(input: CreateCliInput) {
  const startTime = input.startTime ?? Math.floor(Date.now() / 1000);
  const cliffTime = startTime + (input.cliffDays ?? 0) * 86_400;
  const endTime = startTime + input.durationDays * 86_400;

  return {
    createStream: {
      sender: "",
      recipient: input.recipient,
      token: input.token ?? "",
      deposit: BigInt(input.amount),
      startTime,
      endTime,
      cliffTime,
      cancellable: input.cancellable ?? true,
      pausable: input.pausable ?? true,
      transferable: input.transferable ?? true,
    },
  };
}

export function buildWithdrawOperation(input: WithdrawCliInput) {
  return {
    withdraw: {
      streamId: input.streamId,
      amount: input.amount ? BigInt(input.amount) : undefined,
    },
  };
}

export function buildInspectQuery(input: InspectCliInput) {
  return {
    inspect: {
      streamId: input.streamId,
    },
  };
}

export function buildExtendOperation(input: ExtendCliInput) {
  return {
    extend: {
      streamId: input.streamId,
      ttlLedgers: input.ttlLedgers,
    },
  };
}

export function buildBatchCreateFromCsv(
  csv: string,
  sender: string,
  token: string,
  startTime: number,
): BatchCreateTransaction {
  const rows = parse(csv);
  return buildBatchCreateTransaction(rows, {
    sender,
    token,
    startTime,
    cancellable: true,
    pausable: true,
    transferable: true,
  });
}

export function formatResult(result: PerpetuaCliResult): string {
  if (!result.ok) {
    return `Error: ${result.error}`;
  }
  return result.output;
}

// ---------------------------------------------------------------------------
// Event/state schema parity (docs/ABI.md) and fault recovery
// ---------------------------------------------------------------------------

/** Frozen topic layouts for the events the CLI produces or inspects. */
export const CLI_EVENT_TOPIC_LAYOUTS: Readonly<Record<string, readonly string[]>> = Object.freeze({
  withdrawn: ["symbol:withdrawn", "u64:stream_id"],
});

/** Frozen payload layouts matching tests/fixtures/event_snapshots/. */
export const CLI_EVENT_PAYLOAD_SCHEMAS: Readonly<Record<string, readonly string[]>> = Object.freeze({
  withdrawn: ["i128:amount", "i128:remaining_balance"],
});

export interface RecoveryGuidance {
  kind: "archived" | "ledger_skew" | "retryable" | "fatal";
  action: string;
}

/** Archived = not live but id was allocated (see restore_footprint.ts). */
export function isArchivedStream(streamExists: boolean, streamCount: bigint, streamId: number): boolean {
  return streamExists === false && BigInt(streamId) < streamCount;
}

/**
 * Reads served from a node behind the ledger we already observed must be
 * rejected so a lagging RPC never overwrites newer state.
 */
export function isLedgerSkewed(observedLedger: number, minLedger: number): boolean {
  return observedLedger < minLedger;
}

/** Deterministically classify a failure into operator guidance. */
export function classifyFailure(message: string): RecoveryGuidance {
  const m = message.toLowerCase();
  if (m.includes("archived") || m.includes("restore") || m.includes("entry expired")) {
    return {
      kind: "archived",
      action: "Submit a RestoreFootprint transaction for the stream key, then retry the command.",
    };
  }
  if (m.includes("skew") || m.includes("stale ledger")) {
    return { kind: "ledger_skew", action: "Re-read at a ledger >= the last observed ledger, then retry." };
  }
  if (m.includes("try_again_later") || m.includes("timeout") || m.includes("timed out")) {
    return { kind: "retryable", action: "Retry with the same inputs; the operation is idempotent." };
  }
  return { kind: "fatal", action: "Do not retry; escalate to the on-call engineer." };
}

/**
 * Retry a failed/timed-out invocation. `isDone` is checked before every attempt
 * so an invocation that actually landed is never submitted twice.
 */
export async function retryIdempotent<T>(
  attempt: () => Promise<T>,
  isDone: () => Promise<T | undefined>,
  maxAttempts = 3,
): Promise<T> {
  let lastError: unknown;
  for (let i = 0; i < maxAttempts; i++) {
    const done = await isDone();
    if (done !== undefined) return done;
    try {
      return await attempt();
    } catch (err) {
      lastError = err;
      if (classifyFailure(err instanceof Error ? err.message : String(err)).kind === "fatal") throw err;
    }
  }
  throw lastError;
}
