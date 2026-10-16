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

To reproduce the keeper batch TTL renewal benchmark from `contracts/keeper`:

```bash
cargo test --locked --test keeper_ttl_benchmark -- --nocapture --test-threads=1
```

To verify the WASM binary size budget:

```bash
./script/check-stream-wasm-size.sh
```
