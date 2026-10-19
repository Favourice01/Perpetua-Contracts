#!/usr/bin/env sh
set -eu

budget_file="${1:-contracts/stream/wasm-size-budget.env}"

if [ ! -f "$budget_file" ]; then
  echo "missing wasm size budget file: $budget_file" >&2
  echo "expected a budget env file (e.g. contracts/stream/wasm-size-budget.env) defining PACKAGE, TARGET, ARTIFACT, BUILD_COMMAND, BASELINE_BYTES and MAX_BYTES." >&2
  exit 1
fi

# shellcheck disable=SC1090
. "$budget_file"

if [ -n "${ARTIFACT_OVERRIDE:-}" ]; then
  ARTIFACT="$ARTIFACT_OVERRIDE"
fi
if [ -n "${BUILD_COMMAND_OVERRIDE:-}" ]; then
  BUILD_COMMAND="$BUILD_COMMAND_OVERRIDE"
fi
if [ -n "${BASELINE_BYTES_OVERRIDE:-}" ]; then
  BASELINE_BYTES="$BASELINE_BYTES_OVERRIDE"
fi
if [ -n "${MAX_BYTES_OVERRIDE:-}" ]; then
  MAX_BYTES="$MAX_BYTES_OVERRIDE"
fi
if [ -n "${SCHEMA_ARTIFACT_OVERRIDE:-}" ]; then
  SCHEMA_ARTIFACT="$SCHEMA_ARTIFACT_OVERRIDE"
fi
if [ -n "${SCHEMA_MAX_BYTES_OVERRIDE:-}" ]; then
  SCHEMA_MAX_BYTES="$SCHEMA_MAX_BYTES_OVERRIDE"
fi

: "${PACKAGE:?PACKAGE is required}"
: "${TARGET:?TARGET is required}"
: "${ARTIFACT:?ARTIFACT is required}"
: "${BUILD_COMMAND:?BUILD_COMMAND is required}"
: "${BASELINE_BYTES:?BASELINE_BYTES is required}"
: "${MAX_BYTES:?MAX_BYTES is required}"

case "$BASELINE_BYTES" in
  ''|*[!0-9]*)
    echo "BASELINE_BYTES must be an unsigned integer, got: $BASELINE_BYTES" >&2
    exit 1
    ;;
esac

case "$MAX_BYTES" in
  ''|*[!0-9]*)
    echo "MAX_BYTES must be an unsigned integer, got: $MAX_BYTES" >&2
    exit 1
    ;;
esac

echo "fluxora-stream wasm size budget"
echo "package: $PACKAGE"
echo "target: $TARGET"
echo "artifact: $ARTIFACT"
echo "baseline_bytes: $BASELINE_BYTES"
echo "max_bytes: $MAX_BYTES"
echo "build_command: $BUILD_COMMAND"

if [ "$BUILD_COMMAND" != "true" ]; then
  sh -c "$BUILD_COMMAND"
fi

if [ ! -f "$ARTIFACT" ]; then
  echo "expected wasm artifact was not produced: $ARTIFACT" >&2
  exit 1
fi

size=$(wc -c < "$ARTIFACT" | tr -d ' ')
delta=$((size - BASELINE_BYTES))

section_breakdown=$(python3 - "$ARTIFACT" <<'PY'
import sys

path = sys.argv[1]
with open(path, 'rb') as fh:
    data = fh.read()

if len(data) < 8 or data[:4] != b'\x00asm':
    print('section_breakdown: code=0 data=0 custom=0 total=0')
    raise SystemExit(0)

code = 0
custom = 0
data_size = 0
offset = 8
while offset < len(data):
    if offset + 1 > len(data):
        break
    sec_id = data[offset]
    offset += 1
    value = 0
    shift = 0
    while True:
        if offset >= len(data):
            break
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if (byte & 0x80) == 0:
            break
        shift += 7
    if offset + value > len(data):
        break
    payload = data[offset:offset + value]
    offset += value
    if sec_id == 0:
        custom += len(payload)
    elif sec_id == 10:
        code += len(payload)
    elif sec_id == 11:
        data_size += len(payload)

print(f'section breakdown: code={code} data={data_size} custom={custom} total={len(data)}')
PY
)

printf '%s\n' "$section_breakdown"
printf 'fluxora_stream.wasm: %s bytes\n' "$size"
printf 'baseline_delta_bytes: %s\n' "$delta"

if [ "$size" -gt "$MAX_BYTES" ]; then
  echo "WASM size ${size} exceeds budget ${MAX_BYTES} bytes" >&2
  echo "Update $budget_file only when growth is intentional and reviewed." >&2
  exit 1
fi

echo "wasm size budget check passed (issue #290 keeper-runner optimizations)" >&2
# Off-chain subgraph & SQL indexer schema size budget.
# Defaults keep the check optional so existing callers stay green; set
# SCHEMA_ARTIFACT / SCHEMA_MAX_BYTES (or the *_OVERRIDE variants) to enforce.
SCHEMA_ARTIFACT="${SCHEMA_ARTIFACT:-schema/schema.graphql}"
SCHEMA_MAX_BYTES="${SCHEMA_MAX_BYTES:-}"

if [ -n "$SCHEMA_MAX_BYTES" ]; then
  case "$SCHEMA_MAX_BYTES" in
    ''|*[!0-9]*)
      echo "SCHEMA_MAX_BYTES must be an unsigned integer, got: $SCHEMA_MAX_BYTES" >&2
      exit 1
      ;;
  esac

  echo "off-chain schema size budget"
  echo "schema_artifact: $SCHEMA_ARTIFACT"
  echo "schema_max_bytes: $SCHEMA_MAX_BYTES"

  schema_failed=0
  for schema_file in "$SCHEMA_ARTIFACT" schema/schema.sql; do
    if [ ! -f "$schema_file" ]; then
      echo "expected schema artifact was not found: $schema_file" >&2
      schema_failed=1
      continue
    fi
    schema_size=$(wc -c < "$schema_file" | tr -d ' ')
    printf '%s: %s bytes\n' "$schema_file" "$schema_size"
    if [ "$schema_size" -gt "$SCHEMA_MAX_BYTES" ]; then
      echo "schema size ${schema_size} exceeds budget ${SCHEMA_MAX_BYTES} bytes: $schema_file" >&2
      schema_failed=1
    fi
  done

  if [ "$schema_failed" -ne 0 ]; then
    echo "Update $budget_file only when growth is intentional and reviewed." >&2
    exit 1
  fi
fi

