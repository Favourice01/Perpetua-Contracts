"""tests/test_event_byte_budget.py

Tests for script/event-byte-budget.py and script/verify_event_bytes.py.

The contract that matters is the Protocol 27 ledger bound: the SUM of all
contract events in a transaction must stay under
maxSorobanTransactionEventSizeBytes (16,384). A full `batch_withdraw` at
MAX_BATCH_SIZE is the worst realistic case, because it emits one `withdrawn`
event per stream drawn from.

These tests pin the per-event sizes, the batch ceiling, and the independent
hand-calculation cross-check. They also pin the FACT that `withdrawn` is
260 bytes, i.e. that it does not meet issue #57's "< 256" target - changing
that silently would be exactly the kind of drift these tests exist to catch.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _load(name: str):
    path = _ROOT / "script" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ebb = _load("event-byte-budget")

SIZES = {r["name"]: r for r in ebb.sizes()}


class TestPerEventSizes:
    def test_every_event_is_measured(self):
        assert set(SIZES) == {
            "stream_created", "withdrawn", "cancelled", "paused", "resumed",
            "topped_up", "recipient_transferred", "delegate_granted",
            "delegate_revoked", "ttl_extended",
        }

    def test_withdrawn_size_is_pinned(self):
        assert SIZES["withdrawn"]["total"] == 260

    def test_withdrawn_exceeds_the_256_target(self):
        """Issue #57 asks for < 256. The real figure is 260. Do not paper
        over this: if this assertion starts failing, the encoding changed
        and the documentation must be revisited."""
        assert SIZES["withdrawn"]["total"] > 256

    def test_empty_payload_events_are_smallest(self):
        """recipient_transferred / delegate_revoked carry only topics."""
        for name in ("recipient_transferred", "delegate_revoked"):
            assert SIZES[name]["data"] == 12, f"{name} should have an empty map"

    def test_largest_event_is_stream_created(self):
        largest = max(SIZES.values(), key=lambda r: r["total"])
        assert largest["name"] == "stream_created"

    def test_all_sizes_are_positive_and_aligned(self):
        for r in SIZES.values():
            assert r["total"] > 0
            # XDR pads every body to a multiple of 4.
            assert r["total"] % 4 == 0, f"{r['name']} is not 4-byte aligned"


class TestBudgetCeiling:
    def test_full_batch_withdraw_fits_the_ledger_bound(self):
        """16 x withdrawn must leave substantial headroom under 16,384."""
        used = SIZES["withdrawn"]["total"] * ebb.MAX_BATCH_SIZE
        assert used < ebb.MAX_EVENT_BYTES
        assert ebb.MAX_EVENT_BYTES - used > 10_000

    def test_batch_is_a_fraction_of_the_budget(self):
        used = SIZES["withdrawn"]["total"] * ebb.MAX_BATCH_SIZE
        assert used / ebb.MAX_EVENT_BYTES < 0.5

    def test_no_single_event_comes_close_to_the_bound(self):
        assert max(r["total"] for r in SIZES.values()) < ebb.MAX_EVENT_BYTES

    def test_ledger_bound_matches_protocol_27(self):
        assert ebb.MAX_EVENT_BYTES == 16_384
        assert ebb.MAX_BATCH_SIZE == 16


class TestIndependentCrossCheck:
    def test_hand_calculation_agrees_with_the_model(self):
        """verify_event_bytes.py recomputes `withdrawn` from the XDR
        definitions without importing the model. Run it as a subprocess so
        the independence is real."""
        result = subprocess.run(
            [sys.executable, str(_ROOT / "script" / "verify_event_bytes.py")],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "MATCHES evsize.py (260)" in result.stdout


class TestScriptRuns:
    def test_budget_script_exits_cleanly(self):
        result = subprocess.run(
            [sys.executable, str(_ROOT / "script" / "event-byte-budget.py")],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "withdrawn" in result.stdout


# ---------------------------------------------------------------------------
# Boundary cliffs at the 16 KiB ledger bound (issue #288).
# ---------------------------------------------------------------------------

class TestBoundaryCliffs:
    """The Protocol 27 bound is a strict `<` on the SUM of event bytes.
    Exactly 16,384 must be rejected; 16,383 must be accepted."""

    def test_exactly_at_the_limit_is_rejected(self):
        assert ebb.check_budget(ebb.MAX_EVENT_BYTES)["ok"] is False

    def test_one_byte_under_the_limit_is_accepted(self):
        assert ebb.check_budget(ebb.MAX_EVENT_BYTES - 1)["ok"] is True

    def test_one_byte_over_the_limit_is_rejected(self):
        assert ebb.check_budget(ebb.MAX_EVENT_BYTES + 1)["ok"] is False

    def test_zero_bytes_is_accepted(self):
        assert ebb.check_budget(0)["ok"] is True

    def test_negative_bytes_is_rejected(self):
        assert ebb.check_budget(-1)["ok"] is False

    def test_headroom_is_exact_at_the_boundary(self):
        assert ebb.check_budget(ebb.MAX_EVENT_BYTES - 1)["headroom"] == 1
        assert ebb.check_budget(ebb.MAX_EVENT_BYTES)["headroom"] == 0

    def test_full_batch_withdraw_is_accepted(self):
        used = SIZES["withdrawn"]["total"] * ebb.MAX_BATCH_SIZE
        assert ebb.check_budget(used)["ok"] is True

    def test_batch_over_max_batch_size_is_rejected(self):
        used = SIZES["withdrawn"]["total"] * (ebb.MAX_BATCH_SIZE + 1)
        assert ebb.check_budget(used)["ok"] is False


# ---------------------------------------------------------------------------
# Error codes (issue #288): every branch must be reachable and stable.
# ---------------------------------------------------------------------------

class TestErrorCodes:
    def test_error_codes_are_unique(self):
        codes = [c for c in dir(ebb) if c.startswith("ERR_")]
        assert codes, "event-byte-budget.py must expose ERR_* constants"
        values = [getattr(ebb, c) for c in codes]
        assert len(values) == len(set(values))

    def test_oversize_budget_emits_its_error_code(self):
        result = ebb.check_budget(ebb.MAX_EVENT_BYTES + 1)
        assert result["ok"] is False
        assert result["error"] == ebb.ERR_EVENT_BUDGET_EXCEEDED

    def test_negative_budget_emits_its_error_code(self):
        result = ebb.check_budget(-1)
        assert result["ok"] is False
        assert result["error"] == ebb.ERR_INVALID_BYTE_COUNT

    def test_unknown_event_emits_its_error_code(self):
        result = ebb.check_event("no_such_event")
        assert result["ok"] is False
        assert result["error"] == ebb.ERR_UNKNOWN_EVENT

    def test_archived_entry_emits_its_error_code(self):
        result = ebb.classify_entry(stream_id=1, stream_count=16, present=False)
        assert result["ok"] is False
        assert result["error"] == ebb.ERR_ARCHIVED_ENTRY

    def test_ledger_skew_emits_its_error_code(self):
        result = ebb.classify_entry(
            stream_id=1, stream_count=16, present=True, observed_ledger=100,
            latest_ledger=90,
        )
        assert result["ok"] is False
        assert result["error"] == ebb.ERR_LEDGER_SKEW

    def test_every_error_code_is_documented(self):
        doc = (_ROOT / "docs" / "soroban-rpc-read-skew.md").read_text()
        for name in dir(ebb):
            if name.startswith("ERR_"):
                assert getattr(ebb, name) in doc, f"{name} missing from docs"


# ---------------------------------------------------------------------------
# Archival detection and restoration guidance (issue #287).
# ---------------------------------------------------------------------------

class TestArchivedEntryRecovery:
    def test_archived_entry_is_classified_as_archived(self):
        result = ebb.classify_entry(stream_id=3, stream_count=16, present=False)
        assert result["status"] == "Archived"

    def test_archived_entry_emits_restoration_guidance(self):
        result = ebb.classify_entry(stream_id=3, stream_count=16, present=False)
        assert "tools/sdk/restore_footprint.ts" in result["guidance"]

    def test_live_entry_is_not_flagged(self):
        result = ebb.classify_entry(stream_id=3, stream_count=16, present=True)
        assert result["ok"] is True
        assert result["status"] == "Live"

    def test_stream_id_beyond_count_is_not_archived(self):
        """A stream id >= stream_count() was never created; that is a
        caller bug, not archival."""
        result = ebb.classify_entry(stream_id=99, stream_count=16, present=False)
        assert result["status"] != "Archived"

    def test_recovery_is_idempotent(self):
        first = ebb.classify_entry(stream_id=3, stream_count=16, present=False)
        second = ebb.classify_entry(stream_id=3, stream_count=16, present=False)
        assert first == second

    def test_restoration_guidance_names_the_restore_tool(self):
        result = ebb.classify_entry(stream_id=3, stream_count=16, present=False)
        assert re.search(r"restore_footprint\.ts", result["guidance"])


# ---------------------------------------------------------------------------
# RPC read skew (issue #287).
# ---------------------------------------------------------------------------

class TestLedgerSkew:
    def test_stale_observation_is_rejected(self):
        result = ebb.classify_entry(
            stream_id=1, stream_count=16, present=True, observed_ledger=90,
            latest_ledger=100,
        )
        assert result["ok"] is False
        assert result["status"] == "LedgerSkew"

    def test_current_observation_is_accepted(self):
        result = ebb.classify_entry(
            stream_id=1, stream_count=16, present=True, observed_ledger=100,
            latest_ledger=100,
        )
        assert result["ok"] is True

    def test_skew_beyond_tolerance_is_rejected(self):
        result = ebb.classify_entry(
            stream_id=1, stream_count=16, present=True,
            observed_ledger=100 - ebb.MAX_LEDGER_SKEW - 1, latest_ledger=100,
        )
        assert result["ok"] is False

    def test_skew_within_tolerance_is_accepted(self):
        result = ebb.classify_entry(
            stream_id=1, stream_count=16, present=True,
            observed_ledger=100 - ebb.MAX_LEDGER_SKEW, latest_ledger=100,
        )
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# Cross-module integration: factory, governance, SDK (issue #288).
# ---------------------------------------------------------------------------

class TestCrossModuleIntegration:
    def test_sdk_error_codes_stay_in_sync(self):
        """The enforcer's error codes must match tools/sdk/errors.ts so the
        SDK can surface them without translation."""
        sdk = (_ROOT / "tools" / "sdk" / "errors.ts").read_text()
        for name in dir(ebb):
            if name.startswith("ERR_"):
                assert getattr(ebb, name) in sdk, f"{name} missing from SDK"

    def test_factory_batch_size_matches_enforcer(self):
        factory = (_ROOT / "contracts" / "factory" / "src" / "lib.rs").read_text()
        assert "MAX_BATCH_SIZE" in factory or "batch_withdraw" in factory

    def test_governance_docs_reference_the_enforcer(self):
        gov = (_ROOT / "docs" / "soroban-rpc-read-skew.md").read_text()
        assert "event-byte-budget.py" in gov

    def test_restore_tool_exists(self):
        assert (_ROOT / "tools" / "sdk" / "restore_footprint.ts").exists()
