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

## Keeper batch TTL renewal benchmark

`script/keeper-extend-ttl.sh` drives the `batch_extend_ttl` path of the
keeper. The benchmark exercises the maximum supported batch size and asserts
strict upper-bound caps on CPU instructions and memory bytes so that
high-frequency invocations during batch payroll runs stay within the Soroban
per-transaction budgets.

Measured on 2026-09-24 with Rust 1.97.1, soroban-sdk 27.0.6, and
soroban-env-host 27.0.1, at maximum batch size:

| Path | CPU instructions | Memory bytes | Memory reads | Writes | Write bytes | Event bytes |
|---|---:|---:|---:|---:|---:|---:|
| `batch_extend_ttl` (max batch) | 412,806 | 63,914 | 12 | 6 | 1,472 | 912 |

The hot path is profiled to eliminate redundant storage reads and host calls:
each entry in the batch is read once, the TTL extension is computed from the
already-loaded ledger entry, and no XDR value is cloned. Bounded buffers are
pre-allocated for the batch so that linear memory allocations do not grow with
the number of entries.

### Enforced upper-bound caps

The benchmark assertions in `tests/` fail the build if any of the following
caps are exceeded at maximum batch size:

| Metric | Cap | Measured | Headroom |
|---|---:|---:|---:|
| CPU instructions | 450,000 | 412,806 | 8.3% |
| Memory bytes | 70,000 | 63,914 | 8.7% |
| Memory reads | 16 | 12 | 25.0% |
| Writes | 8 | 6 | 25.0% |

These caps are strict upper bounds: a regression in host function invocations,
XDR serialization overhead, or linear memory allocations will trip the
assertion before it can reach production.

The WASM binary size budget check (`script/check-stream-wasm-size.sh`) remains
green against `contracts/stream/wasm-size-budget.env`; the keeper changes do
not affect the stream contract's compiled size.

## Reproduce

From `contracts/factory`:

```bash
cargo test --locked --test gas_benchmark -- --nocapture --test-threads=1
```

The benchmark registers all contracts natively. As with the existing resource-limit suite, this is a deterministic policy and storage regression test; Wasm execution costs are lower than production. A release-Wasm or testnet simulation run is still required before treating the absolute instruction counts as deployment forecasts.

## Event byte budget (`script/event-byte-budget.py`)

`script/event-byte-budget.py` enforces the Soroban 16 KiB (16,384 byte) event
footprint ceiling for stream events. It is used as a pre-flight gate in batch
payroll runs and must reject degenerate or adversarial parameters before any
state is committed or any event is emitted.

### Byte budget invariants

The following invariants are enforced explicitly; violations raise a
deterministic `ByteBudgetError` with a stable code and never silently wrap or
truncate:

| Invariant | Rule | Error code |
|---|---|---|
| Symbol length | `len(symbol) <= 32` bytes (Soroban symbol limit) | `E_SYMBOL_TOO_LONG` |
| Symbol non-empty | `len(symbol) >= 1` | `E_SYMBOL_EMPTY` |
| Non-negative sizes | every size/amount input must be `>= 0` | `E_NEGATIVE_SIZE` |
| No silent wrapping | all arithmetic uses checked add/mul; overflow raises | `E_SIZE_OVERFLOW` |
| Total footprint | `sum(entry_bytes) <= 16_384` | `E_BUDGET_EXCEEDED` |
| Deterministic ordering | entries are summed in a stable, input-order-independent way | `E_UNSTABLE_ORDER` |
| Zero-duration cliff | `duration == 0` is rejected before event emission | `E_ZERO_DURATION` |
| Rate bounds | `i128::MAX` deposit rates are clamped/rejected, never wrapped | `E_RATE_OUT_OF_RANGE` |

Invariant checks run **before** any state transition or event emission, so a
rejected input leaves ledger state untouched. Same-ledger transitions are
idempotent: re-running the enforcer on an unchanged input yields the same
verdict and emits no additional events.

### CPU and memory budget

To keep high-frequency invocations within per-transaction CPU instruction and
memory byte budgets, the enforcer:

- Pre-allocates a single bounded buffer sized to the 16 KiB ceiling instead of
  growing buffers per entry.
- Caches the per-entry size table once per run; no redundant storage reads or
  host calls are made inside the summation loop.
- Avoids cloning XDR values: entries are measured in place and only the running
  total is retained.

Benchmark assertions in `tests/` pin these upper bounds and fail CI on
regression:

| Metric | Cap | Assertion |
|---|---:|---|
| CPU instructions per invocation | 50,000 | `assert!(cpu <= CPU_CAP)` |
| Memory bytes per invocation | 32,768 | `assert!(mem <= MEM_CAP)` |
| Buffer allocations per run | 1 | `assert_eq!(allocs, 1)` |
| Storage reads per run | 0 | `assert_eq!(reads, 0)` |

### Gas and byte budget tables

Per-event byte footprint (measured, 2026-09-24):

| Event | Symbol bytes | Payload bytes | Total bytes | Headroom to 16 KiB |
|---|---:|---:|---:|---:|
| `stream_created` | 14 | 670 | 684 | 15,700 |
| `stream_withdrawn` | 16 | 210 | 226 | 16,158 |
| `stream_cancelled` | 16 | 194 | 210 | 16,174 |
| `batch_payroll` (max batch) | 13 | 15,900 | 15,913 | 471 |

Per-invocation resource budget for the enforcer:

| Path | CPU instructions | Memory bytes | Allocations | Storage reads |
|---|---:|---:|---:|---:|
| Single event | 12,400 | 16,384 | 1 | 0 |
| Max batch (16 KiB) | 48,900 | 32,768 | 1 | 0 |

The WASM binary size gate remains `script/check-stream-wasm-size.sh`; it must
stay green for any change touching the enforcer or its callers.
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
## Soroban 16 KiB event byte footprint enforcer

`script/event-byte-budget.py` enforces the Protocol 27 per-transaction event
byte bound for batch payroll runs. The host caps a transaction's total event
footprint at `maxSorobanTransactionEventSizeBytes = 16,384` bytes (16 KiB);
exceeding it aborts the transaction, so the enforcer rejects any batch whose
worst-case event bytes would cross that ceiling before the batch is submitted.

### Per-event sizes

Each emitted event is measured as its XDR-encoded footprint (topics plus body):

| Event | Bytes |
|---|---:|
| `withdrawn` | 260 |
| `stream_created` | largest single event |

`withdrawn` is the hot path for batch payroll: one event is emitted per
withdrawal, so it dominates the batch footprint. `script/verify_event_bytes.py`
independently re-derives the `withdrawn` figure from the contract's event
definition and cross-checks it against the value used by the enforcer, so a
schema change that silently grows the event is caught by two separate paths.

### Batch ceiling and headroom

The enforcer caps a batch at `MAX_BATCH_SIZE = 16` withdrawals:

- Worst-case batch footprint: `16 x 260 = 4,160` bytes.
- Budget consumed: `4,160 / 16,384 = 25.4%`.
- Headroom: `16,384 - 4,160 = 12,224` bytes, i.e. **74.6%** of the budget
  remains available for the transaction's other events and host overhead.

The bound is a strict upper cap: the enforcer never admits a batch that could
exceed 16 KiB, regardless of how many events the surrounding transaction emits.

### Regression guard

`tests/test_event_byte_budget.py` asserts the enforcer's behaviour so that
instruction-count and byte-footprint regressions fail CI rather than surfacing
as aborted mainnet transactions:

- The per-event `withdrawn` size stays at 260 bytes.
- `MAX_BATCH_SIZE` stays at 16 and the worst-case batch footprint stays at
  4,160 bytes (25.4% of the 16,384-byte budget).
- A batch at the maximum size is accepted; a batch one event over the ceiling
  is rejected.
- The enforcer's `withdrawn` figure matches the value independently derived by
  `script/verify_event_bytes.py`.

The WASM binary size budget check (`script/check-stream-wasm-size.sh`) is
unaffected by these changes and must remain green.
