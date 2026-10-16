-- Perpetua indexer schema — PostgreSQL.
--
-- Two concerns:
--   * `events` is the append-only raw ledger of stream events (source of
--     truth, replayable, feeds the GraphQL layer / SubQuery project).
--   * `streams` is the current-state mirror, rebuilt by folding `events`
--     forward; a keeper reads `ttl_extended_at` and `status` straight off it.
--
-- Mirrors contracts/stream/src/events.rs. Amounts are i128, times are u64
-- (unix seconds), both stored as NUMERIC(39). Ledger sequence is u32.
-- On chain, stream_id is u64 — keep it BIGINT here so ids never collide.
--
-- Invariants (kept in lockstep with schema/schema.graphql, #294):
--   * Amounts are i128, stored as NUMERIC(39) — no silent wrapping; the
--     full i128 range fits without truncation.
--   * Time ordering: end_time > start_time and
--     start_time <= cliff_time <= end_time. Degenerate parameters
--     (zero-duration cliffs, inverted ranges) are rejected early.
--   * Accounting: deposited >= 0, withdrawn >= 0, withdrawn <= deposited,
--     paused_total >= 0, ttl_extended_at >= 0.
--   * status is one of 'active' | 'paused' | 'cancelled' | 'depleted'.
-- These CHECK constraints mirror the GraphQL projection so both stay
-- aligned with the frozen ABI.

CREATE TABLE IF NOT EXISTS events (
    id              BIGSERIAL PRIMARY KEY,
    ledger_seq      INTEGER   NOT NULL,
    tx_hash         TEXT      NOT NULL,
    topic0          TEXT      NOT NULL,             -- event kind, snake_case
    stream_id       BIGINT    NOT NULL,
    payload         JSONB     NOT NULL,             -- full decoded event data
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_events_stream_id      ON events (stream_id, ledger_seq DESC);
CREATE INDEX IF NOT EXISTS idx_events_topic0_ledger  ON events (topic0, ledger_seq DESC);
CREATE INDEX IF NOT EXISTS idx_events_ledger         ON events (ledger_seq DESC);

-- Current-state mirror.
CREATE TABLE IF NOT EXISTS streams (
    stream_id       BIGINT PRIMARY KEY,
    sender          TEXT      NOT NULL,             -- G…/C… address
    recipient       TEXT      NOT NULL,
    token           TEXT      NOT NULL,
    deposited       NUMERIC(39) NOT NULL,
    withdrawn       NUMERIC(39) NOT NULL,
    start_time      BIGINT    NOT NULL,
    end_time        BIGINT    NOT NULL,
    cliff_time      BIGINT    NOT NULL,
    cancellable     BOOLEAN   NOT NULL,
    pausable        BOOLEAN   NOT NULL,
    transferable    BOOLEAN   NOT NULL,
    paused_at       BIGINT,
    paused_total    BIGINT    NOT NULL DEFAULT 0,
    status          TEXT      NOT NULL,             -- active|paused|cancelled|depleted
    created_ledger  INTEGER   NOT NULL,
    updated_ledger  INTEGER   NOT NULL,
    ttl_extended_at INTEGER   NOT NULL DEFAULT 0,

    -- Boundary / invariant guards (mirrors schema/schema.graphql, #294).
    CONSTRAINT streams_end_after_start   CHECK (end_time > start_time),
    CONSTRAINT streams_cliff_in_range    CHECK (cliff_time BETWEEN start_time AND end_time),
    CONSTRAINT streams_deposited_nonneg  CHECK (deposited >= 0),
    CONSTRAINT streams_withdrawn_nonneg  CHECK (withdrawn >= 0),
    CONSTRAINT streams_withdrawn_le_dep  CHECK (withdrawn <= deposited),
    CONSTRAINT streams_paused_total_nonneg CHECK (paused_total >= 0),
    CONSTRAINT streams_ttl_extended_nonneg CHECK (ttl_extended_at >= 0),
    CONSTRAINT streams_status_valid      CHECK (status IN ('active', 'paused', 'cancelled', 'depleted'))
);

CREATE INDEX IF NOT EXISTS idx_streams_sender    ON streams (sender);
CREATE INDEX IF NOT EXISTS idx_streams_recipient ON streams (recipient);
CREATE INDEX IF NOT EXISTS idx_streams_status    ON streams (status);
CREATE INDEX IF NOT EXISTS idx_streams_token     ON streams (token);

-- Materialized activity for dashboards / queries without replay.
CREATE MATERIALIZED VIEW IF NOT EXISTS withdrawals AS
SELECT stream_id, recipient, (payload->>'amount')::NUMERIC           AS amount,
       (payload->>'withdrawn')::NUMERIC                              AS withdrawn,
       ledger_seq, tx_hash
FROM events
WHERE topic0 = 'withdrawn'
ORDER BY ledger_seq DESC;