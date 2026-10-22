"""Only temporary local directories and fake transfers; never deploy a viewer."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from uqmc_recovery import server_sync as sync
from uqmc_recovery import destination_publication as publication
from uqmc_recovery.destination import run_destination
from uqmc_recovery.destination_console import ConsoleReporter
import test_destination_publication as publication_fixture


def sha(value):
    return hashlib.sha256(value).hexdigest()


class Crash(BaseException):
    pass


class ServerSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / 'publication'
        self.directory.mkdir()
        self.destination = self.root / 'share'
        self.destination.mkdir()
        self.target = self.destination / 'index.html'
        self.target.write_bytes(b'<html>old</html>')
        self.content = b'<!doctype html><html>verified new viewer</html>'
        (self.directory / 'viewer.html').write_bytes(self.content)
        self.publication_receipt = {'schema_version': 'uqmc.destination.publication.v1',
            'snapshot_id': 'fixture-snapshot', 'journal_sequence': 10,
            'created_at_utc': '2026-10-22T01:00:00Z', 'files': {'viewer.html': sha(self.content)},
            'sheet': {'state': 'NOT_ATTEMPTED'}, 'html': {'state': 'NOT_ATTEMPTED'}}
        self.save_publication()
        self.config = {'enabled': True, 'destination_directory': str(self.destination)}
        self.calls = []

    def save_publication(self):
        (self.directory / 'publication.json').write_text(json.dumps(self.publication_receipt))

    def transfer(self, source, target):
        self.calls.append((source, target))
        receipt = json.loads((self.directory / 'server-sync.json').read_text())
        self.assertEqual(receipt['state'], 'ATTEMPTED')
        self.assertEqual(receipt['source_sha256'], sha(source.read_bytes()))
        target.write_bytes(source.read_bytes())
        return 0

    def run_sync(self, **kwargs):
        return sync.sync_publication(self.directory, self.config, transfer=self.transfer, **kwargs)

    def test_check_is_read_only_and_disabled_never_reads_source(self):
        self.assertEqual(self.run_sync()['state'], 'READY')
        self.assertEqual(self.target.read_bytes(), b'<html>old</html>')
        self.assertFalse((self.directory / 'server-sync.json').exists())
        self.assertFalse((self.root / '.server-sync.lock').exists())
        self.assertEqual(self.calls, [])
        for config in (None, {}, {'enabled': False}):
            self.assertEqual(sync.sync_publication('/not/a/publication', config, apply=True), {'state': 'DISABLED'})

    def test_transfer_fixed_target_verified_and_repeat_does_not_replay(self):
        before = sha(self.target.read_bytes())
        result = self.run_sync(apply=True)
        self.assertEqual(result['state'], 'VERIFIED')
        self.assertEqual(self.calls, [(self.directory.resolve() / 'viewer.html', self.target.resolve())])
        self.assertEqual(self.target.read_bytes(), self.content)
        self.assertEqual(result['before_sha256'], before)
        self.assertEqual(self.run_sync(apply=True)['before_sha256'], before)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual((self.directory / 'server-sync.json').stat().st_mode & 0o777, 0o600)

    def test_exact_rsync_argv_no_shell_and_bounded_process_timeout(self):
        with patch.object(sync.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            self.assertEqual(sync._transfer(Path('/source with spaces/viewer.html'), Path('/share/index.html')), 0)
        args, kwargs = run.call_args
        self.assertEqual(args[0], ['rsync', '-c', '--progress', '--', '/source with spaces/viewer.html', '/share/index.html'])
        self.assertEqual(kwargs, {'capture_output': True, 'timeout': 60, 'check': False})

    def test_invalid_html_or_hash_cannot_overwrite_server(self):
        for content in (b'', b'not html', b'<html>unsealed change</html>'):
            with self.subTest(content=content):
                (self.directory / 'viewer.html').write_bytes(content)
                self.assertEqual(self.run_sync(apply=True)['state'], 'BLOCKED')
                self.assertEqual(self.target.read_bytes(), b'<html>old</html>')
        self.assertEqual(self.calls, [])

    def test_invalid_receipt_cannot_authorize_copy(self):
        self.publication_receipt['schema_version'] = 'untrusted'
        self.save_publication()
        self.assertEqual(self.run_sync(apply=True)['reason'], 'SERVER_SYNC_PUBLICATION_RECEIPT_INVALID')
        self.assertEqual(self.calls, [])

    def test_missing_share_is_not_created(self):
        self.config['destination_directory'] = str(self.root / 'unmounted' / 'viewer')
        result = self.run_sync(apply=True)
        self.assertEqual(result['reason'], 'SERVER_SYNC_DESTINATION_UNAVAILABLE')
        self.assertFalse((self.root / 'unmounted').exists())
        self.assertEqual(self.calls, [])

    def test_symlink_target_does_not_overwrite_outside_file(self):
        self.target.unlink()
        outside = self.root / 'outside.html'
        outside.write_bytes(b'private outside')
        self.target.symlink_to(outside)
        self.assertEqual(self.run_sync(apply=True)['state'], 'BLOCKED')
        self.assertEqual(outside.read_bytes(), b'private outside')
        self.assertEqual(self.calls, [])

    def test_symlink_destination_directory_is_rejected(self):
        alias = self.root / 'share-alias'
        alias.symlink_to(self.destination, target_is_directory=True)
        self.config['destination_directory'] = str(alias)
        self.assertEqual(self.run_sync(apply=True)['reason'], 'SERVER_SYNC_DESTINATION_SYMLINK')
        self.assertEqual(self.calls, [])

    def test_terminal_transfer_failure_retries_only_after_unchanged_readback(self):
        def partial(source, target):
            self.calls.append(('failed', target))
            (target.parent / '.rsync-partial').write_bytes(b'partial temporary data')
            return 23
        failed = sync.sync_publication(self.directory, self.config, apply=True, transfer=partial)
        self.assertEqual(failed['state'], 'FAILED_NOT_APPLIED')
        self.assertEqual(self.target.read_bytes(), b'<html>old</html>')
        self.assertEqual(self.run_sync(apply=True)['state'], 'VERIFIED')
        self.assertEqual(len(self.calls), 2)

    def test_timeout_after_success_reconciles_without_retry(self):
        def lost(source, target):
            self.transfer(source, target)
            raise subprocess.TimeoutExpired('rsync', 60)
        result = sync.sync_publication(self.directory, self.config, apply=True, transfer=lost)
        self.assertEqual(result['state'], 'VERIFIED')
        self.assertEqual(result['failure']['reason'], 'SERVER_SYNC_TRANSFER_TIMEOUT')
        self.assertEqual(self.run_sync(apply=True)['state'], 'VERIFIED')
        self.assertEqual(len(self.calls), 1)

    def test_timeout_before_change_reads_back_then_explicit_retry_succeeds(self):
        def timeout(source, target):
            raise subprocess.TimeoutExpired('rsync', 60)
        result = sync.sync_publication(self.directory, self.config, apply=True, transfer=timeout)
        self.assertEqual(result['state'], 'FAILED_NOT_APPLIED')
        self.assertEqual(self.run_sync(apply=True)['state'], 'VERIFIED')

    def test_external_change_after_saved_preimage_is_conflict(self):
        sync.sync_publication(self.directory, self.config, apply=True, transfer=lambda *_: 23)
        self.target.write_bytes(b'<html>changed by human</html>')
        result = self.run_sync(apply=True)
        self.assertEqual(result['state'], 'CONFLICT')
        self.assertEqual(result['reason'], 'SERVER_SYNC_DESTINATION_CHANGED')
        self.assertEqual(self.calls, [])

    def test_partial_target_change_is_uncertain_then_conflict_not_replayed(self):
        def partial(source, target):
            target.write_bytes(b'partial destination')
            return 23
        self.assertEqual(sync.sync_publication(self.directory, self.config, apply=True, transfer=partial)['state'], 'UNCERTAIN')
        self.assertEqual(self.run_sync(apply=True)['state'], 'CONFLICT')
        self.assertEqual(self.calls, [])

    def test_crash_after_copy_resumes_from_hash_without_replay(self):
        def crash(source, target):
            self.transfer(source, target)
            raise Crash()
        with self.assertRaises(Crash):
            sync.sync_publication(self.directory, self.config, apply=True, transfer=crash)
        self.assertEqual(json.loads((self.directory / 'server-sync.json').read_text())['state'], 'ATTEMPTED')
        self.assertEqual(self.run_sync(apply=True)['state'], 'VERIFIED')
        self.assertEqual(len(self.calls), 1)

    def test_interrupted_unchanged_transfer_requires_explicit_stopped_confirmation(self):
        def crash(*_):
            raise Crash()
        with self.assertRaises(Crash):
            sync.sync_publication(self.directory, self.config, apply=True, transfer=crash)
        self.assertEqual(self.run_sync(apply=True)['reason'], 'SERVER_SYNC_PRIOR_TRANSFER_UNRESOLVED')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.run_sync(apply=True, confirm_transfer_stopped=True)['state'], 'VERIFIED')
        self.assertEqual(len(self.calls), 1)

    def test_older_unsent_publication_cannot_overwrite_newer_verified_delivery(self):
        newer = self.root / 'newer-publication'
        newer.mkdir()
        content = b'<html>newer verified snapshot</html>'
        (newer / 'viewer.html').write_bytes(content)
        (newer / 'publication.json').write_text(json.dumps({**self.publication_receipt,
            'snapshot_id': 'newer-snapshot', 'journal_sequence': 11,
            'created_at_utc': '2026-10-22T02:00:00Z', 'files': {'viewer.html': sha(content)}}))
        def transfer(source, target):
            target.write_bytes(source.read_bytes())
            return 0
        result = sync.sync_publication(newer, self.config, apply=True, transfer=transfer)
        self.assertEqual(result['state'], 'VERIFIED')
        self.assertFalse((self.directory / 'server-sync.json').exists())
        older = self.run_sync(apply=True)
        self.assertEqual(older['reason'], 'SERVER_SYNC_STALE_PUBLICATION')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.target.read_bytes(), content)

    def test_newer_success_before_receipt_blocks_unsent_older_publication(self):
        newer = self.root / 'newer-uncommitted'
        newer.mkdir()
        content = b'<html>newer remote success before receipt</html>'
        (newer / 'viewer.html').write_bytes(content)
        (newer / 'publication.json').write_text(json.dumps({**self.publication_receipt,
            'snapshot_id': 'newer-snapshot', 'journal_sequence': 11,
            'files': {'viewer.html': sha(content)}}))
        def crash(source, target):
            index = json.loads((self.root / '.server-sync-latest.json').read_text())
            self.assertEqual(next(iter(index['targets'].values()))['state'], 'ATTEMPTED')
            target.write_bytes(source.read_bytes())
            raise Crash()
        with self.assertRaises(Crash):
            sync.sync_publication(newer, self.config, apply=True, transfer=crash)
        result = self.run_sync(apply=True)
        self.assertEqual(result['reason'], 'SERVER_SYNC_STALE_PUBLICATION')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.target.read_bytes(), content)
        self.assertEqual(sync.sync_publication(newer, self.config, apply=True)['state'], 'VERIFIED')

    def test_crash_between_target_intent_and_own_receipt_cannot_blind_retry(self):
        real_save = sync._save
        def crash(path, value):
            if Path(path).name == 'server-sync.json' and value.get('state') == 'ATTEMPTED':
                raise Crash()
            return real_save(path, value)
        with patch.object(sync, '_save', side_effect=crash), self.assertRaises(Crash):
            self.run_sync(apply=True)
        self.assertEqual(json.loads((self.directory / 'server-sync.json').read_text())['state'], 'PREPARED')
        self.assertEqual(self.run_sync(apply=True)['state'], 'UNCERTAIN')
        self.assertEqual(self.calls, [])

    def test_older_same_content_reconciliation_does_not_downgrade_latest_index(self):
        self.assertEqual(self.run_sync(apply=True)['state'], 'VERIFIED')
        same = self.root / 'older-same-content'
        same.mkdir()
        (same / 'viewer.html').write_bytes(self.content)
        (same / 'publication.json').write_text(json.dumps({**self.publication_receipt,
            'snapshot_id': 'older-same-snapshot', 'journal_sequence': 9}))
        before = (self.root / '.server-sync-latest.json').read_bytes()
        self.assertEqual(sync.sync_publication(same, self.config, apply=True)['state'], 'VERIFIED')
        self.assertEqual((self.root / '.server-sync-latest.json').read_bytes(), before)

    def test_same_path_and_hash_reconciles_after_share_directory_remount(self):
        self.assertEqual(self.run_sync(apply=True)['state'], 'VERIFIED')
        original_stat = self.destination.stat()
        self.destination.rename(self.root / 'old-share-instance')
        self.destination.mkdir()
        self.target.write_bytes(self.content)
        self.assertNotEqual(original_stat.st_ino, self.destination.stat().st_ino)
        result = self.run_sync(apply=True)
        self.assertEqual(result['state'], 'VERIFIED')
        self.assertEqual(len(self.calls), 1)

    def test_standalone_cli_check_and_apply_do_not_call_google(self):
        path = self.root / 'config.json'
        path.write_text(json.dumps({'server_sync': self.config}))
        args = ['--publication', str(self.directory), '--config', str(path)]
        with patch.object(sync, '_transfer', side_effect=self.transfer) as transfer, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sync.main([*args, '--check']), 0)
            transfer.assert_not_called()
            self.assertEqual(sync.main([*args, '--apply']), 0)
        self.assertEqual(len(self.calls), 1)


class ServerSyncPublicationHookTests(unittest.TestCase):
    def setUp(self):
        self.f = publication_fixture.DestinationPublicationTests(methodName='runTest')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.target_dir = Path(self.f.tmp.name) / 'share'
        self.target_dir.mkdir()
        self.config = {'enabled': True, 'destination_directory': str(self.target_dir)}
        self.f.config['server_sync'] = self.config
        self.order = []
        original = self.f.drive.replace_content
        def drive_write(fid, content):
            self.order.append('drive')
            self.assertEqual((self.target_dir / 'index.html').read_bytes(), content)
            return original(fid, content)
        self.f.drive.replace_content = drive_write

    def transfer(self, source, target):
        self.order.append('server')
        self.assertEqual(self.f.drive.writes, [])
        target.write_bytes(source.read_bytes())
        return 0

    def test_automatic_publication_server_first_same_bytes_no_desktop_roundtrip(self):
        with patch.object(sync, '_transfer', side_effect=self.transfer):
            result = publication.publish_registered_sections(self.f.config, self.f.registry,
                journal=self.f.j, fresh_workbook=self.f.workbook, sheet_capture=self.f.capture,
                drive=self.f.drive, apply=True)
        self.assertEqual(result['result'], 'PUBLICATION_VERIFIED')
        self.assertEqual(result['server_sync']['state'], 'VERIFIED')
        self.assertEqual(self.order, ['server', 'drive'])
        self.assertEqual(sha(self.f.drive.content), result['server_sync']['source_sha256'])
        saved = publication._load(Path(result['directory']) / 'publication.json')
        self.assertEqual(saved['server_sync']['state'], 'VERIFIED')
        self.assertEqual((Path(result['directory']) / 'viewer.html').read_bytes(), self.f.drive.content)

    def test_failed_server_gates_drive_sheet_independent_then_shared_publish_only_retries(self):
        path = self.f.freeze()
        with patch.object(sync, '_transfer', return_value=23):
            failed = publication.resume_publication(path, sheet_capture=self.f.capture,
                drive=self.f.drive, apply=True, server_sync=self.config)
        self.assertEqual(failed['result'], 'PUBLICATION_INCOMPLETE')
        self.assertEqual(failed['sheet']['state'], 'VERIFIED')
        self.assertEqual(failed['html']['state'], 'WAITING_FOR_SERVER')
        self.assertEqual(self.f.drive.writes, [])
        with patch.object(sync, '_transfer', side_effect=self.transfer):
            recovered = publication.resume_publication(path, sheet_capture=self.f.capture,
                drive=self.f.drive, apply=True, server_sync=self.config)
        self.assertEqual(recovered['result'], 'PUBLICATION_VERIFIED')
        self.assertEqual(len(self.f.sheet.updates), 1)
        self.assertEqual(self.order, ['server', 'drive'])

    def test_missing_mount_is_durable_parent_receipt_and_gates_drive(self):
        path = self.f.freeze()
        config = {'enabled': True, 'destination_directory': str(self.target_dir / 'unmounted')}
        with patch.object(sync, '_transfer') as transfer:
            result = publication.resume_publication(path, sheet_capture=self.f.capture,
                drive=self.f.drive, apply=True, server_sync=config)
        transfer.assert_not_called()
        self.assertEqual(result['server_sync']['reason'], 'SERVER_SYNC_DESTINATION_UNAVAILABLE')
        self.assertEqual(publication._load(path / 'publication.json')['server_sync']['state'], 'BLOCKED')
        self.assertEqual(result['html']['state'], 'WAITING_FOR_SERVER')
        self.assertFalse(self.f.drive.writes)

    def test_existing_verified_drive_not_downgraded_or_reuploaded(self):
        self.f.drive.replace_content = publication_fixture.FakeDrive.replace_content.__get__(self.f.drive)
        path = self.f.freeze()
        original = publication.resume_publication(path, sheet_capture=self.f.capture, drive=self.f.drive, apply=True)
        self.assertEqual(original['html']['state'], 'VERIFIED')
        with patch.object(sync, '_transfer', return_value=23):
            failed = publication.resume_publication(path, sheet_capture=self.f.capture,
                drive=self.f.drive, apply=True, server_sync=self.config)
        self.assertEqual(failed['result'], 'PUBLICATION_INCOMPLETE')
        self.assertEqual(failed['html']['state'], 'VERIFIED')
        self.assertEqual(len(self.f.drive.writes), 1)

    def test_main_destination_publish_only_passes_private_config_to_shared_hook(self):
        path = self.f.freeze()
        root = Path(self.f.tmp.name)
        config = {**self.f.config, 'accepted_inherited_access': True, 'store': str(root / 'state'),
            'registry': str(root / 'registry.json'), 'run_directory': str(root / 'runs'),
            'anchor_paths': {}, 'expected_account': 'fixture@example.test'}
        account = SimpleNamespace(get_account=lambda: {'emailAddress': 'fixture@example.test'})
        with patch.object(publication, 'GooglePublicationDrive', return_value=self.f.drive), \
             patch.object(sync, '_transfer', side_effect=self.transfer):
            result = run_destination(config, 'Root/Section', apply=True, publish_only=str(path),
                                     capture_client=self.f.capture, factory=lambda *_: account)
        self.assertEqual(result['result'], 'PUBLICATION_VERIFIED')
        self.assertEqual(self.order, ['server', 'drive'])

    def test_console_reports_server_separately(self):
        out = io.StringIO()
        console = ConsoleReporter(stream=out)
        console.finish({'publication': {'result': 'PUBLICATION_INCOMPLETE',
            'sheet': {'state': 'VERIFIED'}, 'html': {'state': 'WAITING_FOR_SERVER'},
            'server_sync': {'state': 'BLOCKED', 'reason': 'SERVER_SYNC_DESTINATION_UNAVAILABLE'}}})
        self.assertIn('Server sync: BLOCKED; SERVER_SYNC_DESTINATION_UNAVAILABLE.', out.getvalue())
        self.assertIn('Drive HTML: WAITING_FOR_SERVER.', out.getvalue())


if __name__ == '__main__':
    unittest.main()
