# Gas benchmarks

This file covers the **stream/factory** paths. Governance voting and execution
are analysed separately in
[governance-gas.md](governance-gas.md), which measures operation counts and
event bytes for `propose` / `approve` / `execute` and documents what a
multi-signer round costs.

## Factory gas benchmark

The factory benchmark compares three first-stream creation paths under the same Stellar Asset Contract, schedule, amount, and ledger state:

1. `direct`: `FluxoraStream::create_stream` without a wrapper.
2. `forwarding`: a contract that forwards the same arguments to `FluxoraStream` without factory policy checks.
3. `factory`: `FluxoraFactory::create_stream`, including the consolidated policy read, policy checks, and stream dispatch.

The forwarding path is the control for cross-contract dispatch. The issue's policy-overhead budget is measured as `factory - forwarding`, while `factory - direct` is also logged as total wrapper overhead.

## Measured result

Measured on 2026-09-24 with Rust 1.97.1, soroban-sdk 27.0.6, and soroban-env-host 27.0.1:

| Path | CPU instructions | Memory bytes | Memory reads | Writes | Write bytes | Event bytes |
|---|---:|---:|---:|---:|---:|---:|
| Direct | 276,074 | 42,583 | 8 | 5 | 1,228 | 684 |
| Forwarding control | 299,465 | 46,688 | 9 | 5 | 1,228 | 684 |
| Factory | 327,424 | 51,221 | 9 | 5 | 1,228 | 684 |

- Policy overhead: `327,424 - 299,465 = 27,959` instructions, or **9.34%** of forwarding stream creation.
- Total wrapper overhead: `327,424 - 276,074 = 51,350` instructions, or **18.60%** of direct creation. This includes the forwarding contract invocation itself and is reported separately rather than attributed to policy validation.
- The factory adds one in-memory policy read and no stream-ID vector read or write. Rate division and bounds checks are skipped when both optional rate bounds are unset.

## Reproduce

From `contracts/factory`:

```bash
cargo test --locked --test gas_benchmark -- --nocapture --test-threads=1
```

The benchmark registers all contracts natively. As with the existing resource-limit suite, this is a deterministic policy and storage regression test; Wasm execution costs are lower than production. A release-Wasm or testnet simulation run is still required before treating the absolute instruction counts as deployment forecasts.

## Off-chain indexer schema (`schema/schema.graphql` / `schema/schema.sql`)

The off-chain subgraph and SQL indexer schema is not executed on-chain, so it
consumes no Soroban host instructions directly. It does, however, drive the
CPU and memory budget of the indexer workers that ingest stream events and
serve batch payroll queries. The caps below are enforced as strict
upper bounds: a run that exceeds any of them fails the benchmark suite rather
than merely logging a warning.

### Hot paths and strict upper-bound caps

| Hot path | CPU instruction cap | Memory byte cap | Notes |
|---|---:|---:|---|
| Event ingestion (per event) | 120,000 | 32,768 | Decode + validate + upsert one `StreamEvent` |
| Stream projection fold (per stream) | 480,000 | 131,072 | Fold all events for a single stream into its projection |
| Batch payroll query (per batch) | 2,400,000 | 524,288 | Resolve up to `MAX_BATCH_SIZE` streams in one query |

The caps are strict: the benchmark asserts `measured <= cap` for every path,
so a regression in XDR decoding, redundant storage reads, or unnecessary
cloning of XDR values fails CI immediately. Buffers used during ingestion and
projection are pre-allocated to the cap sizes and reused across events to avoid
per-event linear-memory growth.

### Bounded pagination limits

All list-returning fields in `schema/schema.graphql` are paginated with hard
bounds so that a single query can never force an unbounded scan:

| Limit | Value | Applies to |
|---|---:|---|
| `MAX_PAGE_SIZE` | 100 | Every connection's `first` / `last` argument |
| `MAX_BATCH_SIZE` | 250 | Batch payroll stream resolution |
| `MAX_EVENTS_PER_STREAM` | 10,000 | Projection fold depth |

Requests above these bounds are rejected with a validation error rather than
silently truncated.

### Per-event byte costs

Per-event byte costs are measured by `script/event-byte-budget.py` and must
stay within the Protocol 27 event byte cap of **16,384 bytes** per event.

| Event kind | Measured bytes | Analytical bytes | Headroom vs 16,384 |
|---|---:|---:|---:|
| `stream_created` | 412 | 420 | 15,972 |
| `stream_withdrawn` | 268 | 272 | 16,116 |
| `stream_cancelled` | 244 | 248 | 16,140 |
| `stream_completed` | 236 | 240 | 16,148 |
| `batch_payroll_created` | 1,024 | 1,040 | 15,360 |

Measured values come from `script/event-byte-budget.py`; analytical values are
the worst-case serialized size assuming maximum-length identifiers and
addresses. Both columns are well under the 16,384-byte Protocol 27 cap.

### WASM size budget

The indexer schema ships no Wasm of its own, but the stream contract it indexes
does. `script/check-stream-wasm-size.sh` must remain green:

```bash
./script/check-stream-wasm-size.sh
```

The check fails the build if the release Wasm exceeds the configured size
budget, guarding against schema-driven codegen bloat leaking into the contract
artifact.

### Regression assertions

The benchmark assertions that prevent instruction-count and memory-byte
regressions live in `tests/`:

- `tests/indexer_gas_benchmark.rs` asserts the per-event, per-stream, and
  per-batch caps above and fails on any increase.
- `tests/event_byte_budget.rs` asserts every event kind stays under the
  16,384-byte Protocol 27 cap and cross-checks `script/event-byte-budget.py`.
- `tests/pagination_bounds.rs` asserts `MAX_PAGE_SIZE`, `MAX_BATCH_SIZE`, and
  `MAX_EVENTS_PER_STREAM` are enforced and that over-limit requests error.

Run them with:

```bash
cargo test --locked --test indexer_gas_benchmark --test event_byte_budget --test pagination_bounds -- --nocapture --test-threads=1
```

As with the factory benchmark, these run natively and are deterministic
regression guards; absolute counts are upper bounds, not deployment forecasts.
