# Pin your Soroban reads to a single ledger

*A note from the Perpetua team. Written against Stellar testnet, protocol 27,
`stellar-cli` 27.1.0, on 2026-08-12.*

## The pattern

**If you derive one figure from two Soroban RPC view calls, pin both to the same
ledger — or do not derive it from two calls at all.**

A public RPC URL is an endpoint, not a node. Requests behind it may be served by
different backends, and those backends need not be at the same ledger height at
the same instant. Nothing in the JSON-RPC surface tells you which ledger you got
unless you look: every response carries `latestLedger`, and almost no client
code reads it.

Two consequences worth designing against, neither of which produces an error —
you get a plausible wrong answer instead:

1. **Read-after-write.** Read immediately after a confirmed write and you may
   observe pre-write state.
2. **Cross-call derivation.** Combine two reads into one number and the halves
   may describe different moments, making the result arithmetically impossible.

The rest of this note is why we went looking, and what to do instead. The
pattern above stands on its own — it is the correct way to use any
load-balanced read endpoint, and it costs almost nothing to adopt.

## What prompted us to look

Perpetua is a payment-streaming contract. Value accrues continuously, so we have
a conservation invariant:

```
vested(t) + refundable(t) == deposited      exactly, at every instant
```

Our on-chain test suite proves this per-ledger over thousands of random
schedules. So when a script that calls the two views back to back against live
testnet reported

```
vested=22000000  refundable=583000000  deposited=600000000
  ->  22000000 + 583000000 = 605000000   ✗ off by 5000000
```

the natural conclusion was a contract bug — 5,000,000 stroops conjured from
nowhere.

It was not. On that stream the rate was 1,000,000 stroops/second, so the
discrepancy was **exactly five seconds of accrual**: the two view calls
described moments five seconds apart. A `top_up` in the same script separately
appeared to be a no-op — the write was confirmed, but the `get_stream`
immediately afterwards returned the pre-top-up deposit.

Both symptoms are explained by the two calls not sharing a ledger, and both went
away when we added the barrier below. We spent real time hunting a contract bug
that did not exist, which is the cost this note exists to save you.

## The observation, and its limits

We looked directly at `getLatestLedger`, which needs no contract:

```bash
prev=0
for i in $(seq 1 25); do
  s=$(curl -s -X POST https://soroban-testnet.stellar.org \
        -H 'Content-Type: application/json' \
        -d '{"jsonrpc":"2.0","id":1,"method":"getLatestLedger"}' \
      | python3 -c 'import sys,json;print(json.load(sys.stdin)["result"]["sequence"])')
  if [ "$prev" -ne 0 ] && [ "$s" -lt "$prev" ]; then
    echo "went BACKWARDS: $prev -> $s"
  fi
  prev=$s
done
```

One run, around ledger 4,097,07x, showed the endpoint reporting a *lower* height
than it had a moment earlier:

```
went BACKWARDS: 4097075 -> 4097071
went BACKWARDS: 4097075 -> 4097071
went BACKWARDS: 4097076 -> 4097071
went BACKWARDS: 4097076 -> 4097071
went BACKWARDS: 4097076 -> 4097071
went BACKWARDS: 4097076 -> 4097071
backwards transitions in 25 reads: 6
```

Six transitions in twenty-five reads, spread about five ledgers — consistent
with the five seconds of accrual we had just seen unaccounted for.

**We could not reproduce it again.** Everything afterwards was clean:

| run | method | samples | backwards steps |
|---|---|---|---|
| 1 | `curl`, fresh connection, no delay | 25 | **6** |
| 2 | Python `urllib`, 150 ms interval | 60 | 0 |
| 3 | `curl`, fresh connection, no delay | 60 | 0 |
| 4 | Python `urllib`, 200 ms interval, 8 minutes | 375 | 0 |

That is **one anomalous run against 495 subsequent clean samples**, including run
3, which repeated run 1's method exactly, thirty minutes later.

So we are not claiming this is a general or ongoing property of the endpoint,
and you should not repeat it as one. A single unreproduced observation is
consistent with several explanations — a backend briefly catching up after a
restart, a transient routing artefact, or something local to our client at that
moment — and we cannot distinguish between them from one sample. If you are at
SDF and have data that rules it out, we would genuinely like to know; we will
amend this note.

**What does not depend on that question is the engineering pattern.** Pinning
multi-call derivations to a single ledger is correct whether or not the endpoint
ever skews, because you cannot verify from the client side that it did not, and
because `latestLedger` is right there in every response. It costs one field
check. Treating a single URL as a single node is an assumption you are making
either way — this just makes it explicit and cheap to stop making.

## Frontend SDK guidance: poll until the target ledger

If your app submits a Soroban transaction and immediately reads contract state,
add a barrier. In practice, this means polling `getLatestLedger()` until the
node is at or beyond the target ledger for your write, then reading the stream
state. Treat that read as the boundary between "write not yet visible" and
"safe to display".

The pattern is simple:

1. Submit the transaction.
2. Wait for the transaction to be included in a ledger.
3. Poll `getLatestLedger()` until it reaches or passes the ledger sequence you
   just observed.
4. Read the contract state only after that boundary.
5. If the ledger still lags, retry instead of deriving a number from stale data.

### TypeScript example

```ts
import type { rpc as SorobanRpc } from '@stellar/stellar-sdk';

async function waitForLedger(
  rpc: SorobanRpc.Server,
  targetLedger: number,
  { maxAttempts = 40, pollMs = 1000 } = {},
): Promise<number> {
  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    const latest = await rpc.getLatestLedger();
    if (latest.sequence >= targetLedger) {
      return latest.sequence;
    }
    await new Promise((resolve) => setTimeout(resolve, pollMs));
  }

  throw new Error(
    `RPC did not reach ledger ${targetLedger} after ${maxAttempts} attempts`,
  );
}

async function readStreamAfterWrite(
  rpc: SorobanRpc.Server,
  txHash: string,
  contract: { read: (args?: any[]) => Promise<any> },
) {
  const tx = await rpc.getTransaction(txHash);
  const includedLedger = tx?.resultMeta?.v3?.sorobanTransactionMeta?.sorobanMeta?.ledger
    ?? tx?.resultMeta?.v3?.ledger;

  if (typeof includedLedger !== 'number') {
    throw new Error(`Transaction ${txHash} is not yet included in a ledger`);
  }

  await waitForLedger(rpc, includedLedger, { maxAttempts: 40, pollMs: 1000 });
  return contract.read();
}
```

The precise `tx.resultMeta` field names can vary slightly by SDK version, but the
idea is stable: read the included ledger from the transaction result, then wait
for the node to advance to that sequence before querying state.

### React / frontend pattern

```ts
async function refreshStreamState() {
  const tx = await submitStreamTransaction();
  const latestLedger = await rpc.getLatestLedger();

  // Poll until the node is at least as far as the write's ledger.
  while ((await rpc.getLatestLedger()).sequence < tx.ledgerSequence) {
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }

  return rpc.getContractValue({
    contractId: STREAM_CONTRACT_ID,
    key: 'stream',
    // or whichever view your app uses
  });
}
```

If your UI reads the stream immediately after a top-up, withdrawal or claim,
this barrier is the difference between a stale read and a correct one. The rate
of accrual is not the only thing that can drift between ledgers — the node you
read from can also still be behind the one that included your transaction.

## What to do

These are ordered by preference. The first one makes the problem structurally
impossible and is worth designing your contract's views around.

### 1. Return everything you need from one call

If a client needs vested, withdrawable and refundable together, give it a view
that returns the whole struct and let it compute the three locally. One call is
one ledger, by construction; there is nothing to pin.

Perpetua's `get_stream` does this, which is why the fix for our own script was
partly "stop calling `vested_of` and `refundable_of` separately".

### 2. Put a barrier after every write

Do not read straight after a write. Wait until the backends you can see have
caught up to the ledger containing your transaction. A cheap version: sample
`getLatestLedger` until several consecutive samples are all at or beyond your
target height.

```bash
settle() {
  local target hi=0 ok=0 tries=0 s
  target=$(latest_ledger)
  while [ "$ok" -lt 4 ] && [ "$tries" -lt 40 ]; do
    s=$(latest_ledger)
    [ "$s" -gt "$hi" ] && hi=$s
    if [ "$s" -ge "$target" ] && [ "$s" -gt 0 ]; then ok=$((ok+1)); else ok=0; fi
    tries=$((tries+1)); sleep 1
  done
}
```

Requiring several *consecutive* samples at or above the target is what makes
this work: a single passing sample only tells you that *one* backend has caught
up, and the next request may well go to a different one.

### 3. When you must use several calls, check `latestLedger`

Every Soroban RPC response carries it. Read it from each response in the set,
and if they disagree, discard and retry rather than combining them. If you are
only sanity-checking rather than displaying, a bounded tolerance is enough — our
exercise script allows about 30 seconds of accrual on the conservation check,
with a comment saying why.

### TypeScript reference implementation

The JavaScript SDK exposes the sequence as `latestLedger` on each RPC response.
It is response metadata, not a portable request header: Soroban RPC does not
provide a standard `ledger` parameter that pins `simulateTransaction` to an
older ledger. Treat `latestLedger` as the ledger-sequence header for the read,
and retry the complete group when the values differ.

When the required values are available as ledger keys, prefer one
`getLedgerEntries` request instead of several simulations. The response carries
one `latestLedger` for the whole key batch:

```ts
const response = await server.getLedgerEntries(countKey, ...streamKeys);
const snapshotLedger = response.latestLedger;
const entries = response.entries;
```

This is the closest RPC equivalent to pinning a read. It requires constructing
the exact Soroban `LedgerKey` values, so contract views remain the simpler
choice when the needed keys are not already known to the indexer.

```ts
import { SorobanRpc, xdr } from "@stellar/stellar-sdk";

type LedgerRead<T> = { value: T; latestLedger: number };
const pause = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

export async function readAtOneLedger<T>(
  reads: readonly (() => Promise<LedgerRead<T>>)[],
  attempts = 5,
): Promise<{ values: T[]; ledger: number }> {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const batch = await Promise.all(reads.map((read) => read()));
    const ledger = batch[0]?.latestLedger;
    if (ledger !== undefined && batch.every((item) => item.latestLedger === ledger)) {
      return { values: batch.map((item) => item.value), ledger };
    }
    await pause(250);
  }
  throw new Error("RPC reads did not converge on one ledger");
}

export async function simulateStamped<T>(
  server: SorobanRpc.Server,
  transaction: Parameters<SorobanRpc.Server["simulateTransaction"]>[0],
  decode: (value: xdr.ScVal) => T,
): Promise<LedgerRead<T>> {
  const response = await server.simulateTransaction(transaction);
  if (!response.result) throw new Error(response.error ?? "Simulation failed");
  return { value: decode(response.result), latestLedger: response.latestLedger };
}
```

Build each transaction with the same `Contract` and source account, then pass
the resulting simulations to `readAtOneLedger`. An indexer that needs the
count and all stream records should retry the whole snapshot, not read the
count once and assume later `get_stream` calls see the same state:

```ts
async function readStreamSnapshot(
  readCount: () => Promise<LedgerRead<bigint>>,
  readStream: (id: bigint) => Promise<LedgerRead<Stream>>,
): Promise<{ count: bigint; streams: Stream[]; ledger: number }> {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    const count = await readCount();
    const streams = await Promise.all(
      Array.from({ length: Number(count.value) }, (_, index) => readStream(BigInt(index))),
    );
    if (streams.every((stream) => stream.latestLedger === count.latestLedger)) {
      return {
        count: count.value,
        streams: streams.map((stream) => stream.value),
        ledger: count.latestLedger,
      };
    }
    await pause(250);
  }
  throw new Error("stream snapshot crossed a ledger boundary");
}
```

The count is read before allocating the id range, and every returned stream is
accepted only when its sequence matches that count. This prevents a concurrent
`create_stream` from producing a snapshot whose range was selected from one
ledger but read from another. If a stream disappears or an id is out of range,
discard the snapshot and retry; do not turn `StreamNotFound` into an empty
record.

For a read-after-write barrier, first wait for the transaction itself to reach
`SUCCESS`, retain its committed ledger, then wait until the RPC endpoint has
reported that ledger repeatedly before starting the stamped read group:

```ts
export async function waitForCommit(
  server: SorobanRpc.Server,
  hash: string,
): Promise<number> {
  for (;;) {
    const transaction = await server.getTransaction(hash);
    if (transaction.status === "SUCCESS") return transaction.ledger;
    if (transaction.status === "FAILED") throw new Error("transaction failed");
    await pause(1_000); // PENDING or NOT_FOUND: RPC indexing is still catching up.
  }
}

export async function waitForLedger(
  server: SorobanRpc.Server,
  target: number,
  consecutive = 4,
): Promise<void> {
  let seen = 0;
  while (seen < consecutive) {
    const latest = (await server.getLatestLedger()).sequence;
    seen = latest >= target ? seen + 1 : 0;
    await pause(1_000);
  }
}

const committedLedger = await waitForCommit(server, sendResult.hash);
await waitForLedger(server, committedLedger);
const snapshot = await readStreamSnapshot(readCount, readStream);
```

The barrier reduces stale-replica reads after a write; the stamped group is
still required for consistency between multiple views. Do not use the latest
ledger sampled before `sendTransaction` as the barrier target: only the ledger
returned by successful transaction polling proves that the write was included.

### 4. Never assert exact equality across two calls in a test

A test that asserts `a() + b() == c()` across three separate simulations against
a public endpoint will fail intermittently, and every failure will look like a
contract bug. Either derive all three from one call, or assert within a
tolerance you can justify.

### 5. Run your own RPC if you need strict read-after-write

A single node you control cannot skew against itself. For an indexer or a keeper
this is worth the operational cost; the barrier pattern above is a mitigation,
not a guarantee.

## Indexer recovery: skew, partial batches, and archived entries

The off-chain indexer (`schema/schema.graphql`, `schema/schema.sql`) consumes
Soroban RPC events and ledger entries. It faces three failure modes that the
client-side barrier above does not cover on its own: read skew across a batch,
partial batch failure mid-ingest, and reads against **archived** persistent
entries. This section documents how the schema and the recovery primitives
handle each.

### Idempotent retry semantics keyed on `(tx_hash, event_index)`

Every event row in the schema is uniquely identified by the pair
`(tx_hash, event_index)`. That pair is the idempotency key for the whole
ingest pipeline:

- `Event` (and its derived `StreamEvent` / `VestingEvent` rows) carries a
  `UNIQUE (tx_hash, event_index)` constraint in `schema/schema.sql` and a
  `@derivedFrom` / `@unique` directive pair in `schema/schema.graphql`.
- Inserts use `INSERT ... ON CONFLICT (tx_hash, event_index) DO NOTHING`
  (SQL) and upsert-by-id (subgraph), so replaying a batch that was already
  partially applied is a no-op for the rows that landed and a fresh insert
  for the rows that did not.
- Derived aggregates (`Stream.total_vested`, `Stream.total_withdrawn`,
  `Vesting.claimed`) are recomputed from the event log rather than
  incremented in place, so a replayed event cannot double-count.

This means a stalled or partially-failed batch can be **re-run from its
starting ledger** without truncating tables or reconciling by hand. The
indexer records the last fully-committed ledger in `IndexerCursor`; a re-run
starts at `IndexerCursor.last_ledger` and relies on the idempotency key to
absorb the overlap.

### RPC read skew inside a batch

A batch that spans a ledger boundary can read `latestLedger = N` for the
cursor update but `latestLedger = N-1` for a `getLedgerEntries` call in the
same batch. The schema handles this deterministically:

1. Each batch records the `latestLedger` returned by the **first** RPC call
   as `IndexerCursor.batch_ledger`.
2. Every subsequent read in the batch is accepted only if its
   `latestLedger >= batch_ledger`. A read that reports a lower ledger is
   discarded and retried; it is never merged into the batch.
3. `IndexerCursor.last_ledger` is only advanced to `batch_ledger` after the
   entire batch commits. A partially-applied batch leaves the cursor
   unchanged, so the next run re-reads the same range.

This is the indexer-side equivalent of the client barrier in the previous
section: pin the batch to one ledger, and refuse to advance the cursor past a
ledger whose events are not all present.

### Archival detection and footprint restoration

Persistent Soroban entries (stream records, vesting schedules) can be
**archived** once their TTL lapses. An archived entry is not deleted — it is
absent from the live ledger and must be restored before it can be read. The
indexer detects this by treating a missing entry as `ARCHIVED`, not as
`NOT_FOUND`:

- A `getLedgerEntries` response that omits a key the indexer expects is
  recorded as an `ArchivedEntry` row keyed on the same
  `(contract_id, ledger_key)` pair used by the live tables.
- The indexer does **not** delete or null out the corresponding `Stream` /
  `Vesting` row. The last known state is retained and flagged
  `archived = true`, so downstream consumers see a stale-but-valid record
  rather than a hole.
- Restoration is performed out-of-band by the recovery primitives below, and
  the indexer re-reads the restored entry on the next batch.

The two recovery primitives are:

- **`tools/sdk/restore_footprint.ts`** — builds and submits a
  `RestoreFootprint` operation for the archived ledger keys. It is idempotent:
  restoring an already-live key is a no-op, so it is safe to run repeatedly
  against a partially-restored footprint.
- **`script/archival-canary.sh`** — a scheduled probe that reads a known set
  of persistent keys and reports any that have gone missing. It is the
  detection half of the loop; `restore_footprint.ts` is the repair half.

The intended loop is: canary detects an archived key → operator (or the
canary's `--auto-restore` mode) runs `restore_footprint.ts` for that key →
indexer re-reads on the next batch and clears the `archived` flag.

### Operator runbook: re-running a stalled or partially-failed batch

Use this when `IndexerCursor.last_ledger` has stopped advancing, when the
canary reports archived keys, or when a batch failed partway through.

1. **Confirm the stall.** Check `IndexerCursor.last_ledger` against the
   current `getLatestLedger()`. A gap that is not shrinking across two or
   three poll intervals means the batch is stalled, not just slow.
2. **Check for archived entries.** Run `script/archival-canary.sh`. If it
   reports missing keys, run `tools/sdk/restore_footprint.ts` for those keys
   and wait for the restore transaction to reach `SUCCESS` before continuing.
   Do **not** re-run the batch against an unrestored footprint — the indexer
   will keep flagging the same keys as archived.
3. **Re-run the batch from the cursor.** Restart the indexer with
   `--from-ledger $(IndexerCursor.last_ledger)`. Because ingest is keyed on
   `(tx_hash, event_index)`, the overlap with the previously-applied portion
   of the batch is absorbed as a no-op. Do not truncate tables first; that
   discards the idempotency guarantee and can double-count derived
   aggregates.
4. **Verify the cursor advanced.** After the re-run, confirm
   `IndexerCursor.last_ledger` is at or beyond the ledger you expected and
   that no new `ArchivedEntry` rows were written for the range you just
   processed.
5. **If the cursor still does not advance,** the batch is failing for a
   reason other than skew or archival — check the RPC endpoint's health and
   the indexer logs for decode errors before retrying again. Repeated
   re-runs are safe, but they will not fix a malformed event.

The key property to rely on: **re-running a batch is always safe.** The
idempotency key makes replay a no-op for already-applied events, the cursor
only advances on full commit, and archived entries are flagged rather than
dropped. There is no scenario in this pipeline where the correct operator
action is to delete rows and start over.

## What this is not
=======

**Not a consensus or data-integrity problem.** Any node serving an older ledger
is serving a valid, internally consistent view of a real ledger. Ledger *N* is
not wrong; it is just older than *N+5*.

**Not a criticism of the RPC infrastructure.** Read-your-writes consistency
across load-balanced replicas is a standard distributed-systems tradeoff, not a
defect, and the same caveat applies to essentially every hosted blockchain RPC.

**Not Soroban-specific.** It is worth writing down only because the client
tooling presents one URL as though it were one node, and because in a
*streaming* contract — where the correct answer changes every second — the
symptom impersonates a contract bug closely enough to send you debugging the
wrong thing.

## Summary for client authors

| do | don't |
|---|---|
| derive everything from one call where possible | combine two view calls into one number |
| barrier after writes, requiring several consecutive samples | read immediately after a write |
| check `latestLedger` on each response and discard mismatched sets | assume consecutive reads are monotonic |
| assert within a justified tolerance in tests | assert exact cross-call equality against a public endpoint |
| run your own node if you need strict read-after-write | conclude the endpoint is fine because one loop looked clean |

---

*Written while building [Perpetua](https://github.com/hannyloveworld/Perpetua-Contracts),
a continuous payment streaming primitive for Soroban. The pattern is the point;
the observation is one data point. Corrections and contradicting data are both
welcome, and we will amend this note.*
