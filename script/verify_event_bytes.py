"""Independent hand-calculation of the keeper runner's event surface, written
from the stellar-xdr definitions, to cross-check evsize.py. Deliberately does
NOT import or reuse anything from that script.

Covers every event/state code path `script/keeper-extend-ttl.sh` touches:
  * `withdrawn`    -- frozen total 260 bytes (docs/ABI.md)
  * `ttl_extended` -- frozen total 116 bytes (docs/ABI.md)

Invoke per-event with `--event <name>`; with no flag every event is checked.
"""
import argparse
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

withdrawn_total = FRAME + topics + data

# ttl_extended: topics: symbol "ttl_extended", u64 stream_id
#               data:   u32 extended_to_ledgers
ttl_topics = 4 + 4 + 4 + sym("ttl_extended") + U64
ttl_data = U32
ttl_total = FRAME + ttl_topics + ttl_data

EVENTS = {
    "withdrawn": (withdrawn_total, 260),
    "ttl_extended": (ttl_total, 116),
}


def report_withdrawn():
    print(f"FRAME  = {FRAME}")
    print(f"topics = {topics}")
    print(f"data   = {data}")
    print(f"total  = {withdrawn_total}")
    print()
    print(f"sym sizes: amount={sym('amount')} withdrawn={sym('withdrawn')} "
          f"deposited={sym('deposited')} status={sym('status')}")
    print(f"val sizes: i128={I128} u32={U32} u64={U64} addr={ADDR}")
    assert withdrawn_total == 260, f"expected 260, got {withdrawn_total}"
    print("\nMATCHES evsize.py (260)")


def report_ttl_extended():
    print(f"FRAME      = {FRAME}")
    print(f"topics     = {ttl_topics}")
    print(f"data       = {ttl_data}")
    print(f"total      = {ttl_total}")
    print()
    print(f"sym sizes: ttl_extended={sym('ttl_extended')}")
    print(f"val sizes: u32={U32} u64={U64}")
    assert ttl_total == 116, f"expected 116, got {ttl_total}"
    print("\nMATCHES docs/ABI.md (116)")


REPORTERS = {
    "withdrawn": report_withdrawn,
    "ttl_extended": report_ttl_extended,
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--event",
        choices=sorted(EVENTS),
        default=None,
        help="verify a single event instead of the whole surface",
    )
    args = parser.parse_args()

    names = [args.event] if args.event else sorted(EVENTS)
    for i, name in enumerate(names):
        if i:
            print()
        print(f"== {name} ==")
        REPORTERS[name]()


if __name__ == "__main__":
    main()
