import { describe, expect, it } from "vitest";
import {
  buildBatchCreateTransaction,
  MAX_BATCH_SIZE,
  parseCsv,
  type ParsedBatchStream,
} from "./batch_create.ts";

const HEADER = "recipient,amount,duration_days,cliff_days";
const OPTIONS = {
  sender: "sender",
  token: "token",
  startTime: 1_700_000_000,
  cancellable: true,
  pausable: true,
  transferable: true,
};

function csvRow(recipient = "recipient", amount = "86400", duration = "1", cliff = "0") {
  return `${recipient},${amount},${duration},${cliff}`;
}

function parsedRow(overrides: Partial<ParsedBatchStream> = {}): ParsedBatchStream {
  return {
    recipient: "recipient",
    amount: 86_400n,
    durationDays: 1,
    cliffDays: 0,
    ...overrides,
  };
}

describe("batch stream creation", () => {
  it("parses reordered columns and preserves a zero-day cliff", () => {
    const rows = parseCsv(
      "cliff_days,duration_days,amount,recipient\n0,1,86400,recipient",
    );

    expect(rows).toEqual([parsedRow()]);
    expect(buildBatchCreateTransaction(rows, OPTIONS).operations[0].createStream).toMatchObject({
      startTime: OPTIONS.startTime,
      cliffTime: OPTIONS.startTime,
      endTime: OPTIONS.startTime + 86_400,
    });
  });

  it("accepts an exact i128 deposit and duration product boundary", () => {
    const maxDepositForOneDay = ((1n << 127n) - 1n) / 86_400n;
    const rows = parseCsv(
      `${HEADER}\n${csvRow("recipient", maxDepositForOneDay.toString(), "1", "0")}`,
    );

    expect(rows[0].amount * 86_400n).toBeLessThanOrEqual((1n << 127n) - 1n);
  });

  it.each([
    ["amount outside i128", csvRow("recipient", "170141183460469231731687303715884105728")],
    ["unsafe duration", csvRow("recipient", "86400", "9007199254740992")],
    ["non-numeric cliff", csvRow("recipient", "86400", "1", "NaN")],
    ["empty recipient", csvRow(" ", "86400")],
    ["deposit rate too low", csvRow("recipient", "86399")],
    ["accrual product overflow", csvRow("recipient", "170141183460469231731687303715884105727")],
  ])("rejects %s during CSV parsing", (_case, row) => {
    expect(() => parseCsv(`${HEADER}\n${row}`)).toThrow();
  });

  it("rejects more than the on-chain batch limit while parsing", () => {
    const rows = Array.from({ length: MAX_BATCH_SIZE + 1 }, () => csvRow()).join("\n");

    expect(() => parseCsv(`${HEADER}\n${rows}`)).toThrow(/MAX_BATCH_SIZE/);
  });

  it("revalidates direct builder input and checks timestamp overflow", () => {
    expect(() => buildBatchCreateTransaction([parsedRow({ durationDays: 0 })], OPTIONS)).toThrow(
      /duration_days/,
    );
    expect(() =>
      buildBatchCreateTransaction([parsedRow()], { ...OPTIONS, startTime: Number.MAX_SAFE_INTEGER }),
    ).toThrow(/timestamp/);
  });

  it("constructs a full batch in input order", () => {
    const rows = Array.from({ length: MAX_BATCH_SIZE }, (_, index) =>
      parsedRow({ recipient: `recipient-${index}` }),
    );
    const transaction = buildBatchCreateTransaction(rows, OPTIONS);

    expect(transaction.operations).toHaveLength(MAX_BATCH_SIZE);
    expect(transaction.operations.map(({ createStream }) => createStream.recipient)).toEqual(
      rows.map(({ recipient }) => recipient),
    );
  });
});