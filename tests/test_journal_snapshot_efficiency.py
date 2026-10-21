"""Snapshot integrity with one validation per captured evidence blob."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from uqmc_recovery import journal as module
from uqmc_recovery.journal import Journal, JournalCorrupt, canonical, digest


class SnapshotEfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = Journal(self.root / 'state')

    def test_repeated_refs_read_and_decode_source_once(self):
        ref = self.journal.evidence({'rows': list(range(250))})
        with self.journal.locked():
            for i in range(128):
                self.journal.append('FIXTURE', payload={'refs': [ref, {'nested': ref}]})
        source = self.journal.evidence_dir / (ref[7:] + '.json')
        with mock.patch.object(module, '_read', wraps=module._read) as read, \
                mock.patch.object(module, '_decode_evidence', wraps=module._decode_evidence) as decode:
            result = self.journal.backup(self.root / 'backup')
        self.assertEqual(sum(call.args[0] == source for call in read.call_args_list), 1)
        self.assertEqual(decode.call_count, 1)
        self.assertEqual(result['event_count'], 128)
        self.assertEqual(Journal(self.root / 'backup').read_evidence(ref), {'rows': list(range(250))})

    def test_unreferenced_corrupt_evidence_still_blocks_backup(self):
        ref = self.journal.evidence({'original': True})
        (self.journal.evidence_dir / (ref[7:] + '.json')).write_bytes(b'{}')
        with self.assertRaisesRegex(JournalCorrupt, 'hash mismatch'):
            self.journal.backup(self.root / 'backup')
        self.assertFalse((self.root / 'backup').exists())

    def test_missing_nested_reference_blocks_backup(self):
        self.journal.append('FIXTURE', payload={'nested': [{'ref': 'sha256:' + 'a' * 64}]})
        with self.assertRaisesRegex(JournalCorrupt, 'Missing or invalid evidence'):
            self.journal.backup(self.root / 'backup')

    def test_noncanonical_evidence_cannot_be_copied_even_with_matching_hash(self):
        raw = b'{"a": 1}'
        (self.journal.evidence_dir / (digest(raw) + '.json')).write_bytes(raw)
        with self.assertRaisesRegex(JournalCorrupt, 'not canonical'):
            self.journal.backup(self.root / 'backup')

    def test_snapshot_uses_exact_validated_bytes_despite_later_source_change(self):
        original = {'kept': 'valid captured bytes'}
        ref = self.journal.evidence(original)
        self.journal.append('FIXTURE', payload={'ref': ref})
        source = self.journal.evidence_dir / (ref[7:] + '.json')
        original_read = module._read
        changed = []
        def read_then_change(path):
            raw = original_read(path)
            if path == source and not changed:
                source.write_bytes(b'{}')
                changed.append(True)
            return raw
        with mock.patch.object(module, '_read', side_effect=read_then_change):
            self.journal.backup(self.root / 'backup')
        self.assertEqual(Journal(self.root / 'backup').read_evidence(ref), original)
        self.assertEqual(source.read_bytes(), b'{}')
        with self.assertRaises(JournalCorrupt):
            self.journal.read_evidence(ref)


if __name__ == '__main__':
    unittest.main()
