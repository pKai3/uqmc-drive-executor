"""Offline checkpoint reuse and interrupted-copy recovery regressions."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from uqmc_recovery import journal as jm
from uqmc_recovery import snapshot_store as sm
from uqmc_recovery.journal import Journal, JournalCorrupt


class IncrementalBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = Journal(self.root / 'state')
        self.ref = self.journal.evidence({'name': 'fixture.pdf', 'body': 'x' * 4096})
        self.journal.append('PREPARED', 'fixture', 1, {'ref': self.ref})
        self.name = self.ref[7:] + '.json'

    def backup(self, name):
        return self.journal.backup(self.root / name)

    def test_new_checkpoint_reuses_verified_bytes_even_after_reopen(self):
        first = self.backup('one')
        self.journal = Journal(self.journal.root)
        with mock.patch.object(jm, '_decode_evidence', wraps=jm._decode_evidence) as decode:
            second = self.backup('two')
        self.assertEqual(decode.call_count, 0)
        self.assertEqual(first['evidence_copy_stats']['copied_blob_count'], 1)
        self.assertEqual(second['evidence_copy_stats']['copied_blob_count'], 0)
        self.assertEqual(second['evidence_copy_stats']['reused_blob_count'], 1)
        a = self.root / 'one/evidence' / self.name
        b = self.root / 'two/evidence' / self.name
        self.assertEqual(a.stat().st_ino, b.stat().st_ino)
        self.assertNotEqual(a.stat().st_ino, (self.journal.evidence_dir / self.name).stat().st_ino)
        # Restore remains self-contained, with a full journal and ordinary files.
        self.assertEqual(Journal(self.root / 'two').read_evidence(self.ref)['name'], 'fixture.pdf')

    def test_additions_copy_only_new_evidence(self):
        self.backup('one')
        ref = self.journal.evidence({'new': True})
        self.journal.append('ATTEMPTED', 'fixture', 1, {'ref': ref})
        second = self.backup('two')
        self.assertEqual(second['event_count'], 2)
        stats = second['evidence_copy_stats']
        self.assertEqual(stats['copied_blob_count'], 1)
        self.assertEqual(stats['reused_blob_count'], 1)
        self.assertEqual(stats['copied_blob_bytes'], len(jm.canonical({'new': True})))
        self.assertEqual(Journal(self.root / 'two').records(), self.journal.records())
        self.assertEqual(len(Journal(self.root / 'one').records()), 1)

    def test_legacy_checkpoint_seeds_without_copying_evidence(self):
        legacy = self.journal._snapshot(self.root / 'legacy', recover=False)
        legacy_manifest = (self.root / 'legacy/snapshot-manifest.json').read_bytes()
        result = self.backup('new')
        self.assertEqual(result['evidence_copy_stats']['copied_blob_count'], 0)
        self.assertEqual(result['evidence_copy_stats']['seeded_blob_count'], 1)
        self.assertEqual((self.root / 'legacy/snapshot-manifest.json').read_bytes(), legacy_manifest)
        self.assertEqual(result['head_hash'], legacy['head_hash'])
        self.assertEqual(Journal(self.root / 'legacy').read_evidence(self.ref),
                         Journal(self.root / 'new').read_evidence(self.ref))

    def test_changed_source_is_revalidated_and_backup_remains_intact(self):
        self.backup('one')
        (self.journal.evidence_dir / self.name).write_bytes(b'{}')
        with self.assertRaisesRegex(JournalCorrupt, 'hash mismatch'):
            self.backup('two')
        self.assertEqual(Journal(self.root / 'one').read_evidence(self.ref)['name'], 'fixture.pdf')
        self.assertFalse((self.root / 'two').exists())

    def test_changed_shared_blob_is_revalidated_not_recopied_over(self):
        self.backup('one')
        pool = self.root / '.snapshot-evidence/blobs' / self.name
        pool.write_bytes(b'{}')
        with self.assertRaisesRegex(JournalCorrupt, 'hash mismatch'):
            self.backup('two')
        self.assertEqual(pool.read_bytes(), b'{}')
        with self.assertRaises(JournalCorrupt):
            Journal(self.root / 'one').read_evidence(self.ref)

    def test_broken_index_only_forces_full_verification(self):
        self.backup('one')
        (self.root / '.snapshot-evidence/index.json').write_text('{broken')
        with mock.patch.object(jm, '_decode_evidence', wraps=jm._decode_evidence) as decode:
            second = self.backup('two')
        self.assertEqual(decode.call_count, 1)
        self.assertEqual(second['evidence_copy_stats']['copied_blob_count'], 0)

    def test_interrupted_blob_publication_can_resume_to_new_checkpoint(self):
        original = jm._new_file
        def interrupted(path, data):
            original(path, data)
            if path.name.startswith('.pending-'):
                raise OSError('injected after durable staging before publish')
        with mock.patch.object(jm, '_new_file', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.backup('failed')
        self.assertFalse((self.root / 'failed').exists())
        self.assertEqual(len(list((self.root / '.snapshot-evidence').glob('.pending-*'))), 1)
        self.backup('restarted')
        self.assertEqual(Journal(self.root / 'restarted').records(), self.journal.records())

    def test_interrupted_manifest_or_index_never_becomes_usable_checkpoint(self):
        for phase in ('manifest', 'index'):
            destination = self.root / ('failed-' + phase)
            original = jm._new_file
            def interrupted(path, data):
                original(path, data)
                if ((phase == 'manifest' and path.name == 'snapshot-manifest.json')
                        or (phase == 'index' and path.name.startswith('.index-'))):
                    raise OSError('injected after durable write')
            with mock.patch.object(jm, '_new_file', side_effect=interrupted):
                with self.assertRaises(OSError):
                    self.journal.backup(destination)
            with self.assertRaisesRegex(JournalCorrupt, 'interrupted'):
                Journal(destination).records()
            result = self.backup('restarted-' + phase)
            self.assertEqual(result['evidence_copy_stats']['copied_blob_count'], 0)
            self.assertEqual(Journal(self.root / ('restarted-' + phase)).records(), self.journal.records())

    def test_checkpoint_directory_publication_always_has_durable_incomplete_marker(self):
        rename = os.rename
        for after in (False, True):
            destination = self.root / ('interrupted-publish-' + str(after))
            def interrupted(source, target):
                self.assertTrue((source / '.snapshot-incomplete').exists())
                if after:
                    rename(source, target)
                raise OSError('interrupted at checkpoint directory publication')
            with mock.patch.object(jm.os, 'rename', side_effect=interrupted):
                with self.assertRaises(OSError):
                    self.journal.backup(destination)
            if after:
                with self.assertRaisesRegex(JournalCorrupt, 'interrupted'):
                    Journal(destination).records()
            else:
                self.assertFalse(destination.exists())
            self.backup('recovered-publish-' + str(after))

    def test_recovery_from_torn_tail_keeps_legacy_independent_recovery(self):
        self.backup('one')
        with self.journal.path.open('ab') as stream:
            stream.write(b'{"partial":')
        with self.assertRaises(JournalCorrupt):
            self.backup('blocked')
        result = self.journal.recover_to(self.root / 'recovered')
        self.assertTrue(result['reconciliation_required'])
        self.assertEqual(Journal(self.root / 'recovered').read_evidence(self.ref)['name'], 'fixture.pdf')
        self.assertEqual(len(Journal(self.root / 'one').records()), 1)

    def test_different_journals_share_pool_under_serialized_index_lock(self):
        journals = [Journal(self.root / ('state-' + str(i))) for i in range(4)]
        for journal in journals:
            journal.evidence({'name': 'fixture.pdf', 'body': 'x' * 4096})
            journal.append('FIXTURE', payload={'ref': self.ref})
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda pair: pair[1].backup(self.root / ('copy-' + str(pair[0]))),
                                        enumerate(journals)))
        self.assertEqual(sum(x['evidence_copy_stats']['copied_blob_count'] for x in results), 1)
        for i in range(4):
            self.assertEqual(Journal(self.root / ('copy-' + str(i))).read_evidence(self.ref)['name'], 'fixture.pdf')

    def test_symlink_blob_is_rejected(self):
        self.backup('one')
        blob = self.root / '.snapshot-evidence/blobs' / self.name
        blob.unlink()
        blob.symlink_to(self.journal.evidence_dir / self.name)
        with self.assertRaisesRegex(JournalCorrupt, 'regular'):
            self.backup('two')

    def test_checkpoint_remains_readable_without_pool_directory_entry(self):
        self.backup('one')
        blob = self.root / '.snapshot-evidence/blobs' / self.name
        blob.unlink()
        self.assertEqual(Journal(self.root / 'one').read_evidence(self.ref)['name'], 'fixture.pdf')


if __name__ == '__main__':
    unittest.main()
