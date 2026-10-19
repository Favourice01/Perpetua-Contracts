export interface BatchStreamRow {
  recipient: string;
  amount: string;
  durationDays: string;
  cliffDays: string;
}

export interface ParsedBatchStream {
  recipient: string;
  amount: bigint;
  durationDays: number;
  cliffDays: number;
}

export const MAX_BATCH_SIZE = 16;
const SECONDS_PER_DAY = 86_400;
const I128_MAX = (1n << 127n) - 1n;

function validateRow(row: ParsedBatchStream, rowNumber: number): number {
  if (!row.recipient.trim()) {
    throw new Error(`Row ${rowNumber}: recipient must not be empty`);
  }
  if (row.amount <= 0n || row.amount > I128_MAX) {
    throw new Error(`Row ${rowNumber}: amount must be between 1 and i128::MAX`);
  }
  if (!Number.isSafeInteger(row.durationDays) || row.durationDays <= 0) {
    throw new Error(`Row ${rowNumber}: duration_days must be a positive safe integer`);
  }
  if (
    !Number.isSafeInteger(row.cliffDays) ||
    row.cliffDays < 0 ||
    row.cliffDays > row.durationDays
  ) {
    throw new Error(`Row ${rowNumber}: cliff_days must be between 0 and duration_days`);
  }

  const durationSeconds = BigInt(row.durationDays) * BigInt(SECONDS_PER_DAY);
  if (durationSeconds > BigInt(Number.MAX_SAFE_INTEGER)) {
    throw new Error(`Row ${rowNumber}: duration exceeds the safe timestamp range`);
  }
  if (row.amount < durationSeconds) {
    throw new Error(`Row ${rowNumber}: amount is below the minimum per-second deposit rate`);
  }
  if (row.amount * durationSeconds > I128_MAX) {
    throw new Error(`Row ${rowNumber}: amount multiplied by duration exceeds i128::MAX`);
  }
  return Number(durationSeconds);
}

export function parseCsv(input: string): ParsedBatchStream[] {
  const trimmedInput = input.trim();
  const lines = trimmedInput ? trimmedInput.split(/\r?\n/) : [];
  if (lines.length < 2) {
    throw new Error("CSV must contain a header row and at least one data row");
  }
  if (lines.length - 1 > MAX_BATCH_SIZE) {
    throw new Error(`Batch size exceeds MAX_BATCH_SIZE of ${MAX_BATCH_SIZE}; split client-side`);
  }

  const header = lines[0].split(",").map((cell) => cell.trim().toLowerCase());
  const required = ["recipient", "amount", "duration_days", "cliff_days"];
  for (const field of required) {
    if (!header.includes(field)) {
      throw new Error(`Missing required CSV column: ${field}`);
    }
  }
  if (new Set(header).size !== header.length) {
    throw new Error("CSV header contains duplicate columns");
  }

  const recipientIndex = header.indexOf("recipient");
  const amountIndex = header.indexOf("amount");
  const durationIndex = header.indexOf("duration_days");
  const cliffIndex = header.indexOf("cliff_days");

  const results: ParsedBatchStream[] = [];
  for (let i = 1; i < lines.length; i++) {
    const cells = lines[i].split(",");
    if (cells.length !== header.length) {
      throw new Error(`Row ${i + 1} has ${cells.length} columns, expected ${header.length}`);
    }

    const amountText = cells[amountIndex].trim();
    let amount: bigint;
    try {
      amount = BigInt(amountText);
    } catch {
      throw new Error(`Row ${i + 1}: amount must be an integer`);
    }
    const row: ParsedBatchStream = {
      recipient: cells[recipientIndex].trim(),
      amount,
      durationDays: Number(cells[durationIndex].trim()),
      cliffDays: Number(cells[cliffIndex].trim()),
    };
    validateRow(row, i + 1);
    results.push(row);
  }

  return results;
}

export interface BuildBatchCreateOptions {
  sender: string;
  token: string;
  startTime: number;
  cancellable: boolean;
  pausable: boolean;
  transferable: boolean;
}

export interface BatchCreateTransaction {
  source: string;
  operations: Array<{
    createStream: {
      sender: string;
      recipient: string;
      token: string;
      deposit: bigint;
      startTime: number;
      endTime: number;
      cliffTime: number;
      cancellable: boolean;
      pausable: boolean;
      transferable: boolean;
    };
  }>;
}

export function buildBatchCreateTransaction(
  rows: ParsedBatchStream[],
  options: BuildBatchCreateOptions,
): BatchCreateTransaction {
  if (rows.length === 0) {
    throw new Error("No rows to build into a transaction");
  }
  if (rows.length > MAX_BATCH_SIZE) {
    throw new Error(`Batch size exceeds MAX_BATCH_SIZE of ${MAX_BATCH_SIZE}; split client-side`);
  }
  if (!Number.isSafeInteger(options.startTime) || options.startTime < 0) {
    throw new Error("startTime must be a non-negative safe integer");
  }

  const operations: BatchCreateTransaction["operations"] = new Array(rows.length);
  for (let i = 0; i < rows.length; i++) {
    const row = rows[i];
    const durationSeconds = validateRow(row, i + 1);
    const cliffSeconds = row.cliffDays * SECONDS_PER_DAY;
    const endTime = options.startTime + durationSeconds;
    const cliffTime = options.startTime + cliffSeconds;
    if (!Number.isSafeInteger(endTime) || !Number.isSafeInteger(cliffTime)) {
      throw new Error(`Row ${i + 1}: schedule exceeds the safe timestamp range`);
    }

    operations[i] = { createStream: {
      sender: options.sender,
      recipient: row.recipient,
      token: options.token,
      deposit: row.amount,
      startTime: options.startTime,
      endTime,
      cliffTime,
      cancellable: options.cancellable,
      pausable: options.pausable,
      transferable: options.transferable,
    } };
  }

  return {
    source: options.sender,
    operations,
  };
}
