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

## Operator runbook: archived entries, footprint restoration, and retries

This section is the operational companion to the pattern above. It covers the
failure modes that show up once a streaming or vesting contract has been live
long enough for its persistent entries to expire, and it is the procedure
operators should follow when `script/event-byte-budget.py` (the Soroban 16 KiB
Event Byte Footprint Enforcer) reports a footprint problem or a read-skew
abort.

### Detecting an archived persistent entry

Soroban persistent entries have a TTL. When that TTL lapses the entry is
*archived*: it is not deleted, but it is no longer readable by ordinary
contract calls until it is restored. From the client side the symptom is
subtle, because an archived entry does not produce a distinct RPC error — a
view call that reads it simply behaves as though the entry were absent.

The reliable detector is to compare the stream id against the stream count
returned by the same snapshot:

```
stream_exists(id) == false   AND   id < stream_count()
```

Both halves matter:

- `stream_exists(id) == false` alone is ambiguous. It is also what you get for
  an id that was never allocated, or for a read that landed on a replica that
  is behind the ledger that created the stream.
- `id < stream_count()` proves the id *was* allocated. The count is monotonic
  and is itself a persistent entry, so if the count says the stream exists but
  the record does not, the record has been archived (or the read is skewed —
  see the next subsection).

`script/event-byte-budget.py` applies exactly this test. When it sees
`stream_exists() == false` for an id below `stream_count()`, it does **not**
treat the stream as absent and does **not** silently shrink the footprint it
is about to report. It raises a distinct, non-retryable `ArchivedEntryError`
naming the stream id, so the operator sees the archival rather than a
plausible-but-wrong byte budget.

### Distinguishing archival from ledger skew

Archival and read skew produce the same `stream_exists() == false` symptom, and
the recovery is different, so the enforcer distinguishes them before acting.
Every RPC response carries `latestLedger`; `script/event-byte-budget.py` reads
it from each response in a snapshot and compares the values:

- **All responses agree on `latestLedger`, and the id is below the count.**
  This is archival. Proceed to footprint restoration below.
- **Responses disagree on `latestLedger`.** This is read skew, not archival.
  The enforcer discards the snapshot and retries the whole group (see
  "Idempotent retry semantics"). It never restores a footprint on the basis of
  a skewed read, because a skewed read can make a live entry look archived.
- **Responses agree, but the id is at or above the count.** The id was never
  allocated. This is a caller error, not a recovery scenario.

The enforcer also refuses to act on a snapshot whose `latestLedger` is behind
the ledger it observed at the start of the run, which is the same
"went backwards" condition described in *The observation, and its limits*
above. A backwards `latestLedger` is treated as skew and retried, never as
evidence of archival.

### Restoring an archived footprint

Restoration is a two-step operation and must be done in this order:

1. **Build the restore transaction.** `RestoreFootprint` takes the ledger keys
   of the archived entries, not the entries themselves. Use
   `tools/sdk/restore_footprint.ts` to construct it:

   ```bash
   npx ts-node tools/sdk/restore_footprint.ts \
     --contract "$STREAM_CONTRACT_ID" \
     --stream-id "$STREAM_ID" \
     --network testnet
   ```

   The tool reads `stream_count()` and the stream record, derives the exact
   `LedgerKey` values for the archived entries, and prints the unsigned
   `RestoreFootprint` operation. It does not submit anything.

2. **Submit and confirm.** Sign and submit the transaction, then wait for it
   to reach `SUCCESS` using the `waitForCommit` barrier above. Do not proceed
   on `PENDING` or `NOT_FOUND` — those mean the RPC indexer has not caught up,
   not that the restore failed.

   ```bash
   stellar tx submit --network testnet --sign-with "$SIGNER" restore.xdr
   ```

3. **Re-run the enforcer.** Only after the restore is confirmed:

   ```bash
   python3 script/event-byte-budget.py --contract "$STREAM_CONTRACT_ID"
   ```

   A successful restore makes the previously archived entry readable again,
   and the enforcer's footprint report will include it. If the enforcer still
   reports `ArchivedEntryError` for the same id, the restore did not cover the
   entry — re-derive the ledger keys with `tools/sdk/restore_footprint.ts`
   rather than guessing at them.

Restoration is safe to repeat. `RestoreFootprint` on an entry that is already
live is a no-op, so a restore that was submitted but whose confirmation you
never observed can simply be re-submitted.

### Idempotent retry semantics

A failed or timed-out invocation of the enforcer must be safe to re-run, and
the recovery paths are designed so that it is:

- **The enforcer never mutates contract state.** It reads, computes a byte
  footprint, and reports. Re-running it after any failure is always safe.
- **Restoration is idempotent.** As above, restoring an already-live entry is a
  no-op, so a restore whose confirmation was lost can be re-submitted without
  checking first.
- **Retries are bounded and jittered.** The enforcer retries a skewed snapshot
  a fixed number of times with backoff, then exits non-zero with a
  `ReadSkewError` rather than looping forever. A non-zero exit means "retry the
  whole invocation", not "the contract is broken".
- **A timed-out invocation leaves no partial state.** Because the enforcer
  writes nothing, a timeout mid-run is indistinguishable from a run that never
  started. Re-run it.

The one thing that is *not* idempotent is submitting a `RestoreFootprint`
transaction without confirming it. Always confirm before re-deriving keys, so
that you are reacting to the chain's state and not to your own unconfirmed
submission.

### Usage examples

Check the footprint of a single stream:

```bash
python3 script/event-byte-budget.py --contract "$STREAM_CONTRACT_ID" --stream-id 42
```

Check every stream in the contract, with the read-skew barrier enabled:

```bash
python3 script/event-byte-budget.py --contract "$STREAM_CONTRACT_ID" --all --pin-ledger
```

Emit machine-readable output for a monitoring pipeline:

```bash
python3 script/event-byte-budget.py --contract "$STREAM_CONTRACT_ID" --all --json
```

### Troubleshooting

| symptom | likely cause | action |
|---|---|---|
| `ArchivedEntryError` for a stream id below `stream_count()` | persistent entry TTL lapsed | restore with `tools/sdk/restore_footprint.ts`, confirm, re-run |
| `ReadSkewError` after the retry budget | RPC replicas disagreeing on `latestLedger` | re-run; if it persists, point at a single node you control |
| `latestLedger` went backwards mid-run | backend catching up after a restart | treated as skew and retried automatically; no action unless it persists |
| footprint report omits a stream you know exists | the stream is archived, or the read was skewed | check for `ArchivedEntryError`; if absent, re-run with `--pin-ledger` |
| invocation timed out | RPC latency, not contract state | re-run; the enforcer writes nothing |
| `RestoreFootprint` submitted but never confirmed | RPC indexer lag | re-submit; restoration is idempotent |

See `docs/KNOWN-LIMITATIONS.md` §1 for the underlying constraint on persistent
entry lifetime and why archival is expected rather than exceptional for
long-lived streams.

## What this is not

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
