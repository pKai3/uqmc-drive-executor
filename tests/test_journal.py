from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from uqmc_recovery.journal import Journal, JournalBusy, JournalCorrupt, canonical


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.journal = Journal(self.base / "state")

    def tearDown(self):
        self.temp.cleanup()

    def test_chain_and_nested_lock(self):
        with self.journal.locked():
            first = self.journal.append("PREPARED", "move-1", 1, {"name": "é =SUM(A1)"})
            second = self.journal.append("ATTEMPTED", "move-1", 1)
        self.assertEqual(first["seq"], 1)
        self.assertEqual(second["prev_hash"], first["hash"])
        self.assertEqual(self.journal.records(), [first, second])
        self.assertEqual(len({first["event_id"], second["event_id"]}), 2)

    def test_private_modes(self):
        ref = self.journal.evidence({"sensitive": "exact name and permissions"})
        self.journal.append("PREPARED", payload={"evidence_ref": ref})
        for path in (self.journal.root, self.journal.evidence_dir, self.journal.pending_dir):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        for path in (self.journal.path, self.journal.lock_path, *self.journal.evidence_dir.iterdir()):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_evidence_content_addressed_and_verified(self):
        ref = self.journal.evidence({"name": "budget.xlsx", "parents": ["p"]})
        self.assertEqual(ref, self.journal.evidence({"parents": ["p"], "name": "budget.xlsx"}))
        self.assertEqual(len(list(self.journal.evidence_dir.iterdir())), 1)
        self.assertEqual(self.journal.read_evidence(ref)["name"], "budget.xlsx")
        next(self.journal.evidence_dir.iterdir()).write_text("{}")
        with self.assertRaises(JournalCorrupt):
            self.journal.read_evidence(ref)
        with self.assertRaises(JournalCorrupt):
            self.journal.evidence({"name": "budget.xlsx", "parents": ["p"]})

    def test_tamper_blocks_append_and_backup(self):
        self.journal.append("PREPARED", payload={"name": "before"})
        raw = self.journal.path.read_bytes().replace(b"before", b"manual")
        self.journal.path.write_bytes(raw)
        for action in (self.journal.records, lambda: self.journal.append("ATTEMPTED"),
                       lambda: self.journal.backup(self.base / "backup"),
                       lambda: self.journal.recover_to(self.base / "recovered")):
            with self.assertRaises(JournalCorrupt):
                action()
        self.assertEqual(self.journal.path.read_bytes(), raw)

    def test_torn_tail_is_preserved_and_recovery_is_separate(self):
        first = self.journal.append("ATTEMPTED", "operation", 1)
        with self.journal.path.open("ab") as stream:
            stream.write(b'{"partial_result":')
        raw = self.journal.path.read_bytes()
        with self.assertRaises(JournalCorrupt):
            self.journal.records()
        manifest = self.journal.recover_to(self.base / "recovered")
        self.assertTrue(manifest["reconciliation_required"])
        self.assertEqual(manifest["discarded_terminal_byte_count"], len(b'{"partial_result":'))
        self.assertEqual(self.journal.path.read_bytes(), raw)
        recovered = Journal(self.base / "recovered")
        self.assertEqual(recovered.records(), [first])
        self.assertEqual((recovered.root / "forensic/source-events.jsonl").read_bytes(), raw)
        recovered.append("UNCERTAIN", "operation", 1)
        self.assertEqual(self.journal.path.read_bytes(), raw)

    def test_torn_first_record_can_recover_empty_chain(self):
        self.journal.path.write_bytes(b'{"schema_')
        manifest = self.journal.recover_to(self.base / "recovered")
        self.assertEqual(manifest["event_count"], 0)
        self.assertTrue(manifest["reconciliation_required"])
        self.assertEqual(Journal(self.base / "recovered").records(), [])

    def test_backup_restore_and_no_overwrite(self):
        ref = self.journal.evidence({"exact_name": "Budget", "mime": "sheet"})
        saved = self.journal.append("PREPARED", payload={"evidence_ref": ref})
        manifest = self.journal.backup(self.base / "backup")
        restored = Journal(self.base / "backup")
        self.assertEqual(restored.records(), [saved])
        self.assertEqual(restored.read_evidence(ref), self.journal.read_evidence(ref))
        self.assertEqual(manifest["head_hash"], saved["hash"])
        self.journal.append("ATTEMPTED")
        self.assertEqual(restored.records(), [saved])
        with self.assertRaises(FileExistsError):
            self.journal.backup(self.base / "backup")

    def test_missing_evidence_blocks_backup(self):
        self.journal.append("PREPARED", payload={"evidence_ref": "sha256:" + "a" * 64})
        with self.assertRaises(JournalCorrupt):
            self.journal.backup(self.base / "backup")

    def test_same_object_other_thread_excluded(self):
        with self.journal.locked(), ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.journal.append, "ATTEMPTED")
            with self.assertRaises(JournalBusy):
                future.result()
        self.assertEqual(self.journal.records(), [])

    def test_other_instance_and_process_excluded(self):
        other = Journal(self.journal.root)
        with self.journal.locked():
            with self.assertRaises(JournalBusy):
                other.append("ATTEMPTED")
            script = (
                "from uqmc_recovery.journal import Journal,JournalBusy\n"
                "import sys\n"
                "try: Journal(sys.argv[1]).append('ATTEMPTED')\n"
                "except JournalBusy: sys.exit(23)\n"
            )
            outcome = subprocess.run([sys.executable, "-c", script, str(self.journal.root)],
                                     capture_output=True, text=True)
            self.assertEqual(outcome.returncode, 23, outcome.stderr)
        other.append("PREPARED")
        self.assertEqual(len(self.journal.records()), 1)

    def test_disk_failure_before_append_changes_nothing(self):
        first = self.journal.append("PREPARED")
        with mock.patch("uqmc_recovery.journal.os.write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.journal.append("ATTEMPTED")
        self.assertEqual(self.journal.records(), [first])

    def test_partial_write_failure_fails_closed(self):
        self.journal.append("PREPARED")
        original_write = os.write
        calls = []
        def partial(fd, data):
            if calls:
                raise OSError("disk full after partial write")
            calls.append(True)
            return original_write(fd, data[:10])
        with mock.patch("uqmc_recovery.journal.os.write", side_effect=partial):
            with self.assertRaises(OSError):
                self.journal.append("ATTEMPTED")
        with self.assertRaises(JournalCorrupt):
            self.journal.records()

    def test_partial_evidence_write_can_be_retried_without_overwrite(self):
        original_write = os.write
        calls = []
        def partial(fd, data):
            if calls:
                raise OSError("disk full after partial evidence")
            calls.append(True)
            return original_write(fd, data[:5])
        with mock.patch("uqmc_recovery.journal.os.write", side_effect=partial):
            with self.assertRaises(OSError):
                self.journal.evidence({"name": "exact name"})
        pending = list(self.journal.pending_dir.iterdir())
        self.assertEqual(len(pending), 1)
        partial_bytes = pending[0].read_bytes()
        self.assertEqual(list(self.journal.evidence_dir.iterdir()), [])
        ref = self.journal.evidence({"name": "exact name"})
        self.assertEqual(self.journal.read_evidence(ref), {"name": "exact name"})
        self.assertEqual(pending[0].read_bytes(), partial_bytes)
        manifest = self.journal.backup(self.base / "backup")
        self.assertEqual(manifest["pending_evidence_excluded"], 1)

    def test_fsync_failure_outcome_requires_reopen_not_rewrite(self):
        with mock.patch("uqmc_recovery.journal.os.fsync", side_effect=OSError("fsync failed")):
            with self.assertRaises(OSError):
                self.journal.append("ATTEMPTED")
        # It may be present even though the call failed; state must be reread.
        self.assertEqual(len(self.journal.records()), 1)
        self.assertEqual(self.journal.records()[0]["kind"], "ATTEMPTED")

    def test_interrupted_snapshot_is_not_usable(self):
        self.journal.append("PREPARED")
        original_write = os.write
        def fail_when_journal(fd, data):
            if bytes(data).startswith(b'{"at"'):
                raise OSError("snapshot disk full")
            return original_write(fd, data)
        with mock.patch("uqmc_recovery.journal.os.write", side_effect=fail_when_journal):
            with self.assertRaises(OSError):
                self.journal.backup(self.base / "backup")
        broken = Journal(self.base / "backup")
        with self.assertRaises(JournalCorrupt):
            broken.records()

    def test_no_symlink_files_or_directories(self):
        target = self.base / "target"
        target.write_text("do not change")
        self.journal.path.unlink()
        self.journal.path.symlink_to(target)
        with self.assertRaises(JournalCorrupt):
            self.journal.append("PREPARED")
        self.assertEqual(target.read_text(), "do not change")
        linked = self.base / "linked"
        linked.symlink_to(self.journal.root)
        with self.assertRaises(JournalCorrupt):
            Journal(linked)

    def test_invalid_candidate_does_not_append(self):
        for kwargs in ({"kind": ""}, {"kind": "x", "attempt": -1},
                       {"kind": "x", "payload": []}, {"kind": "x", "payload": {"bad": float("nan")}}):
            with self.assertRaises((JournalCorrupt, ValueError)):
                self.journal.append(**kwargs)
        self.assertEqual(self.journal.records(), [])


if __name__ == "__main__":
    unittest.main()
