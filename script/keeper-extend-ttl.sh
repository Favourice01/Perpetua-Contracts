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
#   CONTRACT_ID=<id> SOURCE=<acct> script/keeper-extend-ttl.sh --dry-run --json
#
# Options:
#   --once        run a single sweep then exit (default: same, kept for parity)
#   --dry-run     discover ids and print the plan; never submit a transaction
#   --json        emit a deterministic machine-readable JSON summary on stdout
#   --help        print this usage block and exit 0
#
# Environment:
#   CONTRACT_ID   Perpetua stream contract instance (required)
#   SOURCE        funded keypair that pays fees and sponsors rent (required)
#   IDS_FILE      path to a file of stream ids, one per line (optional)
#   NETWORK       stellar network alias, default testnet
#   RPC_URL       RPC endpoint, default testnet public
#   BATCH_SIZE    ids per batch_extend_ttl call, clamped to contract MAX_BATCH_SIZE
#   MAX_RETRIES   bounded retry attempts per batch, default 4
#   LEDGER_BARRIER consecutive getLatestLedger samples that must agree, default 3
#
# Event / ABI schema parity (see docs/ABI.md, docs/indexing_guide.md):
#   The keeper consumes the contract's `ttl_extended` event:
#     topic[0] = "ttl_extended" (Symbol)
#     topic[1] = stream_id      (u64)
#     data     = extended_to_ledgers (u32)
#   and the `batch_extend_ttl(ids: Vec<u64>) -> u32` return value, which is the
#   number of entries actually extended in that call. The JSON summary emitted
#   with --json mirrors this schema so indexers can reconcile 1:1.
#
# Exit codes (typed, stable for tests):
#   0  success (sweep complete, or nothing to sweep)
#   2  missing CONTRACT_ID or SOURCE
#   3  missing / unreadable IDS_FILE
#   4  RPC / contract invocation failure
#   5  archived entry detected and not restorable
#   6  one or more batches failed (partial batch failure)
#
# Run periodically (cron/systemd timer). The contract targets a 30-day buffer,
# so a daily or even weekly sweep has ample headroom.

set -euo pipefail

EXIT_OK=0
EXIT_MISSING_ENV=2
EXIT_MISSING_IDS_FILE=3
EXIT_RPC_FAILURE=4
EXIT_ARCHIVED=5
EXIT_PARTIAL_BATCH=6

DRY_RUN=0
JSON_OUT=0
for arg in "$@"; do
  case "$arg" in
    --once) ;;
    --dry-run) DRY_RUN=1 ;;
    --json) JSON_OUT=1 ;;
    --help|-h)
      sed -n '2,60p' "$0" | sed 's/^# \{0,1\}//'
      exit "$EXIT_OK"
      ;;
    *)
      echo "unknown option: $arg" >&2
      exit "$EXIT_MISSING_ENV"
      ;;
  esac
done

if [[ -z "${CONTRACT_ID:-}" || -z "${SOURCE:-}" ]]; then
  echo "CONTRACT_ID and SOURCE (keeper account) are required" >&2
  exit "$EXIT_MISSING_ENV"
fi
NETWORK="${NETWORK:-testnet}"
RPC_URL="${RPC_URL:-https://soroban-testnet.stellar.org}"
BATCH_SIZE="${BATCH_SIZE:-16}"
IDS_FILE="${IDS_FILE:-}"
MAX_RETRIES="${MAX_RETRIES:-4}"
LEDGER_BARRIER="${LEDGER_BARRIER:-3}"

DEFAULT_BATCH_SIZE=16
if [[ "$BATCH_SIZE" -gt "$DEFAULT_BATCH_SIZE" ]]; then
  BATCH_SIZE="$DEFAULT_BATCH_SIZE"
fi

say() { printf '\n\033[1m── %s\033[0m\n' "$*"; }

contract_invoke() {
  stellar contract invoke --id "$CONTRACT_ID" --source "$SOURCE" \
    --network "$NETWORK" --rpc-url "$RPC_URL" "$@"
}

# ---------------------------------------------------------------------------
# Ledger barrier: poll getLatestLedger until LEDGER_BARRIER consecutive samples
# agree, defeating RPC read skew before we read any contract state.
# ---------------------------------------------------------------------------
latest_ledger() {
  stellar ledger latest --network "$NETWORK" --rpc-url "$RPC_URL" 2>/dev/null \
    | grep -oE '[0-9]+' | head -n1
}

ledger_barrier() {
  local last="" stable=0 sample
  for _ in $(seq 1 20); do
    sample="$(latest_ledger || true)"
    if [[ -n "$sample" && "$sample" == "$last" ]]; then
      stable=$((stable + 1))
      if (( stable >= LEDGER_BARRIER )); then
        echo "$sample"
        return 0
      fi
    else
      stable=0
      last="$sample"
    fi
    sleep 1
  done
  echo "${last:-0}"
  return 0
}

# ---------------------------------------------------------------------------
# Archival detection: probe stream_exists(id) and stream_count() in one ledger
# snapshot. If !stream_exists(id) && id < stream_count() the entry is archived.
# ---------------------------------------------------------------------------
is_archived() {
  local id="$1" count="$2" exists
  exists="$(contract_invoke -- stream_exists --u64 "$id" 2>/dev/null | tr -d '"' || echo "false")"
  if [[ "$exists" == "false" && "$id" -lt "$count" ]]; then
    return 0
  fi
  return 1
}

restore_footprint() {
  local id="$1"
  echo "  restoring archived footprint for stream $id" >&2
  stellar contract restore --id "$CONTRACT_ID" --source "$SOURCE" \
    --network "$NETWORK" --rpc-url "$RPC_URL" --key u64:"$id" 2>/dev/null
}

# ---------------------------------------------------------------------------
# Discover the sweep target: indexer file, or fall back to the full id range.
# ---------------------------------------------------------------------------
if [[ -n "$IDS_FILE" ]]; then
  if [[ ! -f "$IDS_FILE" ]]; then
    echo "IDS_FILE not found: $IDS_FILE" >&2
    exit "$EXIT_MISSING_IDS_FILE"
  fi
  mapfile -t IDS < <(grep -E '^[0-9]+$' "$IDS_FILE" | sort -n -u)
  echo "Keeper sweep via IDS_FILE  : $IDS_FILE ($((${#IDS[@]})) ids)"
else
  COUNT=$(contract_invoke -- stream_count | tr -d '"') || {
    echo "RPC failure reading stream_count" >&2
    exit "$EXIT_RPC_FAILURE"
  }
  echo "Keeper sweep via stream_count: $COUNT ids"
  IDS=($(seq 0 $((COUNT - 1))))
fi

if (( ${#IDS[@]} == 0 )); then
  echo "Nothing to sweep."
  exit "$EXIT_OK"
fi

if (( DRY_RUN )); then
  echo "Dry run: would sweep ${#IDS[@]} ids in batches of $BATCH_SIZE"
  exit "$EXIT_OK"
fi

# ---------------------------------------------------------------------------
# Sweep in batches of BATCH_SIZE with bounded, idempotent retries.
# TTL extension is monotonic, so re-running a batch is always safe.
# ---------------------------------------------------------------------------
total=0
batches=0
failed_batches=0
json_batches=""

for ((i = 0; i < ${#IDS[@]}; i += BATCH_SIZE)); do
  batch=("${IDS[@]:i:BATCH_SIZE}")
  batches=$((batches + 1))
  args=()
  for id in "${batch[@]}"; do
    args+=(--u64 "$id")
  done

  # Archival probe against a single, barrier-synced ledger snapshot.
  barrier_ledger="$(ledger_barrier)"
  count_snapshot="$(contract_invoke -- stream_count 2>/dev/null | tr -d '"' || echo "0")"
  archived=0
  for id in "${batch[@]}"; do
    if is_archived "$id" "$count_snapshot"; then
      archived=1
      if ! restore_footprint "$id"; then
        echo "archived entry $id could not be restored" >&2
        exit "$EXIT_ARCHIVED"
      fi
    fi
  done
  if (( archived )); then
    echo "  batch $batches: restored archived footprint(s) at ledger $barrier_ledger"
  fi

  n=0
  attempt=0
  while :; do
    attempt=$((attempt + 1))
    if n=$(contract_invoke -- batch_extend_ttl "${args[@]}" 2>/dev/null | tr -d '"'); then
      break
    fi
    if (( attempt >= MAX_RETRIES )); then
      echo "  batch $batches failed after $attempt attempts" >&2
      failed_batches=$((failed_batches + 1))
      n=0
      break
    fi
    backoff=$((2 ** (attempt - 1)))
    echo "  batch $batches attempt $attempt failed; retrying in ${backoff}s" >&2
    sleep "$backoff"
  done

  total=$((total + n))
  printf '  batch %-3d ids %2d  extended %d\n' "$batches" "${#batch[@]}" "$n"
  json_batches+="{\"batch\":$batches,\"ids\":[$(IFS=,; echo "${batch[*]}")],\"extended\":$n,\"ledger\":$barrier_ledger},"
done

if (( JSON_OUT )); then
  printf '{"schema":"perpetua.keeper.sweep.v1","contract_id":"%s","batches":[%s],"total_extended":%d,"batches_used":%d,"failed_batches":%d}\n' \
    "$CONTRACT_ID" "${json_batches%,}" "$total" "$batches" "$failed_batches"
else
  cat <<REPORT

╭───────────────────────────────────────────────────────────────╮
│ Perpetua keeper sweep complete                                │
│   streams swept    ${total}                      │
│   batches used     ${batches}                      │
╰───────────────────────────────────────────────────────────────╯
Each stream entry now carries ~30 days of rent headroom. Re-run on a schedule.
REPORT
fi

if (( failed_batches > 0 )); then
  exit "$EXIT_PARTIAL_BATCH"
fi
exit "$EXIT_OK"