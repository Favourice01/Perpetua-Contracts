import {
  Address,
  Account,
  Horizon,
  Keypair,
  Operation,
  SorobanDataBuilder,
  Transaction,
  TransactionBuilder,
  nativeToScVal,
  xdr,
} from "@stellar/stellar-sdk";

export interface RestoreFootprintParams {
  contractId: string;
  streamId: bigint | number | string;
  source: string;
  networkPassphrase: string;
  horizonUrl: string;
  fee?: string;
  timeoutSeconds?: number;
}

export interface RestoreFootprintResult {
  xdr: string;
  streamKey: xdr.LedgerKey;
  transaction: Transaction;
}

/**
 * Archival detection for off-chain indexers.
 *
 * Soroban persistent entries (e.g. `DataKey::Stream(streamId)`) are evicted
 * from the live ledger once their TTL lapses. The contract's `stream_exists(id)`
 * read then returns `false` even though the stream was never closed, while the
 * monotonically increasing `stream_count()` still reports the id as allocated.
 *
 * The canonical detection rule is therefore:
 *
 *     stream_exists(id) == false && id < stream_count()
 *
 * Any id satisfying that predicate is *archived*, not absent, and must be
 * restored via a RestoreFootprint transaction before it can be read again.
 *
 * See `docs/soroban-rpc-read-skew.md` for the RPC read-skew caveats that make
 * this check necessary (a lagging RPC node may still report the entry as live)
 * and `docs/KNOWN-LIMITATIONS.md` §1 for the archival/restoration limitations.
 */
export function detectArchivedStream(
  streamExists: boolean,
  streamCount: bigint | number | string,
  streamId: bigint | number | string,
): boolean {
  const id = BigInt(streamId);
  const count = BigInt(streamCount);
  return streamExists === false && id < count;
}

export interface RestoreAndRetryParams extends RestoreFootprintParams {
  /** Maximum number of restore attempts before giving up. Defaults to 3. */
  maxAttempts?: number;
  /** Base backoff in milliseconds between attempts. Defaults to 1000. */
  backoffMs?: number;
  /** Optional sleep hook, injectable for tests. */
  sleep?: (ms: number) => Promise<void>;
}

export interface RestoreAndRetryResult<T> {
  value: T;
  /** Number of restore attempts that were required (0 = no restore needed). */
  attempts: number;
  /** True when a RestoreFootprint transaction was submitted. */
  restored: boolean;
  /** XDR of the last submitted RestoreFootprint transaction, if any. */
  restoreXdr?: string;
}

function defaultSleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Idempotent recovery wrapper for archived stream reads.
 *
 * Workflow (see `docs/soroban-rpc-read-skew.md` and
 * `docs/KNOWN-LIMITATIONS.md` §1):
 *
 *   1. Run `read()` against the current ledger state.
 *   2. If the entry is archived (per `detectArchivedStream`), build and submit
 *      the RestoreFootprint transaction for the stream's persistent ledger key.
 *   3. Re-run `read()` — restoration is idempotent, so a duplicate submission
 *      (e.g. after a timeout where the transaction actually landed) is safe.
 *   4. Retry with backoff up to `maxAttempts` to absorb RPC read skew, where a
 *      node may still serve a stale view of the ledger immediately after the
 *      restore is applied.
 *
 * `read` must be a pure, side-effect-free read of the stream state so that
 * re-running it after restoration cannot corrupt indexer state.
 */
export async function restoreAndRetry<T>(
  params: RestoreAndRetryParams,
  read: () => Promise<{ streamExists: boolean; streamCount: bigint | number | string; value: T }>,
): Promise<RestoreAndRetryResult<T>> {
  const maxAttempts = params.maxAttempts ?? 3;
  const backoffMs = params.backoffMs ?? 1000;
  const sleep = params.sleep ?? defaultSleep;

  let attempts = 0;
  let restored = false;
  let restoreXdr: string | undefined;

  for (let attempt = 0; attempt <= maxAttempts; attempt += 1) {
    const snapshot = await read();

    if (!detectArchivedStream(snapshot.streamExists, snapshot.streamCount, params.streamId)) {
      return { value: snapshot.value, attempts, restored, restoreXdr };
    }

    if (attempt === maxAttempts) {
      break;
    }

    const result = await buildSignedRestoreFootprint(params);
    restoreXdr = result.xdr;
    restored = true;
    attempts += 1;

    // Restoration is idempotent: re-submitting after a timeout is safe, and
    // the subsequent read absorbs any residual RPC read skew.
    await sleep(backoffMs * attempts);
  }

  throw new Error(
    `stream ${params.streamId.toString()} remained archived after ${maxAttempts} restore attempts; ` +
      "see docs/soroban-rpc-read-skew.md and docs/KNOWN-LIMITATIONS.md §1",
  );
}

export function buildRestoreFootprintTransaction(
  account: Account,
  key: xdr.LedgerKey,
  networkPassphrase: string,
  fee = "1000000",
  timeoutSeconds = 30,
): Transaction {
  const sorobanData = restoreFootprintData(key);
  return new TransactionBuilder(account, { fee, networkPassphrase })
    .setSorobanData(sorobanData)
    .addOperation(Operation.restoreFootprint({}))
    .setTimeout(timeoutSeconds)
    .build();
}

/** Build Soroban transaction data with the stream entry in the write footprint. */
export function restoreFootprintData(key: xdr.LedgerKey): xdr.SorobanTransactionData {
  return new SorobanDataBuilder()
    .setFootprint([], [key])
    .setResources(0, 0, 0)
    .build();
}

/** Build the persistent ledger key used by DataKey::Stream(streamId). */
export function streamLedgerKey(contractId: string, streamId: bigint | number | string): xdr.LedgerKey {
  const id = BigInt(streamId);
  if (id < 0n || id > 18_446_744_073_709_551_615n) {
    throw new Error("stream_id must be an unsigned 64-bit integer");
  }

  const key = xdr.ScVal.scvVec([
    xdr.ScVal.scvSymbol("Stream"),
    nativeToScVal(id, { type: "u64" }),
  ]);

  return xdr.LedgerKey.contractData(new xdr.LedgerKeyContractData({
    contract: Address.fromString(contractId).toScAddress(),
    key,
    durability: xdr.ContractDataDurability.persistent(),
  }));
}

/**
 * Build and sign the exact RestoreFootprint transaction for one stream entry.
 * The source secret is used only locally and is never sent to Horizon.
 */
export async function buildSignedRestoreFootprint(
  params: RestoreFootprintParams,
): Promise<RestoreFootprintResult> {
  const key = streamLedgerKey(params.contractId, params.streamId);
  const keypair = Keypair.fromSecret(params.source);
  const server = new Horizon.Server(params.horizonUrl);
  const account = await server.loadAccount(keypair.publicKey());
  const transaction = buildRestoreFootprintTransaction(
    account,
    key,
    params.networkPassphrase,
    params.fee,
    params.timeoutSeconds,
  );

  transaction.sign(keypair);
  return { xdr: transaction.toXDR(), streamKey: key, transaction };
}

function usage(): never {
  throw new Error(
    "Usage: restore_footprint.ts --contract <C...> --stream-id <id> " +
      "--source-secret <S...> --horizon <url> --network-passphrase <passphrase>",
  );
}

function option(args: Map<string, string>, name: string): string {
  const value = args.get(name);
  if (!value) {
    throw new Error(`Missing required option: ${name}`);
  }
  return value;
}

/** CLI entrypoint for operators; secrets are accepted as an argument for local use only. */
export async function main(argv = process.argv.slice(2)): Promise<void> {
  const args = new Map<string, string>();
  for (let index = 0; index < argv.length; index += 2) {
    const name = argv[index];
    const value = argv[index + 1];
    if (!name?.startsWith("--") || value === undefined) {
      usage();
    }
    args.set(name, value);
  }

  const result = await buildSignedRestoreFootprint({
    contractId: option(args, "--contract"),
    streamId: option(args, "--stream-id"),
    source: option(args, "--source-secret"),
    horizonUrl: option(args, "--horizon"),
    networkPassphrase: option(args, "--network-passphrase"),
  });
  process.stdout.write(`${result.xdr}\n`);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((error: unknown) => {
    process.stderr.write(`${error instanceof Error ? error.message : String(error)}\n`);
    process.exitCode = 1;
  });
}
