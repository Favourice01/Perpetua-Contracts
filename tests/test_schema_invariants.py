import os
import re
import random
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRAPHQL_PATH = os.path.join(REPO_ROOT, "schema", "schema.graphql")
SQL_PATH = os.path.join(REPO_ROOT, "schema", "schema.sql")


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def graphql_text():
    return _read(GRAPHQL_PATH)


@pytest.fixture(scope="module")
def sql_text():
    return _read(SQL_PATH)


# ---------------------------------------------------------------------------
# Invariant documentation presence
# ---------------------------------------------------------------------------

DOCUMENTED_INVARIANTS = [
    "zero-duration",
    "i128::MAX",
    "same-ledger",
    "inverted",
    "Cancelled",
    "ttl_extended",
]


@pytest.mark.parametrize("invariant", DOCUMENTED_INVARIANTS)
def test_graphql_documents_invariant(graphql_text, invariant):
    assert invariant in graphql_text, (
        f"Invariant '{invariant}' must be documented in schema/schema.graphql"
    )


@pytest.mark.parametrize("invariant", DOCUMENTED_INVARIANTS)
def test_sql_documents_invariant(sql_text, invariant):
    assert invariant in sql_text, (
        f"Invariant '{invariant}' must be documented in schema/schema.sql"
    )


def test_graphql_has_check_constraints(graphql_text):
    assert "CHECK" in graphql_text, (
        "schema/schema.graphql must express invariants as CHECK constraints"
    )


def test_sql_has_check_constraints(sql_text):
    assert "CHECK" in sql_text, (
        "schema/schema.sql must express invariants as CHECK constraints"
    )


def test_cancelled_is_sticky_documented_both(graphql_text, sql_text):
    for text, name in ((graphql_text, "graphql"), (sql_text, "sql")):
        lowered = text.lower()
        assert "cancelled" in lowered
        assert "sticky" in lowered or "terminal" in lowered, (
            f"Cancelled-is-sticky rule must be documented in schema.{name}"
        )


def test_ttl_extended_idempotency_documented_both(graphql_text, sql_text):
    for text, name in ((graphql_text, "graphql"), (sql_text, "sql")):
        lowered = text.lower()
        assert "ttl_extended" in lowered
        assert "idempotent" in lowered or "idempotency" in lowered, (
            f"ttl_extended idempotency rule must be documented in schema.{name}"
        )


# ---------------------------------------------------------------------------
# Boundary / adversarial validation helpers
# ---------------------------------------------------------------------------

I128_MAX = 2**127 - 1
I128_MIN = -(2**127)


class InvariantViolation(Exception):
    """Raised when a degenerate parameter set is not rejected."""


def validate_cliff(start_ledger, cliff_ledger, end_ledger):
    """Reject inverted ranges and zero-duration cliffs."""
    if start_ledger < 0 or cliff_ledger < 0 or end_ledger < 0:
        raise InvariantViolation("ledger values must be non-negative")
    if not (start_ledger <= cliff_ledger <= end_ledger):
        raise InvariantViolation("inverted time range")
    if cliff_ledger == start_ledger:
        raise InvariantViolation("zero-duration cliff")
    if end_ledger == start_ledger:
        raise InvariantViolation("zero-duration stream")
    return True


def validate_deposit_rate(rate):
    """Reject deposit rates outside the i128 range or non-positive."""
    if not isinstance(rate, int):
        raise InvariantViolation("deposit rate must be an integer")
    if rate <= 0:
        raise InvariantViolation("deposit rate must be positive")
    if rate > I128_MAX:
        raise InvariantViolation("deposit rate exceeds i128::MAX")
    if rate < I128_MIN:
        raise InvariantViolation("deposit rate below i128::MIN")
    return True


def validate_state_transition(prev_ledger, next_ledger, prev_status, next_status):
    """Reject same-ledger state transitions and illegal status moves."""
    if next_ledger < prev_ledger:
        raise InvariantViolation("inverted ledger ordering")
    if next_ledger == prev_ledger:
        raise InvariantViolation("same-ledger state transition")
    if prev_status == "Cancelled":
        raise InvariantViolation("Cancelled is sticky; no further transitions")
    return True


# ---------------------------------------------------------------------------
# Deterministic unit tests
# ---------------------------------------------------------------------------

def test_zero_duration_cliff_rejected():
    with pytest.raises(InvariantViolation):
        validate_cliff(100, 100, 200)


def test_zero_duration_stream_rejected():
    with pytest.raises(InvariantViolation):
        validate_cliff(100, 150, 100)


def test_inverted_time_range_rejected():
    with pytest.raises(InvariantViolation):
        validate_cliff(200, 150, 100)


def test_valid_time_range_accepted():
    assert validate_cliff(100, 150, 200) is True


def test_i128_max_deposit_rate_accepted():
    assert validate_deposit_rate(I128_MAX) is True


def test_deposit_rate_above_i128_max_rejected():
    with pytest.raises(InvariantViolation):
        validate_deposit_rate(I128_MAX + 1)


def test_deposit_rate_below_i128_min_rejected():
    with pytest.raises(InvariantViolation):
        validate_deposit_rate(I128_MIN - 1)


def test_zero_deposit_rate_rejected():
    with pytest.raises(InvariantViolation):
        validate_deposit_rate(0)


def test_negative_deposit_rate_rejected():
    with pytest.raises(InvariantViolation):
        validate_deposit_rate(-1)


def test_same_ledger_transition_rejected():
    with pytest.raises(InvariantViolation):
        validate_state_transition(500, 500, "Active", "Completed")


def test_cancelled_is_sticky():
    with pytest.raises(InvariantViolation):
        validate_state_transition(500, 501, "Cancelled", "Active")


def test_valid_state_transition_accepted():
    assert validate_state_transition(500, 501, "Active", "Completed") is True


# ---------------------------------------------------------------------------
# Property-based style regression tests
# ---------------------------------------------------------------------------

def _random_ledger():
    return random.randint(0, 10**9)


def test_property_inverted_ranges_always_rejected():
    for _ in range(500):
        a = _random_ledger()
        b = _random_ledger()
        c = _random_ledger()
        start, cliff, end = sorted([a, b, c])
        # Force an inverted range.
        with pytest.raises(InvariantViolation):
            validate_cliff(end, cliff, start)


def test_property_zero_duration_cliffs_always_rejected():
    for _ in range(500):
        start = _random_ledger()
        end = start + random.randint(1, 10**6)
        with pytest.raises(InvariantViolation):
            validate_cliff(start, start, end)


def test_property_deposit_rates_out_of_range_rejected():
    for _ in range(500):
        rate = I128_MAX + random.randint(1, 10**6)
        with pytest.raises(InvariantViolation):
            validate_deposit_rate(rate)
        rate = I128_MIN - random.randint(1, 10**6)
        with pytest.raises(InvariantViolation):
            validate_deposit_rate(rate)


def test_property_same_ledger_transitions_rejected():
    for _ in range(500):
        ledger = _random_ledger()
        with pytest.raises(InvariantViolation):
            validate_state_transition(ledger, ledger, "Active", "Completed")


def test_property_valid_ranges_never_raise():
    for _ in range(500):
        start = _random_ledger()
        cliff = start + random.randint(1, 10**6)
        end = cliff + random.randint(1, 10**6)
        assert validate_cliff(start, cliff, end) is True


def test_property_valid_deposit_rates_never_raise():
    for _ in range(500):
        rate = random.randint(1, I128_MAX)
        assert validate_deposit_rate(rate) is True


def test_property_no_silent_wrapping_on_overflow():
    """Ensure overflow is rejected rather than silently wrapping."""
    for _ in range(500):
        rate = I128_MAX + random.randint(1, 10**6)
        with pytest.raises(InvariantViolation):
            validate_deposit_rate(rate)
        # A wrapped value would be negative; ensure we never accept it.
        wrapped = rate - 2**128
        if wrapped <= 0:
            with pytest.raises(InvariantViolation):
                validate_deposit_rate(wrapped)
</｜｜DSML｜｜ parameter>
</｜｜DSML｜｜ invoke>
</｜｜DSML｜｜ calls>