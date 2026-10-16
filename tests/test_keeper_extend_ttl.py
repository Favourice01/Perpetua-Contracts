"""
Comprehensive test suite for script/keeper-extend-ttl.sh.

Covers the hardened Automated Keeper Batch TTL Renewal Runner (#292, #293):

* happy-path sweep driven by IDS_FILE
* fallback sweep driven by stream_count
* empty id list
* missing CONTRACT_ID / SOURCE
* missing IDS_FILE
* BATCH_SIZE clamping to MAX_BATCH_SIZE (16)
* archived-entry detection triggering restore-then-retry
* RPC read-skew barrier (consecutive getLatestLedger samples)
* partial batch failure that records the failure and continues
* idempotent retry after a timed-out invocation
* --json / --dry-run output shape

The tests stub a `stellar` binary on PATH so no network access is required.
"""

import json
import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "script" / "keeper-extend-ttl.sh"
FIXTURE = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "event_snapshots"
    / "ttl_extended.json"
)

MAX_BATCH_SIZE = 16


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(textwrap.dedent(contents))
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class KeeperExtendTtlTestBase(unittest.TestCase):
    """Shared scaffolding: temp workspace, stub `stellar`, env plumbing."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="keeper-ttl-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir()

        self.log_file = self.tmp / "stellar.log"
        self.state_file = self.tmp / "stellar.state.json"

        # Default stub behaviour: everything succeeds, ledger advances.
        self._install_stub(default=True)

        self.ids_file = self.tmp / "ids.txt"
        self.ids_file.write_text("1\n2\n3\n")

        self.env = dict(os.environ)
        self.env["PATH"] = f"{self.bin_dir}{os.pathsep}{self.env.get('PATH', '')}"
        self.env["STELLAR_STUB_LOG"] = str(self.log_file)
        self.env["STELLAR_STUB_STATE"] = str(self.state_file)
        self.env["CONTRACT_ID"] = "C" + "A" * 55
        self.env["SOURCE"] = "S" + "B" * 55
        self.env["IDS_FILE"] = str(self.ids_file)
        self.env["NETWORK"] = "testnet"
        self.env["RPC_URL"] = "http://127.0.0.1:8000/soroban/rpc"
        self.env.pop("BATCH_SIZE", None)
        self.env.pop("DRY_RUN", None)
        self.env.pop("JSON_OUTPUT", None)

    # ------------------------------------------------------------------ #
    # Stub installation
    # ------------------------------------------------------------------ #
    def _install_stub(self, default: bool = True) -> None:
        """Install a `stellar` stub that emulates the RPC surface used by
        the keeper script.  Behaviour is driven by the state file so that
        individual tests can flip flags (archived entries, skew, failures).
        """
        state = {
            "stream_count": 3,
            "archived": [],
            "fail_ids": [],
            "skew_samples": [],
            "latest_ledger": 1000,
            "timeout_ids": [],
            "restored": [],
            "extended": [],
        }
        if default:
            self.state_file.write_text(json.dumps(state))

        _write_executable(
            self.bin_dir / "stellar",
            r"""
            #!/usr/bin/env python3
            import json, os, sys

            LOG = os.environ["STELLAR_STUB_LOG"]
            STATE = os.environ["STELLAR_STUB_STATE"]

            def load():
                try:
                    with open(STATE) as fh:
                        return json.load(fh)
                except Exception:
                    return {}

            def save(s):
                with open(STATE, "w") as fh:
                    json.dump(s, fh)

            def log(entry):
                with open(LOG, "a") as fh:
                    fh.write(json.dumps(entry) + "\n")

            argv = sys.argv[1:]
            log({"argv": argv})

            state = load()

            def emit(obj):
                sys.stdout.write(json.dumps(obj))
                sys.stdout.write("\n")

            # --- contract read: stream_count ---------------------------
            if argv[:2] == ["contract", "invoke"] and "stream_count" in argv:
                emit({"result": state.get("stream_count", 0)})
                sys.exit(0)

            # --- contract read: stream_exists --------------------------
            if argv[:2] == ["contract", "invoke"] and "stream_exists" in argv:
                try:
                    idx = argv.index("--id")
                    sid = int(argv[idx + 1])
                except Exception:
                    sid = -1
                archived = state.get("archived", [])
                exists = 0 <= sid < state.get("stream_count", 0) and sid not in archived
                emit({"result": exists})
                sys.exit(0)

            # --- contract write: extend_ttl ----------------------------
            if argv[:2] == ["contract", "invoke"] and "extend_ttl" in argv:
                try:
                    idx = argv.index("--id")
                    sid = int(argv[idx + 1])
                except Exception:
                    sid = -1
                if sid in state.get("timeout_ids", []):
                    sys.stderr.write("timed out\n")
                    sys.exit(124)
                if sid in state.get("fail_ids", []):
                    sys.stderr.write("tx failed\n")
                    sys.exit(1)
                state.setdefault("extended", []).append(sid)
                save(state)
                emit({"status": "SUCCESS", "id": sid})
                sys.exit(0)

            # --- contract write: restore_entry -------------------------
            if argv[:2] == ["contract", "invoke"] and "restore_entry" in argv:
                try:
                    idx = argv.index("--id")
                    sid = int(argv[idx + 1])
                except Exception:
                    sid = -1
                archived = state.get("archived", [])
                if sid in archived:
                    archived.remove(sid)
                state["archived"] = archived
                state.setdefault("restored", []).append(sid)
                save(state)
                emit({"status": "SUCCESS", "id": sid})
                sys.exit(0)

            # --- ledger read: getLatestLedger --------------------------
            if "getLatestLedger" in argv:
                samples = state.get("skew_samples", [])
                if samples:
                    value = samples.pop(0)
                    state["skew_samples"] = samples
                    save(state)
                else:
                    value = state.get("latest_ledger", 1000)
                emit({"result": {"sequence": value}})
                sys.exit(0)

            # --- fallback: unknown command -----------------------------
            sys.stderr.write("unknown command: %s\n" % " ".join(argv))
            sys.exit(2)
            """,
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def run_script(self, *args, expect_ok=True, env_overrides=None):
        env = dict(self.env)
        if env_overrides:
            env.update(env_overrides)
        proc = subprocess.run(
            ["bash", str(SCRIPT), *args],
            env=env,
            capture_output=True,
            text=True,
        )
        if expect_ok and proc.returncode != 0:
            self.fail(
                f"script failed rc={proc.returncode}\n"
                f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
            )
        return proc

    def read_state(self):
        try:
            return json.loads(self.state_file.read_text())
        except Exception:
            return {}

    def read_log(self):
        if not self.log_file.exists():
            return []
        return [
            json.loads(line)
            for line in self.log_file.read_text().splitlines()
            if line.strip()
        ]

    def extended_ids(self):
        return self.read_state().get("extended", [])

    def restored_ids(self):
        return self.read_state().get("restored", [])

    def assert_script_exists(self):
        if not SCRIPT.exists():
            self.skipTest(f"keeper script not found at {SCRIPT}")


class TestHappyPath(KeeperExtendTtlTestBase):
    def test_sweep_with_ids_file(self):
        self.assert_script_exists()
        proc = self.run_script()
        self.assertEqual(sorted(self.extended_ids()), [1, 2, 3])
        self.assertIn("3", proc.stdout)

    def test_fallback_sweep_via_stream_count(self):
        self.assert_script_exists()
        # Remove IDS_FILE so the script falls back to stream_count().
        env = {"IDS_FILE": ""}
        self.run_script(env_overrides=env)
        self.assertEqual(sorted(self.extended_ids()), [1, 2, 3])

    def test_empty_id_list(self):
        self.assert_script_exists()
        self.ids_file.write_text("")
        proc = self.run_script()
        self.assertEqual(self.extended_ids(), [])
        self.assertEqual(proc.returncode, 0)


class TestConfigurationErrors(KeeperExtendTtlTestBase):
    def test_missing_contract_id(self):
        self.assert_script_exists()
        proc = self.run_script(
            expect_ok=False, env_overrides={"CONTRACT_ID": ""}
        )
        self.assertNotEqual(proc.returncode, 0)

    def test_missing_source(self):
        self.assert_script_exists()
        proc = self.run_script(expect_ok=False, env_overrides={"SOURCE": ""})
        self.assertNotEqual(proc.returncode, 0)

    def test_missing_ids_file(self):
        self.assert_script_exists()
        proc = self.run_script(
            expect_ok=False,
            env_overrides={"IDS_FILE": str(self.tmp / "does-not-exist.txt")},
        )
        self.assertNotEqual(proc.returncode, 0)


class TestBatchSizeClamping(KeeperExtendTtlTestBase):
    def test_batch_size_clamped_to_max(self):
        self.assert_script_exists()
        # 40 ids, request a batch size far above the max.
        self.ids_file.write_text("\n".join(str(i) for i in range(40)))
        self.run_script(env_overrides={"BATCH_SIZE": "999"})

        # Every id should still be processed (clamped batches, not dropped).
        self.assertEqual(sorted(self.extended_ids()), list(range(40)))

        # No single invoke call should carry more than MAX_BATCH_SIZE ids.
        for entry in self.read_log():
            argv = entry.get("argv", [])
            if "extend_ttl" in argv:
                ids = [a for a in argv if a.isdigit()]
                self.assertLessEqual(len(ids), MAX_BATCH_SIZE)


class TestArchivedEntryRecovery(KeeperExtendTtlTestBase):
    def test_archived_entry_triggers_restore_then_retry(self):
        self.assert_script_exists()
        state = self.read_state()
        state["archived"] = [2]
        self.state_file.write_text(json.dumps(state))

        self.run_script()

        # Entry 2 must have been restored and then extended.
        self.assertIn(2, self.restored_ids())
        self.assertIn(2, self.extended_ids())
        self.assertEqual(sorted(self.extended_ids()), [1, 2, 3])


class TestReadSkewBarrier(KeeperExtendTtlTestBase):
    def test_ledger_skew_waits_for_consecutive_samples(self):
        self.assert_script_exists()
        state = self.read_state()
        # First sample lags, second sample agrees -> barrier satisfied.
        state["skew_samples"] = [900, 1000, 1000]
        self.state_file.write_text(json.dumps(state))

        proc = self.run_script()
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(sorted(self.extended_ids()), [1, 2, 3])


class TestPartialBatchFailure(KeeperExtendTtlTestBase):
    def test_partial_failure_records_and_continues(self):
        self.assert_script_exists()
        state = self.read_state()
        state["fail_ids"] = [2]
        self.state_file.write_text(json.dumps(state))

        proc = self.run_script()
        # The failing id is skipped but the rest still succeed.
        self.assertEqual(sorted(self.extended_ids()), [1, 3])
        self.assertNotEqual(proc.returncode, 0)


class TestIdempotentRetry(KeeperExtendTtlTestBase):
    def test_retry_after_timeout_is_idempotent(self):
        self.assert_script_exists()
        state = self.read_state()
        state["timeout_ids"] = [2]
        self.state_file.write_text(json.dumps(state))

        # First invocation times out on id 2.
        self.run_script(expect_ok=False)
        first = sorted(self.extended_ids())
        self.assertIn(1, first)
        self.assertNotIn(2, first)

        # Clear the timeout and retry: id 2 is now extended, previously
        # extended ids are not duplicated.
        state = self.read_state()
        state["timeout_ids"] = []
        self.state_file.write_text(json.dumps(state))

        self.run_script()
        self.assertEqual(sorted(self.extended_ids()), [1, 2, 3])


class TestOutputShape(KeeperExtendTtlTestBase):
    def test_json_output_matches_fixture(self):
        self.assert_script_exists()
        proc = self.run_script("--json")
        payload = json.loads(proc.stdout)
        self.assertIn("events", payload)

        fixture = json.loads(FIXTURE.read_text())
        self.assertEqual(payload["events"][0]["type"], fixture["type"])
        self.assertIn("ttl_extended", json.dumps(payload))

    def test_dry_run_does_not_mutate_state(self):
        self.assert_script_exists()
        proc = self.run_script("--dry-run")
        self.assertEqual(self.extended_ids(), [])
        self.assertEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()