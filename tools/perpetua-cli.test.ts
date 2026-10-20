import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import {
  CLI_EVENT_PAYLOAD_SCHEMAS,
  CLI_EVENT_TOPIC_LAYOUTS,
  classifyFailure,
  isArchivedStream,
  isLedgerSkewed,
  retryIdempotent,
} from "./perpetua-cli";

describe("perpetua-cli schema parity", () => {
  it("matches the withdrawn golden fixture", () => {
    const fx = JSON.parse(
      readFileSync(new URL("../tests/fixtures/event_snapshots/withdrawn.json", import.meta.url), "utf8"),
    );
    expect(CLI_EVENT_TOPIC_LAYOUTS.withdrawn).toEqual(fx.topic_layout);
    expect(CLI_EVENT_PAYLOAD_SCHEMAS.withdrawn).toEqual(fx.payload_schema);
  });
});

describe("perpetua-cli recovery", () => {
  it("detects archival and skew", () => {
    expect(isArchivedStream(false, 10n, 3)).toBe(true);
    expect(isArchivedStream(false, 3n, 3)).toBe(false);
    expect(isLedgerSkewed(99, 100)).toBe(true);
    expect(isLedgerSkewed(100, 100)).toBe(false);
    expect(classifyFailure("entry archived").kind).toBe("archived");
  });

  it("retries without double-submitting", async () => {
    let calls = 0;
    const r = await retryIdempotent(
      async () => {
        if (++calls < 2) throw new Error("TRY_AGAIN_LATER");
        return "ok";
      },
      async () => undefined,
    );
    expect(r).toBe("ok");
    expect(await retryIdempotent(async () => "again", async () => "landed")).toBe("landed");
    await expect(retryIdempotent(async () => { throw new Error("contract error"); }, async () => undefined)).rejects.toThrow();
  });
});
