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

_ROOT = Path(__file__).resolve().parent.parent

_SCHEMA = _ROOT / "schema" / "schema.graphql"
_WASM_SIZE_SCRIPT = _ROOT / "script" / "check-stream-wasm-size.sh"


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


def _schema_event_types() -> set:
    """Event type names declared in schema/schema.graphql.

    The off-chain schema mirrors the on-chain event set; every event the
    contract can emit must have a byte-budget entry so the ledger bound
    stays verifiable end to end.
    """
    text = _SCHEMA.read_text()
    return set(re.findall(r"^\s*type\s+(\w+Event)\b", text, re.MULTILINE))


class TestSchemaParity:
    """Issue #295/#296: the off-chain schema must not drift from the
    byte-budget model. A new event type added to the schema without a
    matching budget entry is a hard failure."""

    def test_schema_declares_event_types(self):
        assert _schema_event_types(), "no *Event types found in schema.graphql"

    def test_every_schema_event_has_a_budget_entry(self):
        missing = sorted(_schema_event_types() - set(SIZES))
        assert not missing, (
            "schema/schema.graphql declares event types with no byte-budget "
            f"entry: {missing}. Add them to script/event-byte-budget.py."
        )

    def test_no_budget_entry_is_orphaned(self):
        """The reverse direction: a budget entry with no schema type means
        the schema dropped an event the model still measures."""
        orphaned = sorted(set(SIZES) - _schema_event_types())
        assert not orphaned, (
            f"byte-budget entries with no schema event type: {orphaned}"
        )


class TestStrictUpperBoundCaps:
    """Pin the strict upper-bound caps for the off-chain schema."""

    def test_batch_withdraw_plus_stream_created_under_cap(self):
        """Worst realistic combination: a full batch_withdraw (16 x
        withdrawn) plus the stream_created that opened the batch."""
        used = (
            SIZES["withdrawn"]["total"] * ebb.MAX_BATCH_SIZE
            + SIZES["stream_created"]["total"]
        )
        assert used < ebb.MAX_EVENT_BYTES, (
            f"batch_withdraw + stream_created = {used} bytes, "
            f"over the {ebb.MAX_EVENT_BYTES}-byte Protocol 27 cap"
        )

    def test_combination_cap_is_16384(self):
        assert ebb.MAX_EVENT_BYTES == 16_384

    def test_combination_leaves_headroom(self):
        used = (
            SIZES["withdrawn"]["total"] * ebb.MAX_BATCH_SIZE
            + SIZES["stream_created"]["total"]
        )
        assert ebb.MAX_EVENT_BYTES - used > 9_000


class TestWasmSizeBudget:
    def test_wasm_size_check_script_exists(self):
        assert _WASM_SIZE_SCRIPT.exists(), (
            f"missing {_WASM_SIZE_SCRIPT}; the WASM size budget check must "
            "remain runnable in CI"
        )

    def test_wasm_size_check_passes(self):
        result = subprocess.run(
            ["bash", str(_WASM_SIZE_SCRIPT)],
            capture_output=True,
            text=True,
            cwd=str(_ROOT),
        )
        assert result.returncode == 0, result.stdout + result.stderr
