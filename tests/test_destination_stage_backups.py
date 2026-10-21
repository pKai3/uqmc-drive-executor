"""Offline stage timing and final checkpoint reuse regressions."""
import io
from pathlib import Path
import unittest
from unittest.mock import patch

import test_destination_command as command_fixture
from uqmc_recovery.destination_console import ConsoleReporter
from uqmc_recovery.journal import Journal
from uqmc_recovery.model import SafetyError
from uqmc_recovery import desired


class DestinationStageBackupTests(unittest.TestCase):
    def fixture(self):
        f = command_fixture.DestinationCommandTests()
        f.setUp()
        self.addCleanup(f.temp.cleanup)
        return f

    def test_runner_final_snapshot_is_reused_not_copied_again(self):
        f = self.fixture()
        real_backup = Journal.backup
        copied = []
        def backup(journal, path):
            copied.append(Path(path))
            return real_backup(journal, path)
        with patch.object(Journal, 'backup', autospec=True, side_effect=backup):
            result = f.run_command(apply=True)
        self.assertEqual(len(copied), 3)
        self.assertEqual(Path(result['backup']), copied[-1])
        self.assertTrue(copied[-1].name.startswith('desired-after-'))
        self.assertEqual((copied[-1] / 'events.jsonl').read_bytes(), Journal(f.config['store']).path.read_bytes())
        stages = [e for e in f.progress if e.get('stage', e.get('event')) == 'stage_finished']
        self.assertEqual(len(stages), 6)
        self.assertTrue(all(e['outcome'] == 'COMPLETE' and e['duration_seconds'] >= 0 for e in stages))

    def test_changed_journal_cannot_reuse_stale_final_snapshot(self):
        f = self.fixture()
        real_export = desired.export_status
        def altered_export(journal, result, directory):
            output = real_export(journal, result, directory)
            journal.append('FIXTURE_LATE_EVENT')
            return output
        with patch.object(desired, 'export_status', side_effect=altered_export):
            with self.assertRaisesRegex(SafetyError, 'BACKUP_VERIFICATION_FAILED'):
                f.run_command(apply=True)
        self.assertFalse(f.publications)

    def test_console_exposes_stage_duration_and_outcome(self):
        stream = io.StringIO()
        reporter = ConsoleReporter(stream=stream, clock=lambda: 0)
        reporter.progress({'stage': 'stage_started', 'stage_name': 'Final recovery backup'})
        reporter.progress({'stage': 'stage_finished', 'stage_name': 'Final recovery backup',
                           'outcome': 'COMPLETE', 'duration_seconds': 1.25})
        self.assertIn('Final recovery backup…', stream.getvalue())
        self.assertIn('complete in 1.250s', stream.getvalue())

    def test_publication_error_retains_safe_reason_after_success(self):
        f = self.fixture()
        def fail(_):
            raise ValueError('SECTION_PATH_NOT_UNIQUE:private detail')
        result = f.run_command(apply=True, publisher=fail)
        self.assertEqual(result['counts'], {'APPLIED': 2})
        self.assertEqual(result['publication'], {'state': 'PENDING',
            'error': 'SECTION_PATH_NOT_UNIQUE', 'error_class': 'ValueError'})


if __name__ == '__main__':
    unittest.main()
