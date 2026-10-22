import copy
import tempfile
import unittest
from unittest.mock import patch

from uqmc_recovery.direct_transport import TransportError
from uqmc_recovery.folder_cleanup import cleanup_folder, operation_id
from uqmc_recovery.journal import Journal, canonical, digest
from uqmc_recovery.model import FOLDER, SafetyError


class FakeCleanupTransport:
    def __init__(self, journal, candidate):
        self.journal = journal
        self.raw = {
            'id': candidate['id'], 'name': candidate['before_name'],
            'parents': list(candidate['parents']), 'mimeType': FOLDER,
            'trashed': False, 'version': '1', 'modifiedTime': '2026-01-01T00:00:00Z',
            'capabilities': {'canRename': True}, '_http_etag': 'fixture-etag',
        }
        self.siblings = []
        self.calls = []
        self.failure = None
        self.after_list = None
        self.after_update = None

    def get_file(self, file_id):
        self.calls.append(('get', file_id))
        if self.failure == 'readback' and self.updates:
            raise TransportError('DRIVE_REQUEST_TIMEOUT', outcome='UNKNOWN')
        return copy.deepcopy(self.raw)

    def list_children(self, parent_id):
        self.calls.append(('list', parent_id))
        if self.failure == 'listing':
            raise TransportError('DRIVE_REQUEST_TIMEOUT', outcome='UNKNOWN')
        result = [copy.deepcopy(self.raw), *copy.deepcopy(self.siblings)]
        if self.after_list:
            self.after_list(self.raw)
        return result

    @property
    def updates(self):
        return [call for call in self.calls if call[0] == 'update']

    def update_metadata(self, intent, *, etag=None):
        # Simulate the remote boundary only after durable intent exists.
        events = self.journal.records()
        assert [event['kind'] for event in events[-2:]] == [
            'FOLDER_CLEANUP_PREPARED', 'FOLDER_CLEANUP_ATTEMPTED']
        assert events[-1]['payload']['intent'] == intent
        self.calls.append(('update', copy.deepcopy(intent), etag))
        if self.failure == 'before':
            raise TransportError('DRIVE_REQUEST_TIMEOUT', outcome='UNKNOWN')
        assert set(intent) == {'fileId', 'name'}
        self.raw['name'] = intent['name']
        self.raw['version'] = '2'
        if self.after_update:
            self.after_update(self.raw)
        if self.failure == 'after':
            raise TransportError('DRIVE_REQUEST_TIMEOUT', outcome='UNKNOWN')
        return copy.deepcopy(self.raw)


class FolderCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = Journal(self.temp.name)
        self.candidate = {'id': 'folder', 'before_name': 'Folder / Notes \t ',
                          'target_name': 'Folder / Notes', 'parents': ['parent']}
        self.authority_ref = self.journal.evidence({
            'schema_version': 'uqmc.folder-cleanup.authority.v1', 'decision': 'APPROVED',
            'root_id': 'root', 'folders': [self.candidate],
        })
        self.transport = FakeCleanupTransport(self.journal, self.candidate)

    def run_cleanup(self):
        return cleanup_folder(self.journal, self.transport, self.candidate, self.authority_ref)

    def test_name_only_update_is_journaled_before_dispatch_and_preserves_slash(self):
        result = self.run_cleanup()
        self.assertEqual(result['status'], 'VERIFIED')
        self.assertEqual(self.transport.updates, [
            ('update', {'fileId': 'folder', 'name': 'Folder / Notes'}, 'fixture-etag')])
        self.assertEqual(self.transport.raw['parents'], ['parent'])
        events = self.journal.records()
        self.assertEqual([e['kind'] for e in events], [
            'FOLDER_CLEANUP_PREPARED', 'FOLDER_CLEANUP_ATTEMPTED', 'FOLDER_CLEANUP_VERIFIED'])
        self.assertTrue(all(e['payload']['authority_ref'] == self.authority_ref for e in events))
        expected_op = 'cleanup-' + digest(canonical(self.candidate))[:24]
        self.assertEqual(operation_id(self.candidate), expected_op)
        self.assertTrue(all(e['operation_id'] == expected_op for e in events))
        self.assertEqual(self.journal.read_evidence(events[0]['payload']['before_ref'])['name'],
                         self.candidate['before_name'])
        observed = self.journal.read_evidence(result['observed_ref'])
        self.assertEqual((observed['name'], observed['parents'], observed['mimeType']),
                         ('Folder / Notes', ['parent'], FOLDER))

    def test_successful_resume_verifies_current_state_without_replaying(self):
        self.assertEqual(self.run_cleanup()['status'], 'VERIFIED')
        self.journal = Journal(self.temp.name)
        self.transport.journal = self.journal
        result = self.run_cleanup()
        self.assertEqual(result['status'], 'VERIFIED')
        self.assertTrue(result['attribution_reconciled'])
        self.assertEqual(len(self.transport.updates), 1)

    def test_lost_response_after_success_reconciles_target(self):
        self.transport.failure = 'after'
        result = self.run_cleanup()
        self.assertEqual(result['status'], 'VERIFIED')
        self.assertTrue(result['attribution_reconciled'])
        self.assertEqual(result['failure']['outcome'], 'UNKNOWN')
        self.assertEqual(len(self.transport.updates), 1)

    def test_unchanged_uncertain_attempt_does_not_redispatch(self):
        self.transport.failure = 'before'
        self.assertEqual(self.run_cleanup()['status'], 'UNCERTAIN')
        self.transport.failure = None
        self.journal = Journal(self.temp.name)
        self.transport.journal = self.journal
        result = self.run_cleanup()
        self.assertEqual(result['status'], 'UNCERTAIN')
        self.assertEqual(result['reason'], 'PRIOR_DISPATCH_UNRESOLVED_NO_RETRY')
        self.assertEqual(len(self.transport.updates), 1)

    def test_crash_after_attempt_before_remote_call_does_not_retry(self):
        append = self.journal.append
        def crash(kind, **kwargs):
            event = append(kind, **kwargs)
            if kind == 'FOLDER_CLEANUP_ATTEMPTED':
                raise KeyboardInterrupt()
            return event
        with patch.object(self.journal, 'append', crash), self.assertRaises(KeyboardInterrupt):
            self.run_cleanup()
        self.assertEqual(self.run_cleanup()['status'], 'UNCERTAIN')
        self.assertEqual(self.transport.updates, [])

    def test_crash_after_remote_success_before_verified_resumes_without_replay(self):
        append = self.journal.append
        def crash(kind, **kwargs):
            if kind == 'FOLDER_CLEANUP_VERIFIED':
                raise KeyboardInterrupt()
            return append(kind, **kwargs)
        with patch.object(self.journal, 'append', crash), self.assertRaises(KeyboardInterrupt):
            self.run_cleanup()
        self.assertEqual(self.run_cleanup()['status'], 'VERIFIED')
        self.assertEqual(len(self.transport.updates), 1)

    def test_exact_and_trailing_whitespace_sibling_collisions_block(self):
        for title in ('Folder / Notes', 'Folder / Notes ', 'Folder / Notes\t'):
            with self.subTest(title=title):
                self.transport.siblings = [{'id': 'other', 'name': title}]
                result = self.run_cleanup()
                self.assertEqual(result['status'], 'BLOCKED')
                self.assertEqual(result['reason'], 'SIBLING_NAME_COLLISION')
                self.assertEqual(result['collision_ids'], ['other'])
        self.assertEqual(self.transport.updates, [])

    def test_wrong_name_parent_mime_trashed_or_id_blocks_before_dispatch(self):
        original = copy.deepcopy(self.transport.raw)
        for key, value in [('name', 'External edit'), ('parents', ['other']),
                           ('mimeType', 'application/pdf'), ('trashed', True), ('id', 'other')]:
            with self.subTest(field=key):
                self.transport.raw = {**copy.deepcopy(original), key: value}
                result = self.run_cleanup()
                self.assertEqual(result['status'], 'BLOCKED')
                self.assertEqual(result['reason'], 'UNAPPROVED_CURRENT_STATE')
        self.assertEqual(self.transport.updates, [])

    def test_changes_during_listing_block_before_dispatch(self):
        for field, value in [('name', 'Changed'), ('parents', ['other']), ('version', '2'),
                             ('capabilities', {'canRename': False})]:
            with self.subTest(field=field):
                drive = FakeCleanupTransport(self.journal, self.candidate)
                drive.after_list = lambda raw, k=field, v=value: raw.update({k: v})
                result = cleanup_folder(self.journal, drive, self.candidate, self.authority_ref)
                self.assertEqual(result['status'], 'BLOCKED')
                self.assertEqual(result['reason'], 'CHANGED_DURING_PREFLIGHT')
                self.assertEqual(drive.updates, [])

    def test_readback_requires_unchanged_parent_and_folder_mime(self):
        for field, value in [('parents', ['other']), ('mimeType', 'application/pdf')]:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as path:
                journal = Journal(path)
                ref = journal.evidence(self.journal.read_evidence(self.authority_ref))
                drive = FakeCleanupTransport(journal, self.candidate)
                drive.after_update = lambda raw, k=field, v=value: raw.update({k: v})
                result = cleanup_folder(journal, drive, self.candidate, ref)
                self.assertEqual(result['status'], 'BLOCKED')
                self.assertEqual(result['reason'], 'READBACK_CONFLICT')
                self.assertEqual(len(drive.updates), 1)

    def test_verified_then_external_drift_blocks_without_overwrite(self):
        self.assertEqual(self.run_cleanup()['status'], 'VERIFIED')
        self.transport.raw['name'] = 'Changed by owner'
        result = self.run_cleanup()
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['reason'], 'UNAPPROVED_CURRENT_STATE')
        self.assertEqual(len(self.transport.updates), 1)

    def test_verified_then_external_reversion_blocks_without_overwrite(self):
        self.assertEqual(self.run_cleanup()['status'], 'VERIFIED')
        self.transport.raw['name'] = self.candidate['before_name']
        result = self.run_cleanup()
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(len(self.transport.updates), 1)

    def test_incomplete_listing_error_never_dispatches(self):
        self.transport.failure = 'listing'
        with self.assertRaises(TransportError):
            self.run_cleanup()
        self.assertEqual(self.transport.updates, [])
        self.assertEqual(self.journal.records(), [])

    def test_readback_unavailable_stays_uncertain_until_target_read(self):
        self.transport.failure = 'readback'
        result = self.run_cleanup()
        self.assertEqual(result['status'], 'UNCERTAIN')
        self.assertEqual(result['reason'], 'READBACK_UNAVAILABLE')
        self.transport.failure = None
        self.assertEqual(self.run_cleanup()['status'], 'VERIFIED')
        self.assertEqual(len(self.transport.updates), 1)

    def test_target_without_prior_dispatch_is_not_claimed_as_our_success(self):
        self.transport.raw['name'] = self.candidate['target_name']
        result = self.run_cleanup()
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertEqual(result['reason'], 'TARGET_WITHOUT_RECORDED_ATTEMPT')
        self.assertEqual(self.transport.updates, [])

    def test_authority_must_include_exact_candidate_and_approved_decision(self):
        for change in ({'decision': 'WITHDRAWN'}, {'folders': []},
                       {'schema_version': 'other'}):
            with self.subTest(change=change):
                ref = self.journal.evidence({**self.journal.read_evidence(self.authority_ref), **change})
                with self.assertRaisesRegex(SafetyError, 'FOLDER_CLEANUP_AUTHORITY_MISMATCH'):
                    cleanup_folder(self.journal, self.transport, self.candidate, ref)
        self.assertEqual(self.transport.calls, [])

    def test_non_trailing_cleanup_cannot_be_authorized_as_this_operation(self):
        candidate = {**self.candidate, 'target_name': 'Folder / Renamed'}
        ref = self.journal.evidence({**self.journal.read_evidence(self.authority_ref), 'folders': [candidate]})
        with self.assertRaisesRegex(SafetyError, 'TRAILING_WHITESPACE_CORRECTION_ONLY'):
            cleanup_folder(self.journal, self.transport, candidate, ref)
        self.assertEqual(self.transport.calls, [])


if __name__ == '__main__':
    unittest.main()
