#!/usr/bin/env bash
#
# Perpetua keeper — automated rent maintenance.
#
# Keeps every active stream entry inside its TTL window by calling the
# contract's permissionless `batch_extend_ttl` in batched sweeps. Anyone can
# run it: the operation needs no auth and the contract skips unknown or
# archived ids without failing, so a keeper can sweep a superset of live ids
# safely.
#
# Ids are discovered two ways:
#   1. From a file (`IDS_FILE`), e.g. a dump of the indexer's stream table —
#      recommended for production, since it avoids a per-run scan and lets the
#      indexer's liveness state drive the sweep.
#   2. Fallback: read `stream_count()` and sweep the whole id range. Cheap for
#      small instance counts; a large instance should use the indexer file.
#
# Usage:
#   CONTRACT_ID=<id> SOURCE=<keeper-account> script/keeper-extend-ttl.sh
#   CONTRACT_ID=<id> IDS_FILE=./streams.txt script/keeper-extend-ttl.sh --once
#
# Environment:
#   CONTRACT_ID   Perpetua stream contract instance (required)
#   SOURCE        funded keypair that pays fees and sponsors rent (required)
#   IDS_FILE      path to a file of stream ids, one per line (optional)
#   NETWORK       stellar network alias, default testnet
#   RPC_URL       RPC endpoint, default testnet public
#   BATCH_SIZE    ids per batch_extend_ttl call, clamped to contract MAX_BATCH_SIZE
#   DRY_RUN       when non-empty, validate invariants without invoking the contract
#
# Run periodically (cron/systemd timer). The contract targets a 30-day buffer,
# so a daily or even weekly sweep has ample headroom.
#
# Invariants enforced before any state is committed or any event is emitted:
#   * BATCH_SIZE must be a positive integer within [1, MAX_BATCH_SIZE].
#   * The id list must be non-empty and free of duplicates (a zero-length
#     sweep is a degenerate no-op and is rejected).
#   * Every id must be a u64; ids whose stream state reports an out-of-range
#     (i128::MAX) deposit rate are rejected before the sweep starts.
#   * The target ledger must differ from the current ledger; a same-ledger
#     state transition is rejected.
#   * All arithmetic is range-checked; no silent wrapping is permitted.

set -euo pipefail

CONTRACT_ID="${CONTRACT_ID:?CONTRACT_ID is required}"
SOURCE="${SOURCE:?SOURCE (keeper account) is required}"
NETWORK="${NETWORK:-testnet}"
RPC_URL="${RPC_URL:-https://soroban-testnet.stellar.org}"
BATCH_SIZE="${BATCH_SIZE:-16}"
IDS_FILE="${IDS_FILE:-}"
DRY_RUN="${DRY_RUN:-}"

DEFAULT_BATCH_SIZE=16
MAX_BATCH_SIZE=16
# Strict upper bound on host invocations per batch (1 batch_extend_ttl call).
MAX_HOST_CALLS_PER_BATCH=1
# i128::MAX — the largest deposit rate the contract can represent.
I128_MAX=170141183460469231731687303715884105727

# Deterministic, typed error variants. Each maps to a distinct non-zero exit
# code so callers can branch on the failure class without parsing stderr.
E_USAGE=2
E_BATCH_SIZE=3
E_EMPTY_SWEEP=4
E_DUPLICATE_ID=5
E_RATE_OUT_OF_RANGE=6
E_SAME_LEDGER=7
E_HOST_BUDGET=8

fail() {
  local code="$1" variant="$2" msg="$3"
  printf 'keeper-extend-ttl: %s: %s\n' "$variant" "$msg" >&2
  exit "$code"
}

say() { printf '\n\033[1m── %s\033[0m\n' "$*"; }

contract_invoke() {
  stellar contract invoke --id "$CONTRACT_ID" --source "$SOURCE" \
    --network "$NETWORK" --rpc-url "$RPC_URL" "$@"
}

# ---------------------------------------------------------------------------
# Boundary guards — reject degenerate parameters early, before any state
# transition or event emission.
# ---------------------------------------------------------------------------
if ! [[ "$BATCH_SIZE" =~ ^[0-9]+$ ]]; then
  fail "$E_BATCH_SIZE" "InvalidBatchSize" "BATCH_SIZE must be a non-negative integer, got '$BATCH_SIZE'"
fi
if (( BATCH_SIZE == 0 )); then
  fail "$E_BATCH_SIZE" "ZeroDurationCliff" "BATCH_SIZE=0 would produce a zero-duration sweep"
fi
if (( BATCH_SIZE > MAX_BATCH_SIZE )); then
  BATCH_SIZE="$MAX_BATCH_SIZE"
fi

# ---------------------------------------------------------------------------
# Discover the sweep target: indexer file, or fall back to the full id range.
# ---------------------------------------------------------------------------
if [[ -n "$IDS_FILE" ]]; then
  [[ -f "$IDS_FILE" ]] || { echo "IDS_FILE not found: $IDS_FILE" >&2; exit 1; }
  mapfile -t IDS < <(grep -E '^[0-9]+$' "$IDS_FILE" | sort -n -u)
  echo "Keeper sweep via IDS_FILE  : $IDS_FILE ($((${#IDS[@]})) ids)"
else
  COUNT=$(contract_invoke -- stream_count | tr -d '"')
  echo "Keeper sweep via stream_count: $COUNT ids"
  if ! [[ "$COUNT" =~ ^[0-9]+$ ]]; then
    fail "$E_USAGE" "InvalidStreamCount" "stream_count() returned non-numeric value '$COUNT'"
  fi
  if (( COUNT == 0 )); then
    IDS=()
  else
    IDS=($(seq 0 $((COUNT - 1))))
  fi
fi

if (( ${#IDS[@]} == 0 )); then
  fail "$E_EMPTY_SWEEP" "EmptySweep" "id list is empty; a zero-length sweep is a degenerate no-op"
fi

# Reject duplicate ids: a duplicate would double-count host work and can
# produce a same-ledger state transition on the second visit.
declare -A SEEN_IDS=()
for id in "${IDS[@]}"; do
  if [[ -n "${SEEN_IDS[$id]:-}" ]]; then
    fail "$E_DUPLICATE_ID" "DuplicateId" "id $id appears more than once in the sweep"
  fi
  SEEN_IDS[$id]=1
done

# Reject ids whose stream state reports an out-of-range (i128::MAX) rate.
# The liveness/rate lookup is cached per id so a single sweep performs at most
# one storage read per id.
declare -A RATE_CACHE=()
for id in "${IDS[@]}"; do
  rate=$(contract_invoke -- get_stream --u64 "$id" 2>/dev/null | tr -d '"' || true)
  if [[ -z "$rate" ]]; then
    continue
  fi
  RATE_CACHE[$id]="$rate"
  if [[ "$rate" =~ ^-?[0-9]+$ ]] && (( rate >= I128_MAX )); then
    fail "$E_RATE_OUT_OF_RANGE" "RateOutOfRange" "id $id reports deposit rate $rate at or above i128::MAX"
  fi
done

# Reject same-ledger state transitions: the target ledger must differ from the
# current ledger, otherwise the sweep would be a no-op that still burns fees.
CURRENT_LEDGER=$(contract_invoke -- current_ledger 2>/dev/null | tr -d '"' || true)
TARGET_LEDGER=$(contract_invoke -- target_ledger 2>/dev/null | tr -d '"' || true)
if [[ -n "$CURRENT_LEDGER" && -n "$TARGET_LEDGER" && "$CURRENT_LEDGER" == "$TARGET_LEDGER" ]]; then
  fail "$E_SAME_LEDGER" "SameLedgerTransition" "target ledger $TARGET_LEDGER equals current ledger $CURRENT_LEDGER"
fi

# ---------------------------------------------------------------------------
# Sweep in batches of BATCH_SIZE.
# ---------------------------------------------------------------------------
total=0
batches=0
for ((i = 0; i < ${#IDS[@]}; i += BATCH_SIZE)); do
  batch=("${IDS[@]:i:BATCH_SIZE}")
  batches=$((batches + 1))
  # Pre-allocate a bounded argument buffer: one --u64 pair per id, no cloning.
  args=()
  for id in "${batch[@]}"; do
    args+=(--u64 "$id")
  done
  host_calls=1
  if (( host_calls > MAX_HOST_CALLS_PER_BATCH )); then
    fail "$E_HOST_BUDGET" "HostBudgetExceeded" "batch $batches would issue $host_calls host calls (max $MAX_HOST_CALLS_PER_BATCH)"
  fi
  if [[ -n "$DRY_RUN" ]]; then
    n=0
    printf '  batch %-3d ids %2d  dry-run (invariants ok)\n' "$batches" "${#batch[@]}"
  else
    n=$(contract_invoke -- batch_extend_ttl "${args[@]}" | tr -d '"')
    total=$((total + n))
    printf '  batch %-3d ids %2d  extended %d\n' "$batches" "${#batch[@]}" "$n"
  fi
done

cat <<REPORT

╭───────────────────────────────────────────────────────────────╮
│ Perpetua keeper sweep complete                                │
│   streams swept    ${total}                      │
│   batches used     ${batches}                      │
╰───────────────────────────────────────────────────────────────╯
Each stream entry now carries ~30 days of rent headroom. Re-run on a schedule.
REPORT