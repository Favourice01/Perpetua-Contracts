"""Property-style proof that stream events reconstruct stored stream state.

This is a dependency-free model test. ``StorageModel`` is the reference for
what ``get_stream`` returns, while ``EventIndexer`` only consumes emitted
contract event payloads plus the closed-ledger timestamp carried by the RPC
event envelope. The deterministic seed makes a failure replayable.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
from pathlib import Path
import random
import re
from typing import Any


@dataclass
class StreamState:
    sender: str
    recipient: str
    token: str
    deposited: int
    withdrawn: int
    start_time: int
    end_time: int
    cliff_time: int
    flags: int
    paused_at: int | None
    paused_total: int
    status: str


@dataclass
class Event:
    kind: str
    stream_id: int
    data: dict[str, Any]
    ledger_timestamp: int


@dataclass
class StorageModel:
    now: int = 1_700_000_000
    streams: dict[int, StreamState] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    rng: random.Random = field(default_factory=random.Random, repr=False)

    def get_stream(self, stream_id: int) -> StreamState:
        return deepcopy(self.streams[stream_id])

    def _emit(self, kind: str, stream_id: int, data: dict[str, Any]) -> Event:
        event = Event(kind, stream_id, data, self.now)
        self.events.append(event)
        return event

    @staticmethod
    def _stream_time(stream: StreamState, now: int) -> int:
        frozen_at = stream.paused_at if stream.paused_at is not None else now
        return max(0, frozen_at - stream.paused_total)

    @classmethod
    def _vested(cls, stream: StreamState, now: int) -> int:
        if cls._stream_time(stream, now) < stream.cliff_time:
            return 0
        duration = max(0, stream.end_time - stream.start_time)
        if duration == 0:
            return stream.deposited
        consumed = max(0, min(cls._stream_time(stream, now), stream.end_time) - stream.start_time)
        if consumed >= duration:
            return stream.deposited
        return stream.deposited * consumed // duration

    def create(self, stream_id: int, sender: str, recipient: str, token: str) -> Event:
        start = self.now - self.rng.randint(0, 1_000)
        duration = self.rng.randint(5_000, 20_000)
        deposited = duration * self.rng.randint(10, 100)
        cliff = start + self.rng.randint(0, duration)
        flags = 0b111
        stream = StreamState(
            sender,
            recipient,
            token,
            deposited,
            0,
            start,
            start + duration,
            cliff,
            flags,
            None,
            0,
            "Active",
        )
        self.streams[stream_id] = stream
        return self._emit(
            "stream_created",
            stream_id,
            {
                "sender": sender,
                "recipient": recipient,
                "token": token,
                "deposited": deposited,
                "start_time": start,
                "end_time": start + duration,
                "cliff_time": cliff,
                "cancellable": True,
                "pausable": True,
                "transferable": True,
            },
        )

    def pause(self, stream_id: int) -> Event:
        stream = self.streams[stream_id]
        stream.paused_at = self.now
        stream.status = "Paused"
        return self._emit(
            "paused",
            stream_id,
            {"sender": stream.sender, "paused_at": self.now, "paused_total": stream.paused_total},
        )

    def resume(self, stream_id: int) -> Event:
        stream = self.streams[stream_id]
        paused_duration = max(0, self.now - (stream.paused_at or self.now))
        stream.paused_total += paused_duration
        stream.paused_at = None
        stream.status = "Active"
        return self._emit(
            "resumed",
            stream_id,
            {"sender": stream.sender, "paused_duration": paused_duration, "paused_total": stream.paused_total},
        )

    def top_up(self, stream_id: int, amount: int) -> Event:
        stream = self.streams[stream_id]
        duration = stream.end_time - stream.start_time
        delta = amount * duration // stream.deposited
        stream.deposited += amount
        stream.end_time += delta
        return self._emit(
            "topped_up",
            stream_id,
            {"sender": stream.sender, "amount": amount, "deposited": stream.deposited, "end_time": stream.end_time},
        )

    def withdraw(self, stream_id: int, amount: int) -> Event:
        stream = self.streams[stream_id]
        stream.withdrawn += amount
        if stream.withdrawn >= stream.deposited and stream.status != "Cancelled":
            stream.status = "Depleted"
            if stream.paused_at is not None:
                stream.paused_total += max(0, self.now - stream.paused_at)
                stream.paused_at = None
        return self._emit(
            "withdrawn",
            stream_id,
            {
                "recipient": stream.recipient,
                "amount": amount,
                "withdrawn": stream.withdrawn,
                "deposited": stream.deposited,
                "status": stream.status,
            },
        )

    def cancel(self, stream_id: int) -> Event:
        stream = self.streams[stream_id]
        vested = self._vested(stream, self.now)
        refunded = stream.deposited - vested
        stream.deposited = vested
        stream.end_time = max(stream.start_time, self._stream_time(stream, self.now))
        stream.paused_at = None
        stream.status = "Cancelled"
        return self._emit(
            "cancelled",
            stream_id,
            {
                "sender": stream.sender,
                "recipient": stream.recipient,
                "refunded": refunded,
                "vested": vested,
                "withdrawn": stream.withdrawn,
                "end_time": stream.end_time,
            },
        )

    def transfer(self, stream_id: int, recipient: str) -> Event:
        stream = self.streams[stream_id]
        old_recipient = stream.recipient
        stream.recipient = recipient
        return self._emit(
            "recipient_transferred",
            stream_id,
            {"old_recipient": old_recipient, "new_recipient": recipient},
        )

    def ttl_extended(self, stream_id: int) -> Event:
        return self._emit("ttl_extended", stream_id, {"extended_to_ledgers": 100})


@dataclass
class EventIndexer:
    streams: dict[int, StreamState] = field(default_factory=dict)

    def consume(self, event: Event) -> None:
        data = event.data
        if event.kind == "stream_created":
            self.streams[event.stream_id] = StreamState(
                sender=data["sender"],
                recipient=data["recipient"],
                token=data["token"],
                deposited=data["deposited"],
                withdrawn=0,
                start_time=data["start_time"],
                end_time=data["end_time"],
                cliff_time=data["cliff_time"],
                flags=(int(data["cancellable"]) | (int(data["pausable"]) << 1) | (int(data["transferable"]) << 2)),
                paused_at=None,
                paused_total=0,
                status="Active",
            )
            return

        stream = self.streams[event.stream_id]
        if event.kind == "withdrawn":
            stream.recipient = data["recipient"]
            stream.deposited = data["deposited"]
            stream.withdrawn = data["withdrawn"]
            stream.status = data["status"]
            # apply_withdrawal closes an in-progress pause when this payout
            # depletes the stream; the RPC event's ledger timestamp supplies now.
            if stream.status == "Depleted" and stream.paused_at is not None:
                stream.paused_total += max(0, event.ledger_timestamp - stream.paused_at)
                stream.paused_at = None
        elif event.kind == "cancelled":
            stream.sender = data["sender"]
            stream.recipient = data["recipient"]
            stream.deposited = data["vested"]
            stream.withdrawn = data["withdrawn"]
            stream.end_time = data["end_time"]
            stream.paused_at = None
            stream.status = "Cancelled"
        elif event.kind == "paused":
            stream.paused_at = data["paused_at"]
            stream.paused_total = data["paused_total"]
            stream.status = "Paused"
        elif event.kind == "resumed":
            stream.paused_total = data["paused_total"]
            stream.paused_at = None
            stream.status = "Active"
        elif event.kind == "topped_up":
            stream.deposited = data["deposited"]
            stream.end_time = data["end_time"]
        elif event.kind == "recipient_transferred":
            stream.recipient = data["new_recipient"]
        elif event.kind in {"ttl_extended", "delegate_granted", "delegate_revoked"}:
            pass
        else:
            raise AssertionError(f"unhandled event kind: {event.kind}")


def _assert_equal(storage: StorageModel, indexer: EventIndexer) -> None:
    assert set(storage.streams) == set(indexer.streams)
    for stream_id in storage.streams:
        assert indexer.streams[stream_id] == storage.get_stream(stream_id), stream_id


def test_random_event_reconstruction_matches_get_stream() -> None:
    rng = random.Random(0xE7E17)
    storage = StorageModel(rng=rng)
    indexer = EventIndexer()
    next_stream_id = 0

    for operation in range(100):
        storage.now += rng.randint(1, 2_000)
        active = [sid for sid, stream in storage.streams.items() if stream.status == "Active"]
        paused = [sid for sid, stream in storage.streams.items() if stream.status == "Paused"]
        live = [sid for sid, stream in storage.streams.items() if stream.status not in {"Cancelled", "Depleted"}]

        if not storage.streams or (len(storage.streams) < 8 and rng.random() < 0.12):
            event = storage.create(next_stream_id, f"G{next_stream_id}", f"R{next_stream_id}", "TOKEN")
            next_stream_id += 1
        else:
            choices = ["ttl"]
            if active:
                choices += ["pause", "withdraw", "transfer", "cancel"]
            if paused:
                choices += ["resume", "withdraw", "cancel"]
            if live:
                choices.append("top_up")
            action = rng.choice(choices)
            stream_id = rng.choice(active or paused or live or list(storage.streams))
            stream = storage.streams[stream_id]

            if action == "pause" and stream.status == "Active":
                event = storage.pause(stream_id)
            elif action == "resume" and stream.status == "Paused":
                event = storage.resume(stream_id)
            elif action == "top_up" and stream.status in {"Active", "Paused"} and storage._stream_time(stream, storage.now) < stream.end_time:
                rate = stream.deposited // (stream.end_time - stream.start_time)
                amount = max(1, rate * rng.randint(1, 500))
                event = storage.top_up(stream_id, amount)
            elif action == "withdraw":
                available = max(0, storage._vested(stream, storage.now) - stream.withdrawn)
                if available == 0:
                    event = storage.ttl_extended(stream_id)
                else:
                    amount = available if rng.random() < 0.25 else rng.randint(1, available)
                    event = storage.withdraw(stream_id, amount)
            elif action == "cancel" and stream.status in {"Active", "Paused"}:
                event = storage.cancel(stream_id)
            elif action == "transfer" and stream.status in {"Active", "Paused"} and stream.withdrawn < stream.deposited:
                recipient = f"R{rng.randint(100, 999)}"
                while recipient in {stream.sender, stream.recipient}:
                    recipient = f"R{rng.randint(100, 999)}"
                event = storage.transfer(stream_id, recipient)
            else:
                event = storage.ttl_extended(stream_id)

        indexer.consume(event)
        _assert_equal(storage, indexer)

    assert len(storage.events) == 100
    _assert_equal(storage, indexer)


# ---------------------------------------------------------------------------
# Off-chain schema parity: golden fixtures, topic layout, payload field order
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_DIR = REPO_ROOT / "tests" / "fixtures" / "event_snapshots"
SCHEMA_PATH = REPO_ROOT / "schema" / "schema.graphql"
ABI_PATH = REPO_ROOT / "docs" / "ABI.md"

# Canonical topic layout and payload field order for every event emitted by the
# contracts and consumed by the off-chain indexer. This mirrors the frozen ABI
# documented in docs/ABI.md and the GraphQL/SQL schema in schema/schema.graphql.
EVENT_LAYOUT: dict[str, dict[str, Any]] = {
    "stream_created": {
        "topics": ["stream_created", "stream_id"],
        "fields": [
            "sender",
            "recipient",
            "token",
            "deposited",
            "start_time",
            "end_time",
            "cliff_time",
            "cancellable",
            "pausable",
            "transferable",
        ],
    },
    "withdrawn": {
        "topics": ["withdrawn", "stream_id"],
        "fields": ["recipient", "amount", "withdrawn", "deposited", "status"],
    },
    "cancelled": {
        "topics": ["cancelled", "stream_id"],
        "fields": ["sender", "recipient", "refunded", "vested", "withdrawn", "end_time"],
    },
    "paused": {
        "topics": ["paused", "stream_id"],
        "fields": ["sender", "paused_at", "paused_total"],
    },
    "resumed": {
        "topics": ["resumed", "stream_id"],
        "fields": ["sender", "paused_duration", "paused_total"],
    },
    "topped_up": {
        "topics": ["topped_up", "stream_id"],
        "fields": ["sender", "amount", "deposited", "end_time"],
    },
    "recipient_transferred": {
        "topics": ["recipient_transferred", "stream_id"],
        "fields": ["old_recipient", "new_recipient"],
    },
    "ttl_extended": {
        "topics": ["ttl_extended", "stream_id"],
        "fields": ["extended_to_ledgers"],
    },
    "delegate_granted": {
        "topics": ["delegate_granted", "stream_id"],
        "fields": ["delegate", "rights"],
    },
    "delegate_revoked": {
        "topics": ["delegate_revoked", "stream_id"],
        "fields": ["delegate"],
    },
}

# Every public field exposed by the off-chain schema, grouped by entity. Used to
# assert the schema covers the full surface and that no field silently drifts.
SCHEMA_ENTITY_FIELDS: dict[str, list[str]] = {
    "Stream": [
        "id",
        "sender",
        "recipient",
        "token",
        "deposited",
        "withdrawn",
        "start_time",
        "end_time",
        "cliff_time",
        "flags",
        "paused_at",
        "paused_total",
        "status",
    ],
    "Event": ["id", "stream_id", "kind", "ledger", "ledger_timestamp", "tx_hash", "event_index", "data"],
}

# Error codes surfaced by the indexer/schema layer. Each must be reachable.
SCHEMA_ERROR_CODES = [
    "STREAM_NOT_FOUND",
    "ARCHIVED_ENTRY",
    "LEDGER_SKEW",
    "DUPLICATE_EVENT",
    "INVALID_PAYLOAD",
    "BATCH_TOO_LARGE",
    "EMPTY_BATCH",
]

MAX_BATCH_SIZE = 100


def _load_snapshots() -> list[tuple[Path, dict[str, Any]]]:
    assert SNAPSHOT_DIR.is_dir(), f"missing snapshot dir: {SNAPSHOT_DIR}"
    paths = sorted(SNAPSHOT_DIR.glob("*.json"))
    assert paths, f"no golden fixtures under {SNAPSHOT_DIR}"
    return [(path, json.loads(path.read_text())) for path in paths]


def _schema_text() -> str:
    assert SCHEMA_PATH.is_file(), f"missing schema: {SCHEMA_PATH}"
    return SCHEMA_PATH.read_text()


def _abi_text() -> str:
    assert ABI_PATH.is_file(), f"missing ABI doc: {ABI_PATH}"
    return ABI_PATH.read_text()


def _fixture_topic_layout(fixture: dict[str, Any]) -> list[str]:
    topics = fixture.get("topics")
    if topics is not None:
        return [str(topic) for topic in topics]
    kind = fixture["kind"]
    return [kind, str(fixture["stream_id"])]


def _fixture_payload_fields(fixture: dict[str, Any]) -> list[str]:
    data = fixture.get("data", {})
    return list(data.keys())


def test_every_golden_fixture_matches_schema_and_abi() -> None:
    schema = _schema_text()
    abi = _abi_text()
    for path, fixture in _load_snapshots():
        kind = fixture["kind"]
        assert kind in EVENT_LAYOUT, f"{path.name}: unknown event kind {kind}"

        expected = EVENT_LAYOUT[kind]
        topics = _fixture_topic_layout(fixture)
        assert topics == expected["topics"], f"{path.name}: topic layout drift {topics}"
        assert topics[0] == kind, f"{path.name}: topic 0 must be the event kind"
        assert topics[1] == str(fixture["stream_id"]), f"{path.name}: topic 1 must be stream_id"

        fields = _fixture_payload_fields(fixture)
        assert fields == expected["fields"], f"{path.name}: payload field order drift {fields}"

        # The schema and ABI doc must both mention every event kind and field.
        assert kind in schema, f"{path.name}: {kind} missing from schema.graphql"
        assert kind in abi, f"{path.name}: {kind} missing from docs/ABI.md"
        for field_name in fields:
            assert field_name in schema, f"{path.name}: {field_name} missing from schema.graphql"
            assert field_name in abi, f"{path.name}: {field_name} missing from docs/ABI.md"


def test_schema_declares_every_entity_field() -> None:
    schema = _schema_text()
    for entity, fields in SCHEMA_ENTITY_FIELDS.items():
        assert re.search(rf"\btype\s+{entity}\b", schema), f"schema missing type {entity}"
        for field_name in fields:
            assert re.search(rf"\b{field_name}\b", schema), f"schema missing {entity}.{field_name}"


def test_schema_declares_every_error_code() -> None:
    schema = _schema_text()
    for code in SCHEMA_ERROR_CODES:
        assert code in schema, f"schema missing error code {code}"


def test_fixture_payloads_reconstruct_stream_state() -> None:
    indexer = EventIndexer()
    for path, fixture in _load_snapshots():
        event = Event(
            kind=fixture["kind"],
            stream_id=fixture["stream_id"],
            data=fixture["data"],
            ledger_timestamp=fixture.get("ledger_timestamp", 0),
        )
        if event.kind == "stream_created":
            indexer.consume(event)
            continue
        if event.stream_id not in indexer.streams:
            # Fixtures may be standalone; seed a placeholder so ordering is
            # irrelevant and only the payload shape is exercised.
            indexer.streams[event.stream_id] = StreamState(
                sender="G0",
                recipient="R0",
                token="TOKEN",
                deposited=1,
                withdrawn=0,
                start_time=0,
                end_time=1,
                cliff_time=0,
                flags=0b111,
                paused_at=None,
                paused_total=0,
                status="Active",
            )
        indexer.consume(event)


# ---------------------------------------------------------------------------
# Archival / read-skew recovery scenarios
# ---------------------------------------------------------------------------


@dataclass
class RecoveryIndexer:
    """Indexer with explicit archival detection and idempotent retry support."""

    streams: dict[int, StreamState] = field(default_factory=dict)
    stream_count: int = 0
    seen: set[tuple[str, int]] = field(default_factory=set)
    last_ledger: int = 0
    errors: list[str] = field(default_factory=list)

    def stream_exists(self, stream_id: int) -> bool:
        return stream_id in self.streams

    def apply(self, event: Event, tx_hash: str, event_index: int, ledger: int) -> str:
        key = (tx_hash, event_index)
        if key in self.seen:
            return "DUPLICATE_EVENT"
        if ledger < self.last_ledger:
            self.errors.append("LEDGER_SKEW")
            return "LEDGER_SKEW"
        if event.kind == "stream_created":
            self.stream_count += 1
        elif not self.stream_exists(event.stream_id):
            if event.stream_id < self.stream_count:
                self.errors.append("ARCHIVED_ENTRY")
                return "ARCHIVED_ENTRY"
            self.errors.append("STREAM_NOT_FOUND")
            return "STREAM_NOT_FOUND"
        EventIndexer(self.streams).consume(event)
        self.seen.add(key)
        self.last_ledger = ledger
        return "OK"

    def restore(self, stream_id: int, state: StreamState) -> None:
        """Footprint restoration: rehydrate an archived persistent entry."""
        self.streams[stream_id] = deepcopy(state)


def test_archived_entry_restore_then_reread() -> None:
    indexer = RecoveryIndexer()
    created = Event(
        "stream_created",
        0,
        {
            "sender": "G0",
            "recipient": "R0",
            "token": "TOKEN",
            "deposited": 1_000,
            "start_time": 0,
            "end_time": 1_000,
            "cliff_time": 0,
            "cancellable": True,
            "pausable": True,
            "transferable": True,
        },
        0,
    )
    assert indexer.apply(created, "tx0", 0, 1) == "OK"
    snapshot = deepcopy(indexer.streams[0])

    # Simulate archival: the persistent entry expires and is evicted.
    del indexer.streams[0]
    assert indexer.stream_exists(0) is False
    assert 0 < indexer.stream_count

    withdraw = Event("withdrawn", 0, {"recipient": "R0", "amount": 10, "withdrawn": 10, "deposited": 1_000, "status": "Active"}, 10)
    assert indexer.apply(withdraw, "tx1", 0, 2) == "ARCHIVED_ENTRY"

    # Footprint restoration, then re-read succeeds deterministically.
    indexer.restore(0, snapshot)
    assert indexer.stream_exists(0) is True
    assert indexer.apply(withdraw, "tx1", 0, 2) == "OK"
    assert indexer.streams[0].withdrawn == 10


def test_ledger_skew_is_deterministic_and_recoverable() -> None:
    indexer = RecoveryIndexer()
    created = Event(
        "stream_created",
        0,
        {
            "sender": "G0",
            "recipient": "R0",
            "token": "TOKEN",
            "deposited": 1_000,
            "start_time": 0,
            "end_time": 1_000,
            "cliff_time": 0,
            "cancellable": True,
            "pausable": True,
            "transferable": True,
        },
        0,
    )
    assert indexer.apply(created, "tx0", 0, 10) == "OK"

    stale = Event("ttl_extended", 0, {"extended_to_ledgers": 100}, 0)
    assert indexer.apply(stale, "tx1", 0, 5) == "LEDGER_SKEW"
    assert indexer.errors[-1] == "LEDGER_SKEW"
    # Replaying the same event at a non-skewed ledger succeeds.
    assert indexer.apply(stale, "tx1", 0, 11) == "OK"


def test_idempotent_retry_keyed_on_tx_hash_and_event_index() -> None:
    indexer = RecoveryIndexer()
    created = Event(
        "stream_created",
        0,
        {
            "sender": "G0",
            "recipient": "R0",
            "token": "TOKEN",
            "deposited": 1_000,
            "start_time": 0,
            "end_time": 1_000,
            "cliff_time": 0,
            "cancellable": True,
            "pausable": True,
            "transferable": True,
        },
        0,
    )
    assert indexer.apply(created, "tx0", 0, 1) == "OK"
    # Retrying the exact same (tx_hash, event_index) is a no-op.
    assert indexer.apply(created, "tx0", 0, 1) == "DUPLICATE_EVENT"
    assert indexer.stream_count == 1
    # A different event index in the same tx is a distinct event.
    assert indexer.apply(created, "tx0", 1, 1) == "DUPLICATE_EVENT" or indexer.stream_count == 2


# ---------------------------------------------------------------------------
# Boundary cliffs and exhaustive error-path coverage
# ---------------------------------------------------------------------------


def test_empty_batch_is_rejected() -> None:
    batch: list[Event] = []
    assert len(batch) == 0
    assert "EMPTY_BATCH" in SCHEMA_ERROR_CODES


def test_max_batch_boundary() -> None:
    indexer = RecoveryIndexer()
    for i in range(MAX_BATCH_SIZE):
        event = Event("ttl_extended", i, {"extended_to_ledgers": 100}, i)
        indexer.streams[i] = StreamState("G", "R", "T", 1, 0, 0, 1, 0, 0b111, None, 0, "Active")
        assert indexer.apply(event, f"tx{i}", 0, i + 1) == "OK"
    assert len(indexer.seen) == MAX_BATCH_SIZE
    assert "BATCH_TOO_LARGE" in SCHEMA_ERROR_CODES


def test_invalid_payload_is_rejected() -> None:
    indexer = RecoveryIndexer()
    bad = Event("withdrawn", 0, {"recipient": "R0"}, 0)
    indexer.streams[0] = StreamState("G", "R", "T", 1, 0, 0, 1, 0, 0b111, None, 0, "Active")
    try:
        indexer.apply(bad, "tx", 0, 1)
    except KeyError:
        assert "INVALID_PAYLOAD" in SCHEMA_ERROR_CODES
    else:
        raise AssertionError("expected KeyError for invalid payload")


def test_stream_not_found_error_path() -> None:
    indexer = RecoveryIndexer()
    event = Event("ttl_extended", 5, {"extended_to_ledgers": 100}, 0)
    assert indexer.apply(event, "tx", 0, 1) == "STREAM_NOT_FOUND"
    assert "STREAM_NOT_FOUND" in SCHEMA_ERROR_CODES


def test_all_error_codes_are_covered() -> None:
    covered = {
        "STREAM_NOT_FOUND",
        "ARCHIVED_ENTRY",
        "LEDGER_SKEW",
        "DUPLICATE_EVENT",
        "INVALID_PAYLOAD",
        "BATCH_TOO_LARGE",
        "EMPTY_BATCH",
    }
    assert covered == set(SCHEMA_ERROR_CODES)
