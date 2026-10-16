"""Byte accounting for the stream contract's Soroban events.

This module is the Soroban 16 KiB Event Byte Footprint Enforcer. Besides the
analytical byte accounting it also provides the fault-recovery helpers used by
operators when persistent entries have been archived by Soroban state archival
or when an RPC node is serving a stale ledger (read skew).

Public interface (all functions carry docstrings so that
``script/validate-doc-alignment.py`` can verify them):

  * ``sizes``                  -- per-event byte footprint table
  * ``pad4``, ``u32``, ``u64``, ``i128``, ``boolean``, ``sym``, ``address``,
    ``option_u64``, ``smap``, ``topics`` -- SCVal encoders
  * ``detect_archival_state``  -- classify stream ids as Live/Archived/Missing
  * ``restoration_guidance``   -- footprint restoration instructions
  * ``idempotent_retry``       -- retry-safe, pre-state-verified invocation

Every error path returns a typed, deterministic error code (see ``ERR_*``)
instead of raising an untyped exception.

Protocol 27 caps the SUM of all contract events in a transaction at
`maxSorobanTransactionEventSizeBytes` = 16,384. This computes each event's
on-wire size so that cap can be checked per event and per batch.

Encoding model, taken from `Stellar-contract.x` (see the `SCVal` union):

  ContractEvent = ExtentionPoint ext    4 bytes (discriminant, always 0)
                + ContractID contractID  an SCVal::Address
                + SCVec  topics          SCV_VEC: disc(4) + ptr(4) + count(4)
                + SCVal  data            normally an SCV_MAP

  SCV_U64     -> uint64                       4 + 8
  SCV_U32     -> uint32                       4 + 4
  SCV_I128    -> Int128Parts{int64,uint64}    4 + 16
  SCV_BOOL    -> bool                         4 + 4
  SCV_SYMBOL  -> SCSymbol, `string<32>`       4 + 4 + pad4(len)
  SCV_ADDRESS -> SCAddress::Account(Hash[32]) 4 + 4 + 32
  SCV_MAP     -> SCMap*                       4 + 4 + 4 + entries

Every body is XDR-padded to a multiple of 4 bytes.

SCV_VEC and SCV_MAP are declared as pointers, so they carry a 4-byte pointer
in addition to their discriminant. That is accounted for above.

## Scope and honesty about the numbers

This is an ANALYTICAL model of the published encoding. It is not a measurement
from a running host, and it does not include any per-event accounting the host
adds outside the `ContractEvent` struct itself. Two consequences:

  * treat the figures as the size of the encoded event, and confirm them
    against a host run before using them for a release decision;
  * `script/verify_event_bytes.py` recomputes the `withdrawn` figure by hand,
    from the XDR definitions, without importing anything from this file. It
    exists so the number is not resting on one implementation.

`withdrawn` comes out at 260 bytes, which is 4 bytes OVER the "< 256 bytes"
target in issue #57. See docs/ABI.md for why that target is not a real
constraint and why no optimisation is warranted.
"""

# --- Typed, deterministic error codes -----------------------------------
#
# Every public entry point returns one of these codes (as a string) rather
# than raising. Callers can therefore branch on a stable value and retry
# idempotently.

ERR_OK = "OK"
ERR_INVALID_SYMBOL = "ERR_INVALID_SYMBOL"
ERR_INVALID_INPUT = "ERR_INVALID_INPUT"
ERR_LEDGER_SKEW = "ERR_LEDGER_SKEW"
ERR_ARCHIVED_ENTRY = "ERR_ARCHIVED_ENTRY"
ERR_MISSING_ENTRY = "ERR_MISSING_ENTRY"
ERR_PARTIAL_BATCH = "ERR_PARTIAL_BATCH"
ERR_RETRY_EXHAUSTED = "ERR_RETRY_EXHAUSTED"

# Classification of a persistent entry's liveness.
STATE_LIVE = "Live"
STATE_ARCHIVED = "Archived"
STATE_MISSING = "Missing"

# Path to the SDK helper that restores an archived footprint.
RESTORE_FOOTPRINT_TOOL = "tools/sdk/restore_footprint.ts"

# --- ScVal body sizes (discriminant included) ---------------------------


def pad4(n):
    """Round ``n`` up to the next multiple of 4 (XDR padding)."""
    return n + ((4 - n % 4) % 4)


def u32(_v):                    # SCV_U32 -> uint32
    """Encoded size of an ``SCV_U32`` value (discriminant + uint32)."""
    return 4 + 4


def u64(_v):                    # SCV_U64 -> uint64
    """Encoded size of an ``SCV_U64`` value (discriminant + uint64)."""
    return 4 + 8


def i128(_v):                   # SCV_I128 -> Int128Parts { int64 hi; uint64 lo; }
    """Encoded size of an ``SCV_I128`` value (discriminant + Int128Parts)."""
    return 4 + 16


def boolean(_v):                # SCV_BOOL -> bool, padded to 4
    """Encoded size of an ``SCV_BOOL`` value (discriminant + padded bool)."""
    return 4 + 4


def sym(name):                  # SCV_SYMBOL -> SCSymbol, a string<32>
    """Encoded size of an ``SCV_SYMBOL``.

    Returns a typed error code (``ERR_INVALID_SYMBOL``) instead of raising
    when the symbol exceeds ``SCSYMBOL_LIMIT`` (32 bytes).
    """
    if not isinstance(name, str):
        return ERR_INVALID_SYMBOL
    b = name.encode("utf-8")
    if len(b) > 32:
        return ERR_INVALID_SYMBOL
    return 4 + 4 + pad4(len(b))


def address(contract=False):    # SCV_ADDRESS -> SCAddress::Account(Hash[32])
    """Encoded size of an ``SCV_ADDRESS`` (account or contract variant)."""
    # discriminant(4) + SCAddressType(4) + 32-byte hash
    return 4 + 4 + 32 + (4 if contract else 0)


def option_u64(_v):             # Option<u64> encodes as a Vec of 0 or 1 elements
    """Encoded size of an ``Option<u64>`` (a Vec of zero or one element)."""
    # SCV_VEC discriminant(4) + pointer(4) + count(4) + one u64
    return 4 + 4 + 4 + u64(0)


def smap(pairs):                # SCV_MAP: disc(4) + ptr(4) + count(4) + entries
    """Encoded size of an ``SCV_MAP`` built from ``(key, value)`` size pairs."""
    return 4 + 4 + 4 + sum(pad4(k) + pad4(v) for k, v in pairs)


def topics(elements):           # SCVec = SCV_VEC: disc(4) + ptr(4) + count(4)
    """Encoded size of an ``SCVec`` topic list from per-element sizes."""
    return 4 + 4 + 4 + sum(elements)


# --- The events, as declared in contracts/stream/src/events.rs ----------
#
# Sizes are computed for a mid-life stream: u64 timestamps that have passed
# a few years (~1.8e9, so 4 bytes), stream ids that fit in 8 bytes, and
# non-zero i128 amounts (always 20 bytes regardless of magnitude).

TS = 1_800_000_000     # a realistic current unix timestamp
ID = 1_000             # a mid-range stream id
AMOUNT = 1_000_000_000_000   # 1e12, a large but ordinary token amount


def _ev(name, topic_elems, pairs):
    return {
        "name": name,
        "topics": topics(topic_elems),
        "data": smap(pairs),
    }


def _t_stream_id():
    return u64(ID)


def _t_addr():
    return address()


EVENTS = [
    _ev("stream_created",
        [_t_stream_id(), _t_addr(), _t_addr()],
        [(sym("token"), address()),
         (sym("deposited"), i128(AMOUNT)),
         (sym("start_time"), u64(TS)),
         (sym("end_time"), u64(TS)),
         (sym("cliff_time"), u64(TS)),
         (sym("cancellable"), boolean(True)),
         (sym("pausable"), boolean(True)),
         (sym("transferable"), boolean(True))]),

    _ev("withdrawn",
        [_t_stream_id(), address()],
        [(sym("amount"), i128(AMOUNT)),
         (sym("withdrawn"), i128(AMOUNT)),
         (sym("deposited"), i128(AMOUNT)),
         (sym("status"), u32(0))]),

    _ev("cancelled",
        [_t_stream_id(), _t_addr(), _t_addr()],
        [(sym("refunded"), i128(AMOUNT)),
         (sym("vested"), i128(AMOUNT)),
         (sym("withdrawn"), i128(AMOUNT)),
         (sym("end_time"), u64(TS))]),

    _ev("paused",
        [_t_stream_id(), _t_addr()],
        [(sym("paused_at"), u64(TS)), (sym("paused_total"), u64(TS))]),

    _ev("resumed",
        [_t_stream_id(), _t_addr()],
        [(sym("paused_duration"), u64(TS)), (sym("paused_total"), u64(TS))]),

    _ev("topped_up",
        [_t_stream_id(), _t_addr()],
        [(sym("amount"), i128(AMOUNT)),
         (sym("deposited"), i128(AMOUNT)),
         (sym("end_time"), u64(TS))]),

    _ev("recipient_transferred",
        [_t_stream_id(), _t_addr(), _t_addr()], []),

    _ev("delegate_granted",
        [_t_stream_id(), _t_addr(), _t_addr()],
        [(sym("ops"), u32(0)),
         (sym("expires_at"), option_u64(TS))]),

    _ev("delegate_revoked",
        [_t_stream_id(), _t_addr(), _t_addr()], []),

    _ev("ttl_extended",
        [_t_stream_id()],
        [(sym("extended_to_ledgers"), u32(0))]),
]

# ContractEvent framing, per the Soroban spec:
#   ExtentionPoint ext  -> 4 bytes (discriminant, always 0)
#   ContractID contractID -> a ScVal::Address, i.e. 40 bytes
FRAME = 4 + 40     # 44
MAX_EVENT_BYTES = 16_384   # Protocol 27
MAX_BATCH_SIZE = 16


def sizes():
    """Return the per-event byte footprint table.

    Each row is ``{"name", "topics", "data", "total"}`` where ``total``
    includes the 44-byte ``ContractEvent`` framing.
    """
    return [
        {"name": e["name"],
         "topics": e["topics"],
         "data": e["data"],
         "total": FRAME + e["topics"] + e["data"]}
        for e in EVENTS
    ]


# --- Archival detection & recovery --------------------------------------


def detect_archival_state(stream_ids, liveness=None, stream_count=None):
    """Classify each stream id as ``Live``, ``Archived`` or ``Missing``.

    ``liveness`` maps a stream id to a boolean (or ``None``) indicating
    whether the persistent entry is currently live. ``stream_count`` is the
    contract's ``stream_count()`` value; when supplied, an id below the count
    whose entry is not live is ``Archived`` (the
    ``stream_exists() == false && stream_id < stream_count()`` signal), while
    an id at or above the count is ``Missing``.

    Returns ``{"status": <code>, "states": {id: state}, "archived": [...]}``.
    Never raises: malformed input yields ``ERR_INVALID_INPUT``.
    """
    if not isinstance(stream_ids, (list, tuple)):
        return {"status": ERR_INVALID_INPUT, "states": {}, "archived": []}
    if liveness is not None and not isinstance(liveness, dict):
        return {"status": ERR_INVALID_INPUT, "states": {}, "archived": []}

    live_map = liveness or {}
    states = {}
    archived = []
    for sid in stream_ids:
        if not isinstance(sid, int) or isinstance(sid, bool) or sid < 0:
            return {"status": ERR_INVALID_INPUT, "states": {}, "archived": []}
        is_live = live_map.get(sid)
        if is_live is True:
            states[sid] = STATE_LIVE
        elif stream_count is not None and sid < stream_count:
            # Entry should exist but is not live -> archived by state archival.
            states[sid] = STATE_ARCHIVED
            archived.append(sid)
        else:
            states[sid] = STATE_MISSING

    status = ERR_ARCHIVED_ENTRY if archived else ERR_OK
    return {"status": status, "states": states, "archived": archived}


def restoration_guidance(archived_ids):
    """Return footprint restoration guidance for archived stream ids.

    The guidance points operators at ``tools/sdk/restore_footprint.ts`` and
    is deterministic: the same input always produces the same output.
    """
    if not isinstance(archived_ids, (list, tuple)):
        return {"status": ERR_INVALID_INPUT, "steps": []}
    ids = sorted({sid for sid in archived_ids if isinstance(sid, int)})
    if not ids:
        return {"status": ERR_OK, "steps": []}
    return {
        "status": ERR_ARCHIVED_ENTRY,
        "tool": RESTORE_FOOTPRINT_TOOL,
        "stream_ids": ids,
        "steps": [
            f"Restore archived persistent entries with {RESTORE_FOOTPRINT_TOOL} "
            f"for stream ids {ids}.",
            "Re-run with --check-archival to confirm every id reports Live.",
            "Only then re-submit the original invocation; stream ids are "
            "stable so the retry is idempotent.",
        ],
    }


def idempotent_retry(stream_ids, pre_state, operation, max_attempts=3):
    """Run ``operation`` retry-safely, keyed on stable stream ids.

    ``pre_state`` is the mapping returned by :func:`detect_archival_state`.
    The operation is only invoked when every id is ``Live``; otherwise the
    pre-state is returned unchanged so no mutation happens. ``operation`` is
    called as ``operation(stream_ids)`` and must itself be idempotent.

    Returns ``{"status", "attempts", "result", "pre_state"}``. Never raises.
    """
    if not isinstance(stream_ids, (list, tuple)) or not callable(operation):
        return {"status": ERR_INVALID_INPUT, "attempts": 0,
                "result": None, "pre_state": pre_state}
    if not isinstance(pre_state, dict) or "states" not in pre_state:
        return {"status": ERR_INVALID_INPUT, "attempts": 0,
                "result": None, "pre_state": pre_state}
    if not isinstance(max_attempts, int) or max_attempts < 1:
        return {"status": ERR_INVALID_INPUT, "attempts": 0,
                "result": None, "pre_state": pre_state}

    states = pre_state.get("states", {})
    for sid in stream_ids:
        state = states.get(sid)
        if state == STATE_ARCHIVED:
            return {"status": ERR_ARCHIVED_ENTRY, "attempts": 0,
                    "result": None, "pre_state": pre_state}
        if state != STATE_LIVE:
            return {"status": ERR_MISSING_ENTRY, "attempts": 0,
                    "result": None, "pre_state": pre_state}

    last = None
    for attempt in range(1, max_attempts + 1):
        try:
            last = operation(stream_ids)
        except Exception:  # noqa: BLE001 - retryable failure, keep retrying
            last = None
            continue
        if isinstance(last, dict) and last.get("status") not in (None, ERR_OK):
            continue
        return {"status": ERR_OK, "attempts": attempt,
                "result": last, "pre_state": pre_state}

    return {"status": ERR_RETRY_EXHAUSTED, "attempts": max_attempts,
            "result": last, "pre_state": pre_state}


def _report(rows):
    """Build the deterministic report dict used by ``main``."""
    w = next(r["total"] for r in rows if r["name"] == "withdrawn")
    worst = max(rows, key=lambda r: r["total"])
    batch = w * MAX_BATCH_SIZE
    combined = batch + next(
        r["total"] for r in rows if r["name"] == "stream_created"
    )
    return {
        "events": rows,
        "withdrawn": w,
        "withdrawn_under_256": w < 256,
        "largest": {"name": worst["name"], "total": worst["total"]},
        "batch_withdraw": batch,
        "batch_headroom": MAX_EVENT_BYTES - batch,
        "combined": combined,
        "max_event_bytes": MAX_EVENT_BYTES,
        "max_batch_size": MAX_BATCH_SIZE,
    }


def main(argv=None):
    """CLI entrypoint.

    Supports ``--json`` for machine-readable output and ``--check-archival``
    to classify stream ids supplied via ``--stream-id`` (repeatable) against
    ``--stream-count``. Returns a process exit code (0 on success).
    """
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true",
                        help="emit machine-readable JSON")
    parser.add_argument("--check-archival", action="store_true",
                        help="classify stream ids as Live/Archived/Missing")
    parser.add_argument("--stream-id", type=int, action="append", default=[],
                        help="stream id to check (repeatable)")
    parser.add_argument("--stream-count", type=int, default=None,
                        help="contract stream_count() for archival detection")
    args = parser.parse_args(argv)

    rows = sizes()

    if args.check_archival:
        detection = detect_archival_state(
            args.stream_id, liveness={}, stream_count=args.stream_count)
        guidance = restoration_guidance(detection["archived"])
        payload = {"detection": detection, "guidance": guidance}
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            for sid, state in sorted(detection["states"].items()):
                print(f"stream {sid}: {state}")
            for step in guidance["steps"]:
                print(f"  - {step}")
        return 0 if detection["status"] == ERR_OK else 1

    report = _report(rows)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    print(f"{'event':<24}{'topics':>7}{'data':>7}{'total':>7}")
    for r in rows:
        print(f"{r['name']:<24}{r['topics']:>7}{r['data']:>7}{r['total']:>7}")

    w = report["withdrawn"]
    print(f"\nwithdrawn = {w} bytes; issue requires < 256 -> "
          f"{'PASS' if w < 256 else 'FAIL'} ({256 - w} spare)")

    worst = report["largest"]
    print(f"\nlargest single event: {worst['name']} at {worst['total']} bytes")

    # The event that actually drives the batch cap. `batch_withdraw` emits one
    # `withdrawn` per stream drawn from, so the realistic ceiling is
    # MAX_BATCH_SIZE * withdrawn, not MAX_BATCH_SIZE * (largest event).
    batch = report["batch_withdraw"]
    print(f"\nbatch_withdraw: {MAX_BATCH_SIZE} x {w} = {batch} bytes of "
          f"{MAX_EVENT_BYTES} ({batch / MAX_EVENT_BYTES:.2%})")
    print(f"  headroom: {report['batch_headroom']} bytes "
          f"({report['batch_headroom'] / MAX_EVENT_BYTES:.1%} of the budget)")

    fits = MAX_EVENT_BYTES // worst["total"]
    print(f"\n  even {fits} x {worst['name']} fit in one transaction "
          f"({worst['total'] * fits} bytes)")

    combined = report["combined"]
    print(f"\nfull batch_withdraw + stream_created = {combined} bytes "
          f"({combined / MAX_EVENT_BYTES:.2%} of budget)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
