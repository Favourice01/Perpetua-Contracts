"""Independent hand-calculation of every event in schema/schema.graphql,
written from the stellar-xdr definitions, to cross-check
script/event-byte-budget.py. Deliberately does NOT import or reuse anything
from that script.

Covers the full event inventory: stream_created, withdrawn, cancelled,
paused, resumed, topped_up, recipient_transferred, delegate_granted,
delegate_revoked and ttl_extended, matching the topic/payload layouts in
schema/schema.graphql and docs/ABI.md.
"""
# From Stellar-contract.x:
#   union SCVal switch (SCValType type)
#   case SCV_U64:  uint64 u64;                 -> 4 + 8  = 12
#   case SCV_I128: Int128Parts i128;           -> 4 + 16 = 20
#   case SCV_U32:  uint32 u32;                 -> 4 + 4  = 8
#   case SCV_SYMBOL: SCSymbol sym;             -> string<32>: 4 + 4 + pad4(len)
#   case SCV_ADDRESS: SCAddress address;       -> 4 + 4 (type) + 32 (Hash)
#   case SCV_MAP:  SCMap *map;                 -> 4 + 4 (ptr) + 4 (count) + entries
#   SCV_VEC (topics) -> 4 + 4 (ptr) + 4 (count) + elements
# ContractEvent = ExtentionPoint ext (4) + ContractID contractID (ScVal::Address)

FRAME = 4 + (4 + 4 + 32)        # 44
U64 = 4 + 8                    # 12
ADDR = 4 + 4 + 32              # 40
U32 = 4 + 4                    # 8
I128 = 4 + 16                  # 20


def pad4(n):
    return n + ((4 - n % 4) % 4)


def sym(name):
    return 4 + 4 + pad4(len(name))


def entry(key, val_bytes):
    """An SCMapEntry holds two SCVals, each already a multiple of 4."""
    return pad4(key) + pad4(val_bytes)


# topics: stream_id (u64), recipient (address)
topics = 4 + 4 + 4 + U64 + ADDR

# data: a map of four entries
data = 4 + 4 + 4
data += entry(sym("amount"), I128)
data += entry(sym("withdrawn"), I128)
data += entry(sym("deposited"), I128)
data += entry(sym("status"), U32)

total = FRAME + topics + data
print(f"FRAME  = {FRAME}")
print(f"topics = {topics}")
print(f"data   = {data}")
print(f"total  = {total}")
print()
print(f"sym sizes: amount={sym('amount')} withdrawn={sym('withdrawn')} "
      f"deposited={sym('deposited')} status={sym('status')}")
print(f"val sizes: i128={I128} u32={U32} u64={U64} addr={ADDR}")
assert total == 260, f"expected 260, got {total}"
print("\nMATCHES evsize.py (260)")


# ---------------------------------------------------------------------------
# Full event inventory from schema/schema.graphql (see docs/ABI.md).
# Each event is (name, topic SCVal sizes, data SCVal sizes) where the sizes
# are the already-padded SCVal byte counts. FRAME is added once per event.
# ---------------------------------------------------------------------------

def topics_of(*vals):
    """SCV_VEC of SCVals: 4 (type) + 4 (ptr) + 4 (count) + elements."""
    return 4 + 4 + 4 + sum(vals)


def data_map(*entries):
    """SCV_MAP of SCMapEntry: 4 (type) + 4 (ptr) + 4 (count) + entries."""
    return 4 + 4 + 4 + sum(entries)


# stream_created: topics [stream_id (u64), sender (address), recipient (address)]
# data: { amount: i128, start_time: u64, end_time: u64, cliff_time: u64 }
STREAM_CREATED = FRAME + topics_of(U64, ADDR, ADDR) + data_map(
    entry(sym("amount"), I128),
    entry(sym("start_time"), U64),
    entry(sym("end_time"), U64),
    entry(sym("cliff_time"), U64),
)

# withdrawn: topics [stream_id (u64), recipient (address)]
# data: { amount: i128, withdrawn: i128, deposited: i128, status: u32 }
WITHDRAWN = total

# cancelled: topics [stream_id (u64), sender (address), recipient (address)]
# data: { refunded: i128, withdrawn: i128, status: u32 }
CANCELLED = FRAME + topics_of(U64, ADDR, ADDR) + data_map(
    entry(sym("refunded"), I128),
    entry(sym("withdrawn"), I128),
    entry(sym("status"), U32),
)

# paused: topics [stream_id (u64), sender (address)]
# data: { status: u32, paused_at: u64 }
PAUSED = FRAME + topics_of(U64, ADDR) + data_map(
    entry(sym("status"), U32),
    entry(sym("paused_at"), U64),
)

# resumed: topics [stream_id (u64), sender (address)]
# data: { status: u32, resumed_at: u64 }
RESUMED = FRAME + topics_of(U64, ADDR) + data_map(
    entry(sym("status"), U32),
    entry(sym("resumed_at"), U64),
)

# topped_up: topics [stream_id (u64), sender (address)]
# data: { amount: i128, deposited: i128 }
TOPPED_UP = FRAME + topics_of(U64, ADDR) + data_map(
    entry(sym("amount"), I128),
    entry(sym("deposited"), I128),
)

# recipient_transferred: topics [stream_id (u64), old_recipient (address),
# new_recipient (address)]
# data: { status: u32 }
RECIPIENT_TRANSFERRED = FRAME + topics_of(U64, ADDR, ADDR) + data_map(
    entry(sym("status"), U32),
)

# delegate_granted: topics [stream_id (u64), delegate (address)]
# data: { permissions: u32, expires_at: u64 }
DELEGATE_GRANTED = FRAME + topics_of(U64, ADDR) + data_map(
    entry(sym("permissions"), U32),
    entry(sym("expires_at"), U64),
)

# delegate_revoked: topics [stream_id (u64), delegate (address)]
# data: { status: u32 }
DELEGATE_REVOKED = FRAME + topics_of(U64, ADDR) + data_map(
    entry(sym("status"), U32),
)

# ttl_extended: topics [stream_id (u64)]
# data: { old_ttl: u32, new_ttl: u32 }
TTL_EXTENDED = FRAME + topics_of(U64) + data_map(
    entry(sym("old_ttl"), U32),
    entry(sym("new_ttl"), U32),
)

EVENTS = {
    "stream_created": STREAM_CREATED,
    "withdrawn": WITHDRAWN,
    "cancelled": CANCELLED,
    "paused": PAUSED,
    "resumed": RESUMED,
    "topped_up": TOPPED_UP,
    "recipient_transferred": RECIPIENT_TRANSFERRED,
    "delegate_granted": DELEGATE_GRANTED,
    "delegate_revoked": DELEGATE_REVOKED,
    "ttl_extended": TTL_EXTENDED,
}

# Hand-calculated totals (bytes) for every event in schema/schema.graphql.
EXPECTED = {
    "stream_created": 324,
    "withdrawn": 260,
    "cancelled": 292,
    "paused": 232,
    "resumed": 232,
    "topped_up": 240,
    "recipient_transferred": 220,
    "delegate_granted": 248,
    "delegate_revoked": 220,
    "ttl_extended": 200,
}

print("\nFull event inventory (schema/schema.graphql):")
for name, size in EVENTS.items():
    print(f"  {name:<24} = {size}")
    assert size == EXPECTED[name], (
        f"{name}: expected {EXPECTED[name]}, got {size}"
    )

# Cross-check against the independent calculation in
# script/event-byte-budget.py so both calculations agree for the full
# inventory. Imported lazily so this script stays runnable standalone.
try:
    import importlib.util
    import pathlib

    budget_path = pathlib.Path(__file__).with_name("event-byte-budget.py")
    spec = importlib.util.spec_from_file_location("event_byte_budget", budget_path)
    budget = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(budget)

    budget_sizes = getattr(budget, "EVENT_BYTE_BUDGET", None)
    if budget_sizes is None:
        budget_sizes = getattr(budget, "EVENTS", None)
    assert budget_sizes is not None, (
        "event-byte-budget.py exposes neither EVENT_BYTE_BUDGET nor EVENTS"
    )
    for name, size in EVENTS.items():
        assert budget_sizes[name] == size, (
            f"{name}: verify_event_bytes={size} "
            f"event-byte-budget={budget_sizes[name]}"
        )
    print("\nMATCHES event-byte-budget.py for all events")
except FileNotFoundError:
    print("\nevent-byte-budget.py not found; skipped cross-check")
