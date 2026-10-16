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
-- schemaVersion / abiVersion marker: keep in lockstep with the GraphQL
-- `schemaVersion` marker in schema/schema.graphql. Bump both together so
-- indexers can detect schema drift between the SQL and GraphQL layers.
CREATE TABLE IF NOT EXISTS schema_metadata (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO schema_metadata (key, value) VALUES
    ('schema_version', '1'),
    ('abi_version',    '1')
ON CONFLICT (key) DO NOTHING;

CREATE TABLE IF NOT EXISTS events (
    id              BIGSERIAL PRIMARY KEY,
    ledger_seq      INTEGER   NOT NULL,
    tx_hash         TEXT      NOT NULL,
    event_index     INTEGER   NOT NULL DEFAULT 0,   -- position within tx
    topic0          TEXT      NOT NULL,             -- event kind, snake_case
    stream_id       BIGINT    NOT NULL,
    payload         JSONB     NOT NULL,             -- full decoded event data
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Idempotent replay/dedup: a retried or re-ingested transaction must not
    -- double-apply the same event. (tx_hash, event_index) is the natural key.
    CONSTRAINT events_tx_hash_event_index_key UNIQUE (tx_hash, event_index)
);

CREATE INDEX IF NOT EXISTS idx_events_stream_id      ON events (stream_id, ledger_seq DESC);
CREATE INDEX IF NOT EXISTS idx_events_topic0_ledger  ON events (topic0, ledger_seq DESC);
CREATE INDEX IF NOT EXISTS idx_events_ledger         ON events (ledger_seq DESC);
-- Bounded-cost lookup for high-frequency batch payroll reads: covers the
-- (topic0, stream_id, ledger_seq) access path without a full scan.
CREATE INDEX IF NOT EXISTS idx_events_topic0_stream_ledger
    ON events (topic0, stream_id, ledger_seq DESC);

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
    status          TEXT      NOT NULL,             -- Active|Paused|Cancelled|Depleted
    created_ledger  INTEGER   NOT NULL,
    updated_ledger  INTEGER   NOT NULL,
    ttl_extended_at INTEGER   NOT NULL DEFAULT 0,
    -- Archival state, detected off-chain via
    -- `stream_exists(id) == false && id < stream_count()` (expired persistent
    -- entry). Set TRUE when the entry is archived; the indexer must not treat
    -- an archived stream as deleted. See docs/soroban-rpc-read-skew.md for the
    -- archival detection and footprint-restoration recovery procedure.
    archived        BOOLEAN   NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_streams_sender    ON streams (sender);
CREATE INDEX IF NOT EXISTS idx_streams_recipient ON streams (recipient);
CREATE INDEX IF NOT EXISTS idx_streams_status    ON streams (status);
CREATE INDEX IF NOT EXISTS idx_streams_token     ON streams (token);
-- Partial index: archival sweeps only ever scan archived rows, so keep the
-- index small and the lookup bounded.
CREATE INDEX IF NOT EXISTS idx_streams_archived  ON streams (stream_id)
    WHERE archived;

-- Materialized activity for dashboards / queries without replay.
CREATE MATERIALIZED VIEW IF NOT EXISTS withdrawals AS
SELECT stream_id, recipient, (payload->>'amount')::NUMERIC           AS amount,
       (payload->>'withdrawn')::NUMERIC                              AS withdrawn,
       ledger_seq, tx_hash
FROM events
WHERE topic0 = 'withdrawn'
ORDER BY ledger_seq DESC;