"""Offline SMB identity and publication tests; all OS commands are mocked."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from uqmc_recovery import smb_mount as smb
from uqmc_recovery import server_sync as sync


class SMBFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.point = self.root / 'Volumes' / 'appdata'
        self.destination = self.point / 'viewer'
        self.endpoint = 'smb://fixture.invalid/appdata'
        self.config = {'enabled': True, 'destination_directory': str(self.destination),
                       'mount_point': str(self.point), 'smb_url': self.endpoint,
                       'auto_mount': True}
        self.entry = {'mount_point': str(self.point), 'smb_url': self.endpoint}
        # Any unmocked OS invocation is a test failure, never a real mount/network call.
        self.os_run = self.enterContext(patch.object(smb.subprocess, 'run',
            side_effect=AssertionError('Unexpected OS command in offline test')))

    def make_destination(self):
        self.destination.mkdir(parents=True)


class SMBMountTests(SMBFixture):
    def test_existing_exact_mount_is_verified_without_mount_command(self):
        self.make_destination()
        reader = Mock(return_value=[self.entry]); mounter = Mock()
        result = smb.ensure_mount(self.config, apply=True, reader=reader, mounter=mounter)
        self.assertEqual(result, {**self.entry, 'verified': True})
        reader.assert_called_once_with(); mounter.assert_not_called()

    def test_wrong_server_or_share_is_never_remounted(self):
        self.make_destination()
        for endpoint in ('smb://other.invalid/appdata', 'smb://fixture.invalid/other'):
            with self.subTest(endpoint=endpoint):
                mounter = Mock()
                reader = Mock(return_value=[{**self.entry, 'smb_url': endpoint}])
                with self.assertRaisesRegex(ValueError, '^SERVER_SYNC_MOUNT_IDENTITY_CONFLICT$'):
                    smb.ensure_mount(self.config, apply=True, reader=reader, mounter=mounter)
                mounter.assert_not_called()

    def test_missing_mount_succeeds_only_after_exact_mount_observed(self):
        reader = Mock(side_effect=[[], [self.entry]])
        mounter = Mock(side_effect=lambda _: self.make_destination())
        result = smb.ensure_mount(self.config, apply=True, reader=reader, mounter=mounter)
        self.assertTrue(result['verified'])
        mounter.assert_called_once_with(self.endpoint)
        self.assertEqual(reader.call_count, 2)

    def test_populated_local_volume_directory_is_not_mounted_over(self):
        self.make_destination()
        marker = self.destination / 'local.txt'; marker.write_text('local content')
        mounter = Mock()
        with self.assertRaisesRegex(ValueError, '^SERVER_SYNC_UNMOUNTED_LOCAL_DIRECTORY_NOT_EMPTY$'):
            smb.ensure_mount(self.config, apply=True, reader=lambda: [], mounter=mounter)
        mounter.assert_not_called()
        self.assertEqual(marker.read_text(), 'local content')

    def test_read_only_missing_mount_never_mounts_or_creates_directories(self):
        mounter = Mock()
        with self.assertRaisesRegex(ValueError, '^SERVER_SYNC_SMB_MOUNT_NOT_VERIFIED$'):
            smb.ensure_mount(self.config, reader=lambda: [], mounter=mounter)
        mounter.assert_not_called()
        self.assertFalse(self.point.exists())

    def test_disabled_auto_mount_does_not_dispatch(self):
        mounter = Mock()
        with self.assertRaisesRegex(ValueError, '^SERVER_SYNC_SMB_MOUNT_NOT_VERIFIED$'):
            smb.ensure_mount({**self.config, 'auto_mount': False}, apply=True,
                             reader=lambda: [], mounter=mounter)
        mounter.assert_not_called()

    def test_mount_success_without_mount_table_proof_is_rejected(self):
        mounter = Mock()
        with self.assertRaisesRegex(ValueError, '^SERVER_SYNC_SMB_MOUNT_NOT_VERIFIED$'):
            smb.ensure_mount(self.config, apply=True, reader=lambda: [], mounter=mounter)
        mounter.assert_called_once_with(self.endpoint)
        self.assertFalse(self.point.exists())

    def test_mount_timeout_without_observed_mount_is_reported_once(self):
        mounter = Mock(side_effect=ValueError('SERVER_SYNC_MOUNT_TIMEOUT'))
        reader = Mock(return_value=[])
        with self.assertRaisesRegex(ValueError, '^SERVER_SYNC_MOUNT_TIMEOUT$'):
            smb.ensure_mount(self.config, apply=True, reader=reader, mounter=mounter)
        mounter.assert_called_once_with(self.endpoint)
        self.assertEqual(reader.call_count, 2)

    def test_timeout_can_reconcile_exact_mount_without_second_dispatch(self):
        def mounted_then_timeout(_):
            self.make_destination()
            raise ValueError('SERVER_SYNC_MOUNT_TIMEOUT')
        mounter = Mock(side_effect=mounted_then_timeout)
        result = smb.ensure_mount(self.config, apply=True,
            reader=Mock(side_effect=[[], [self.entry]]), mounter=mounter)
        self.assertTrue(result['verified'])
        mounter.assert_called_once_with(self.endpoint)

    def test_volumes_destination_requires_pinned_identity(self):
        with self.assertRaisesRegex(ValueError, '^SERVER_SYNC_SMB_IDENTITY_NOT_CONFIGURED$'):
            smb.ensure_mount({'destination_directory': '/Volumes/offline-fixture/viewer'})

    def test_symlink_destination_and_duplicate_mount_proof_are_rejected(self):
        self.point.mkdir(parents=True)
        outside = self.root / 'outside'; outside.mkdir()
        self.destination.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, '^SERVER_SYNC_DESTINATION_NOT_ON_VERIFIED_MOUNT$'):
            smb.ensure_mount(self.config, reader=lambda: [self.entry])
        with self.assertRaisesRegex(ValueError, '^SERVER_SYNC_MOUNT_IDENTITY_CONFLICT$'):
            smb.ensure_mount(self.config, reader=lambda: [self.entry, self.entry])

    def test_mount_table_strips_synthetic_userinfo_and_uses_bounded_command(self):
        raw = '//fixture-user:fixture-secret@FIXTURE.invalid/appdata on ' + str(self.point)
        response = SimpleNamespace(returncode=0, stdout=raw + ' (smbfs, nodev, nosuid)\n', stderr='')
        with patch.object(smb.subprocess, 'run', return_value=response) as run:
            entries = smb.mount_entries()
        self.assertEqual(entries, [self.entry])
        self.assertNotIn('fixture-user', repr(entries)); self.assertNotIn('fixture-secret', repr(entries))
        run.assert_called_once_with(['/sbin/mount', '-t', 'smbfs'], capture_output=True,
                                    text=True, timeout=5, check=False)

    def test_configured_credentials_and_malformed_endpoints_are_rejected(self):
        for endpoint in ('smb://fixture-user:fixture-secret@fixture.invalid/appdata',
                         'smb://fixture-user@fixture.invalid/appdata',
                         'https://fixture.invalid/appdata', 'smb://fixture.invalid/a/b',
                         'smb://fixture.invalid/appdata?query=yes', 'smb://fixture.invalid/a%0Ab'):
            with self.subTest(endpoint=endpoint):
                with self.assertRaisesRegex(ValueError, '^SERVER_SYNC_SMB_URL_INVALID$'):
                    smb.canonical_smb_url(endpoint)

    def test_mount_command_passes_url_as_argument_not_script_or_shell(self):
        endpoint = smb.canonical_smb_url('smb://fixture.invalid/a%22%3B%24%28echo%20injected%29')
        with patch.object(smb.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            smb.mount_guest(endpoint)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ['/usr/bin/osascript', '-e'])
        self.assertEqual(argv[3:], [endpoint])
        self.assertNotIn(endpoint, argv[2])
        self.assertIn('mount volume (item 1 of argv)', argv[2])
        self.assertEqual(run.call_args.kwargs, {'capture_output': True, 'timeout': 20, 'check': False})

    def test_os_mount_failures_have_sanitized_errors(self):
        cases = [(subprocess.TimeoutExpired('synthetic-private-command', 20), 'SERVER_SYNC_MOUNT_TIMEOUT'),
                 (OSError('synthetic-private-host'), 'SERVER_SYNC_MOUNT_COMMAND_UNAVAILABLE')]
        for error, expected in cases:
            with self.subTest(expected=expected), patch.object(smb.subprocess, 'run', side_effect=error):
                with self.assertRaisesRegex(ValueError, '^' + expected + '$'):
                    smb.mount_guest(self.endpoint)
        response = SimpleNamespace(returncode=1, stdout=b'synthetic-private-host', stderr=b'fixture-secret')
        with patch.object(smb.subprocess, 'run', return_value=response):
            with self.assertRaisesRegex(ValueError, '^SERVER_SYNC_GUEST_MOUNT_FAILED_LOGIN_MAY_BE_REQUIRED$'):
                smb.mount_guest(self.endpoint)

    def test_unavailable_mount_table_fails_closed(self):
        for failure in (OSError('private detail'), subprocess.TimeoutExpired('mount', 5)):
            with self.subTest(error=type(failure).__name__), patch.object(smb.subprocess, 'run', side_effect=failure):
                with self.assertRaisesRegex(ValueError, '^SERVER_SYNC_MOUNT_TABLE_UNAVAILABLE$'):
                    smb.mount_entries()
        with patch.object(smb.subprocess, 'run', return_value=SimpleNamespace(returncode=1)):
            with self.assertRaisesRegex(ValueError, '^SERVER_SYNC_MOUNT_TABLE_UNAVAILABLE$'):
                smb.mount_entries()


class SMBPublicationTests(SMBFixture):
    def setUp(self):
        super().setUp()
        self.directory = self.root / 'publication'; self.directory.mkdir()
        self.content = b'<!doctype html><html>offline verified viewer</html>'
        (self.directory / 'viewer.html').write_bytes(self.content)
        receipt = {'schema_version': 'uqmc.destination.publication.v1',
            'snapshot_id': 'offline-smb-fixture', 'journal_sequence': 10,
            'created_at_utc': '2026-10-23T00:00:00Z',
            'files': {'viewer.html': hashlib.sha256(self.content).hexdigest()}}
        (self.directory / 'publication.json').write_text(json.dumps(receipt))
        self.make_destination()
        self.target = self.destination / 'index.html'; self.target.write_bytes(b'<html>old</html>')
        self.reader = self.enterContext(patch.object(smb, 'mount_entries', return_value=[self.entry]))
        self.mounter = self.enterContext(patch.object(smb, 'mount_guest'))
        self.transfer = Mock(side_effect=self.copy)

    def copy(self, source, target):
        saved = json.loads((self.directory / 'server-sync.json').read_text())
        self.assertEqual(saved['state'], 'ATTEMPTED')
        target.write_bytes(source.read_bytes())
        return 0

    def run_sync(self, *, apply=True):
        return sync.sync_publication(self.directory, self.config, apply=apply, transfer=self.transfer)

    def test_exact_mount_fake_transfer_and_readback_verify(self):
        result = self.run_sync()
        self.assertEqual(result['state'], 'VERIFIED')
        self.assertEqual(self.target.read_bytes(), self.content)
        self.transfer.assert_called_once()
        self.mounter.assert_not_called()
        self.assertEqual(self.reader.call_count, 4)

    def test_read_only_missing_mount_does_not_transfer_or_write_receipts(self):
        self.reader.return_value = []
        result = self.run_sync(apply=False)
        self.assertEqual(result['reason'], 'SERVER_SYNC_SMB_MOUNT_NOT_VERIFIED')
        self.mounter.assert_not_called(); self.transfer.assert_not_called()
        self.assertFalse((self.directory / 'server-sync.json').exists())
        self.assertFalse((self.root / '.server-sync.lock').exists())

    def test_missing_mount_can_be_verified_before_fake_transfer(self):
        self.target.unlink(); self.destination.rmdir(); self.point.rmdir()
        self.reader.return_value = []
        def mounted(_):
            self.make_destination()
            self.reader.return_value = [self.entry]
        self.mounter.side_effect = mounted
        result = self.run_sync()
        self.assertEqual(result['state'], 'VERIFIED')
        self.assertEqual(self.target.read_bytes(), self.content)
        self.mounter.assert_called_once_with(self.endpoint)
        self.transfer.assert_called_once()

    def test_disappearing_mount_before_transfer_blocks_dispatch(self):
        self.reader.side_effect = [[self.entry], [self.entry], []]
        result = self.run_sync()
        self.assertEqual(result['state'], 'BLOCKED')
        self.assertEqual(result['reason'], 'SERVER_SYNC_SMB_MOUNT_NOT_VERIFIED')
        self.transfer.assert_not_called(); self.mounter.assert_not_called()
        self.assertEqual(self.target.read_bytes(), b'<html>old</html>')
        saved = json.loads((self.directory / 'server-sync.json').read_text())
        self.assertEqual(saved['state'], 'PREPARED')

    def test_disappearing_mount_before_readback_preserves_uncertain_result(self):
        self.reader.side_effect = [[self.entry], [self.entry], [self.entry], []]
        with patch.object(sync, '_destination_hash', wraps=sync._destination_hash) as read_hash:
            result = self.run_sync()
        self.assertEqual(result['state'], 'UNCERTAIN')
        self.assertEqual(result['reason'], 'SERVER_SYNC_READBACK_UNAVAILABLE')
        self.transfer.assert_called_once(); self.mounter.assert_not_called()
        self.assertEqual(read_hash.call_count, 2)
        saved = json.loads((self.directory / 'server-sync.json').read_text())
        self.assertEqual(saved['state'], 'UNCERTAIN')


if __name__ == '__main__':
    unittest.main()
