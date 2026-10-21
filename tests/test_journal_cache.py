"""Lock-epoch cache checks use temporary synthetic journals only."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import uqmc_recovery.journal as module
from uqmc_recovery.journal import Journal, JournalCorrupt


class JournalCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.journal = Journal(self.base / 'state')

    def test_full_validation_once_per_outer_epoch_and_incremental_tail(self):
        with mock.patch.object(module, '_validate', wraps=module._validate) as validate:
            with self.journal.locked():
                epoch = self.journal._lock_epoch
                for number in range(12):
                    self.journal.append('PREPARED', 'fixture-' + str(number), 1)
                    self.assertEqual(len(self.journal.records()), number + 1)
                    self.assertEqual(len(self.journal.records_after(number)), 1)
                self.assertEqual(validate.call_count, 1)
                self.assertEqual(self.journal._lock_epoch, epoch)
            self.assertIsNone(self.journal._cached_records)
            self.assertEqual(len(self.journal.records_after(10)), 2)
            self.assertEqual(validate.call_count, 2)
            self.assertGreater(self.journal._lock_epoch, epoch)

    def test_cached_records_and_append_return_do_not_alias_caller_payloads(self):
        payload = {'nested': {'values': [1]}}
        with self.journal.locked():
            saved = self.journal.append('PREPARED', payload=payload)
            payload['nested']['values'].append(2)
            saved['payload']['nested']['values'].append(3)
            outward = self.journal.records()
            outward[0]['payload']['nested']['values'].append(4)
            tail = self.journal.records_after(0)
            tail[0]['payload']['nested']['values'].append(5)
            self.assertEqual(self.journal.records()[0]['payload'], {'nested': {'values': [1]}})
        self.assertEqual(self.journal.records()[0]['payload'], {'nested': {'values': [1]}})

    def test_cursor_validation_and_empty_tail(self):
        self.journal.append('PREPARED')
        for cursor in (-1, True, 1.0, '0', None):
            with self.subTest(cursor=cursor), self.assertRaises(ValueError):
                self.journal.records_after(cursor)
        self.assertEqual(self.journal.records_after(1), [])
        with self.assertRaises(JournalCorrupt):
            self.journal.records_after(2)

    def test_concurrent_appends_keep_one_verified_chain(self):
        start = threading.Barrier(8)
        def append(number):
            if number < 8:
                start.wait(timeout=5)
            row = self.journal.append('ATTEMPTED', 'fixture-' + str(number), 1)
            self.assertEqual(self.journal.records_after(row['seq'] - 1)[0], row)
            return row
        with mock.patch.object(module, '_validate', wraps=module._validate) as validate:
            with self.journal.shared_locked():
                with ThreadPoolExecutor(max_workers=8) as pool:
                    rows = list(pool.map(append, range(32)))
                self.assertEqual(validate.call_count, 1)
                self.assertEqual({r['seq'] for r in rows}, set(range(1, 33)))
                self.assertEqual(len(self.journal.records_after(24)), 8)
        self.assertEqual(len(Journal(self.journal.root).records()), 32)

    def test_cached_same_size_edit_is_detected_even_with_mtime_restored(self):
        self.journal.append('PREPARED', payload={'value': 'before'})
        with self.journal.locked():
            self.journal.records()
            before = self.journal.path.stat()
            raw = self.journal.path.read_bytes().replace(b'before', b'edited')
            self.journal.path.write_bytes(raw)
            os.utime(self.journal.path, ns=(before.st_atime_ns, before.st_mtime_ns))
            with self.assertRaises(JournalCorrupt):
                self.journal.records_after(0)
            self.assertTrue(self.journal._cache_poisoned)
            with self.assertRaises(JournalCorrupt):
                self.journal.append('ATTEMPTED')

    def test_cached_replacement_and_truncation_are_detected(self):
        for replacement in (False, True):
            with self.subTest(replacement=replacement):
                journal = Journal(self.base / ('replace' if replacement else 'truncate'))
                journal.append('PREPARED')
                with journal.locked():
                    journal.records()
                    if replacement:
                        other = self.base / 'replacement.jsonl'
                        other.write_bytes(journal.path.read_bytes())
                        other.chmod(0o600)
                        os.replace(other, journal.path)
                    else:
                        journal.path.write_bytes(b'')
                    with self.assertRaises(JournalCorrupt):
                        journal.records()

    def test_valid_external_append_during_epoch_is_not_silently_accepted(self):
        self.journal.append('PREPARED')
        with self.journal.locked():
            self.journal.records()
            # Simulate an uncooperative writer with a valid separately built line.
            prior = self.journal.records()[0]
            next_row = dict(prior, seq=2, prev_hash=prior['hash'], event_id='00000000-0000-4000-8000-000000000002')
            body = {k:v for k,v in next_row.items() if k != 'hash'}
            next_row['hash'] = module.digest(module.canonical(body))
            with self.journal.path.open('ab') as stream:
                stream.write(module.canonical(next_row) + b'\n')
            with self.assertRaises(JournalCorrupt):
                self.journal.records_after(1)
        # Reacquiring deliberately performs a complete validation of current disk.
        self.assertEqual(len(self.journal.records()), 2)

    def test_append_readback_failure_never_updates_memory_tip(self):
        self.journal.append('PREPARED')
        with self.journal.locked():
            self.journal.records()
            with mock.patch.object(module.os, 'pread', return_value=b'wrong'):
                with self.assertRaises(JournalCorrupt):
                    self.journal.append('ATTEMPTED')
            self.assertIsNone(self.journal._cached_records)
            with self.assertRaises(JournalCorrupt):
                self.journal.records()
        reopened = Journal(self.journal.root)
        self.assertEqual([r['kind'] for r in reopened.records()], ['PREPARED', 'ATTEMPTED'])

    def test_partial_append_crash_still_requires_separate_recovery(self):
        self.journal.append('PREPARED')
        original_write = os.write
        calls = []
        def torn(fd, raw):
            if calls:
                raise OSError('fixture crash')
            calls.append(True)
            return original_write(fd, raw[:9])
        with self.journal.locked():
            self.journal.records()
            with mock.patch.object(module.os, 'write', side_effect=torn):
                with self.assertRaises(OSError):
                    self.journal.append('ATTEMPTED')
            self.assertIsNone(self.journal._cached_records)
        reopened = Journal(self.journal.root)
        with self.assertRaises(JournalCorrupt):
            reopened.records()
        recovery = reopened.recover_to(self.base / 'recovered')
        self.assertTrue(recovery['reconciliation_required'])
        self.assertEqual(len(Journal(self.base / 'recovered').records()), 1)

    def test_backup_revalidates_full_chain_and_private_reads_do_not_change_ctime(self):
        self.journal.append('PREPARED')
        with self.journal.locked():
            self.journal.records()
            before = self.journal.path.stat().st_ctime_ns
            with mock.patch.object(module, '_validate', wraps=module._validate) as validate:
                self.journal.backup(self.base / 'backup')
                self.assertGreaterEqual(validate.call_count, 2)
            self.assertEqual(self.journal.path.stat().st_ctime_ns, before)
            self.assertEqual(len(self.journal.records()), 1)


if __name__ == '__main__':
    unittest.main()
