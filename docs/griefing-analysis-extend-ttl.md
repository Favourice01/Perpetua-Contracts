# Griefing Analysis — `extend_stream_ttl` / `batch_extend_ttl`

Issue #97. The two TTL-maintenance entrypoints are **permissionless by design**:
any keeper can pay to keep any stream alive, so a recipient's claim never
depends on the sender's continued goodwill. This document formally examines
whether an unauthenticated caller can grief a stream — locking its state,
manipulating its rent target, exhausting storage, or otherwise imposing cost or
harm on another party.

**Status: no griefing vector found.** Every analysed avenue either (a) is
impossible by construction because the entrypoints only ever bump an entry's
TTL, or (b) costs the *attacker* money while benefiting the stream owner. The
claims below are pinned by tests in `contracts/stream/src/test/ttl.rs`,
`test/batch.rs` and `test/auth.rs`.

---

## 1. Threat model

The contract has no admin key, no upgrade path, no fee switch and no global
pause (all explicit non-goals, README § Non-goals). The only state that exists
is: one `NextStreamId`/`StreamCount` instance pair, one `Stream(id)` entry per
stream, and one `Delegate(...)` entry per grant. An attacker's goal is to make
a *third party* worse off — lose funds, lose access, pay unexpected rent, or
suffer a denial of access to chain state (a stream entry archived).

The permissionless surface is intentionally tiny:

```rust
extend_stream_ttl(stream_id: u64) -> Result<u32, Error>
batch_extend_ttl(stream_ids: Vec<u64>) -> Result<u32, Error>
```

Both share two structural facts, which are the load-bearing results of the
whole analysis:

1. **They write nothing but TTL.** The entry path is
   `peek_stream` (a read that never writes) followed by
   `env.storage().persistent().extend_ttl(...)`. There is no `save_stream`, no
   `token_transfer`, no state-field assignment anywhere in either function
   (`contracts/stream/src/lib.rs:1178`, `:1202`). A stream read after a sweep is
   byte-identical to before — `test/ttl.rs::extend_stream_ttl_leaves_stream_state_untouched`.

2. **They read no more than the stream itself.** No `Vec`s of ids are
   persisted, no cross-stream state is touched. Isolation between streams is
   total: a sweep of stream *A* cannot alter stream *B*'s TTL, let alone its
   accounting.

## 2. Vector-by-vector analysis

### V1 — Lock the stream (freeze/pause/cancel it through the keeper path)

**Result: impossible.**

Extending TTL does not touch `status`, `paused_at`, `withdrawn`, `deposited`,
`end_time`, or any other field. State transitions (`pause`, `resume`, `cancel`,
`withdraw`) are all guards published under the *mutating* entrypoints and all
require the sender's or recipient's authorization. There is no code path in
`extend_stream_ttl` that reaches any of those state changes. An attacker cannot
use the keeper path to start, stop, cancel, or reassign a stream, cannot push a
stream into `Cancelled`/`Depleted`, and cannot freeze accrual.

Tests: `extend_stream_ttl_leaves_stream_state_untouched` (a swept, paused
stream stays `Paused` with an unchanged withdrawable balance and unchanged
storage).

### V2 — Manipulate the rent target against the owner

**Result: the attacker can only pay rent, never extort it.**

The rent target is a pure function of the stream and the current time
(`storage::ttl_target_ledgers`):

```
target = clamp(seconds_to_ledgers(remaining_life + TTL_BUFFER_SECONDS),
               MIN_STREAM_TTL_LEDGERS, network_max_ttl)
```

There is no stored `config` or per-stream knob an attacker could flip to make
the target huge or tiny — the inputs are the stream's own schedule and the
network's `max_ttl()`, neither of which the caller controls. What the caller
*can* do is pay the ledger cost of a transaction that extends an entry. That
cost is charged to the caller's transaction, not to anyone else, and it always
performs the same `min(target, max)` regardless of who asked. There is no way
to force the network or the stream owner to pay more rent than their own normal
usage already requires.

V2a — **sub-vector: can an attacker *starve* a stream (i.e. on purpose extend
it to a low target, or spam extensions to keep a legitimate high target away)?**
Soroban's `extend_ttl(threshold, extend_to)` only raises live-until for
entries below `threshold`; it never lowers it. Rent already paid is never
clawed back, either by the contract (a `cancel` reshapes the *target* but the
entry keeps its higher funded value) or by a later lower-target sweep. The
README states this and `test/ttl.rs::permissionless_extension_cannot_reduce_existing_rent`
pins it. An attacker therefore cannot reduce another party's funded TTL —
only ever add to it.

### V3 — Exhaust storage / blow the footprint

**Result: bounded, and no new state is created.**

`batch_extend_ttl` is capped at `MAX_BATCH_SIZE = 16` and rejects oversized
payloads with `Error::BatchTooLarge` before the loop starts. The only entries
touched are the stream entries that already exist plus the instance entry.
No `Vec`, no per-caller list, no new key is ever written; TTL extension charges
footprint for the touched entries, which is the same bounded set as any
legitimate 16-stream sweep. Storage exhaustion is therefore impossible to
compound: a caller can only ever touch a maximum of 16 stream entries per call,
and each call costs the caller non-trivially (transaction fee + rent).

### V4 — Gas griefing / panic with malformed inputs

**Result: every malformed input is a cheap typed error.**

- **Empty** vector → `EmptyBatch` before any work.
- **Oversized** vector → `BatchTooLarge` before any work.
- **Malformed element** (not a `u64`) → `MalformedStreamId` before any work.
- **Duplicate id** → `DuplicateStreamId`. The duplicate check
  (`reject_duplicate_ids`) is O(n²) with `n ≤ 16`, i.e. at most 120
  comparisons — nothing remotely expensive.
- **Unknown id** → *skipped in the batch sweep* (`batch_extend_ttl` is
  per-item, `test/ttl.rs::batch_extend_skips_unknown_ids_without_failing`), and
  a typed `StreamNotFound` for the single-item call. An attacker who feeds a
  stale or hallucinated id list imposes, per item, exactly one missed storage
  read. Because `batch_extend_ttl` skips instead of panicking, a griefing list
  can never abort a legitimate keeper's sweep.
- **Hot/expiring entries**: reading an archived entry via `peek_stream` performs
  a host restore, but restoring is what the entry needs anyway and is charged to
  the caller's footprint, not the owner's.

### V5 — Event spam / indexer denial-of-service

**Result: bounded one-per-stream and self-funded.**

Per sweep each successfully extended stream emits exactly one `ttl_extended`
event. The event budget is capped by `MAX_BATCH_SIZE`, so a single call can
produce at most 16 events. Generating them repeatedly costs the caller the
instruction and fee budget every time; it does not grow any unbounded on-chain
structure and does not touch other parties' accounts.

### V6 — Freezing out a legitimate extender (front-running the keeper)

**Result: not harmful.** Because TTL only ever grows and no one else's rent is
ever reduced (V2a), a griefing keeper beating a legitimate keeper to a stream
merely funds the stream slightly earlier. The legitimate keeper's later sweep
is then a cheap no-op. There is no race to exploit.

### V7 — Reentrancy / cross-contract abuse

**Result: no sub-contract calls.** Neither permissionless function invokes the
token contract or any other contract. There is nothing to reenter, and the
Soroban host forbids reentrancy outright.

### V8 — Burning the contract's pooled funds

**Result: impossible.** The contract owns a pooled token balance, but TTL rent
is paid by the *transaction*, never from the pooled balance, and neither
function moves tokens (V1). `test/ttl.rs::a_sweep_cannot_change_what_is_withdrawable`
and the pool-invariant assertions in every sweep test pin the pooled balance
unchanged.

## 3. Who can act, and the cost asymmetry

| Party | What they may do | Cost to them | Effect on others |
|---|---|---|---|
| Keeper | extend any stream's TTL | tx fee + rent | strictly positive: longer readability |
| Recipient | extend own streams | tx fee + rent | strictly positive |
| Attacker | extend any stream | tx fee + rent | strictly positive for owner; attacker loses fees; cannot shrink/alter state |

The asymmetry runs **against** the attacker: every action costs the actor and
benefits the stream owner. There is no action with negative-sum externalities.

## 4. Conclusion

`extend_stream_ttl` and `batch_extend_ttl` are grief-proof as designed. The
permissionless surface is safe precisely *because* it is minimal — it reads a
stream and writes a TTL, and nothing else. The README's claim
("There is nothing to grief — the caller only ever *pays* rent, and TTL
extension cannot move funds or change stream state") is substantiated:

- no state mutation → no lock (V1),
- deterministic bounded rent target, never reduced → no extortion or
  starvation (V2 / V2a),
- bounded, new-state-free storage footprint → no storage exhaustion (V3),
- cheap typed rejection of every malformed input → no gas griefing (V4),
- no sub-contract calls and no pooled-fund access → nothing to reenter or
  drain (V7 / V8).

**Recommendation (no change required):** keep the permissionless design, keep
`MAX_BATCH_SIZE = 16` as the enforcement point, and keep the duplicate/empty/
oversized/malformed preflights. The existing regression tests in `test/ttl.rs`
and `test/batch.rs` plus the new Issue #97 cases in `test/ttl.rs` should run in
CI.

---

## 5. Enforced invariants for `script/keeper-extend-ttl.sh` (Issue #289)

The off-chain keeper runner `script/keeper-extend-ttl.sh` is the operational
counterpart of the permissionless entrypoints above. Because it feeds
adversarial or degenerate parameters straight into the contract, it enforces
the same invariants *before* committing state or emitting any Soroban event.
Every check below rejects early with a deterministic, non-zero exit and a
stable error token — there is no silent wrapping, no implicit defaulting, and
no partial batch commit.

### 5.1 Explicit boundary checks

| Boundary | Invariant enforced | Rejection |
|---|---|---|
| Zero-duration cliff | `duration_seconds > 0`; a zero or negative remaining life is never converted into a TTL target | `E_ZERO_DURATION` |
| Deposit rate | `0 < deposit_rate <= i128::MAX`; rates are parsed as `i128` and rejected on overflow rather than wrapping | `E_RATE_OUT_OF_RANGE` |
| Same-ledger transition | `current_ledger != target_ledger`; a transition that would resolve to the same ledger is a no-op and is refused | `E_SAME_LEDGER` |
| Batch size | `1 <= n <= MAX_BATCH_SIZE (16)` | `E_EMPTY_BATCH` / `E_BATCH_TOO_LARGE` |
| Duplicate ids | ids are unique within a batch | `E_DUPLICATE_ID` |
| Malformed id | every element parses as a `u64` | `E_MALFORMED_ID` |
| TTL target | `MIN_STREAM_TTL_LEDGERS <= target <= network_max_ttl`, clamped, never negative | `E_TTL_TARGET_OUT_OF_RANGE` |

The same-ledger and zero-duration guards are the two that matter most in
practice: both are "cliffs" where a naive implementation would either emit a
redundant event or compute a zero-length extension, and both are refused
outright rather than coerced.

### 5.2 Deterministic error variants

Each rejection maps to exactly one error token, emitted on stderr and used as
the process exit status class, so CI and operators can assert on it:

- `E_ZERO_DURATION` — degenerate zero/negative duration.
- `E_RATE_OUT_OF_RANGE` — deposit rate outside `(0, i128::MAX]`.
- `E_SAME_LEDGER` — same-ledger state transition.
- `E_EMPTY_BATCH`, `E_BATCH_TOO_LARGE` — batch cardinality.
- `E_DUPLICATE_ID`, `E_MALFORMED_ID` — batch element validation.
- `E_TTL_TARGET_OUT_OF_RANGE` — computed TTL target outside the legal window.

All variants are checked *before* any state is committed or any event is
emitted; a rejected run leaves no partial mutation behind.

### 5.3 Property-based regression tests

The invariants above are pinned by property-based regression tests that
generate adversarial inputs (zero durations, `i128::MAX` and out-of-range
rates, same-ledger transitions, oversized/duplicate/malformed batches) and
assert that:

1. every degenerate input is rejected with its deterministic error token,
2. no state mutation or event emission occurs on a rejected run,
3. accepted runs preserve the TTL-only, monotonic-non-decreasing property
   described in §2 (V2 / V2a).

These tests run in CI alongside the contract-level cases in `test/ttl.rs` and
`test/batch.rs`.

### 5.4 Cross-references

- `docs/KNOWN-LIMITATIONS.md` — the documented limits on TTL targets, batch
  size, and rate ranges that these guards enforce.
- §2 (V2 / V2a) above — the monotonic, never-reduced rent target that the
  same-ledger and zero-duration guards protect.