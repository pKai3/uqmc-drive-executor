"""Initial imports use one exact live-format capture; all tests are offline."""
import copy
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import unittest
from unittest.mock import patch

import test_section_importer as section_fixtures
from test_importer import set_cell
from uqmc_recovery.agent_profile import register_agent_plan, agent_snapshot
from uqmc_recovery.batch_source import bind_initial_section_source, require_batch_source
from uqmc_recovery.importer import catalog
from uqmc_recovery.model import SafetyError, digest


class InitialBatchSourceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = section_fixtures.SectionImporterTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.journal = self.fixture.journal
        self.capture = self.fixture.export
        self.capture.update(exported_at_utc=datetime.now(timezone.utc).isoformat(),
            capture_method='sheets.spreadsheets.get_with_drive_revision_bracket',
            source_revision={'id': self.capture['workbook_id'],
                'mimeType': 'application/vnd.google-apps.spreadsheet',
                'version': '12', 'modifiedTime': '2026-10-21T00:00:00Z', 'trashed': False},
            cell_evidence={'fixture': 'formulas and effective values'})
        self.seal_capture(self.capture)
        self.fixture.projection['method_profile'] = 'approved_folder_scope'
        for value in self.fixture.states.values():
            value['parents_complete'] = True
        self.fixture.states = {key: agent_snapshot(value) for key, value in self.fixture.states.items()}
        result = self.fixture.run_section(metadata_profile='AGENT_ASSISTED',
            expected_method_profile='approved_folder_scope', owner_approval_ref=self.fixture.approval(
                expires_at_utc=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()))
        self.assertIsNotNone(result.plan, result.manifest)
        self.plan = result.plan
        self.approval_id = result.manifest['authorization_scope']['owner_approval_id']
        register_agent_plan(self.journal, self.plan)

    @staticmethod
    def seal_capture(capture):
        capture['input_sha256'] = catalog(capture)['input_sha256']
        capture['capture_sha256'] = digest({key: capture[key] for key in
            ('workbook_id', 'sheets', 'complete', 'cell_evidence', 'source_revision')})

    def test_one_capture_binds_exact_import_without_execution_authority(self):
        event = bind_initial_section_source(self.journal, self.plan, self.capture)
        self.assertEqual(event['kind'], 'INITIAL_BATCH_SOURCE_BOUND')
        require_batch_source(self.journal, self.plan)
        self.assertFalse(event['payload']['execution_approved'])
        self.assertFalse(any(r['kind'] in {'APPROVED', 'ATTEMPTED'} for r in self.journal.records()))

    def test_changed_cells_cannot_bypass_section_binding(self):
        capture = copy.deepcopy(self.capture)
        set_cell(capture, 'Move Manifest', 1, 'Exact target path', 'Unapproved/Elsewhere')
        self.seal_capture(capture)
        with self.assertRaisesRegex(SafetyError, 'WORKBOOK_DIFFERS_FROM_SECTION_IMPORT'):
            bind_initial_section_source(self.journal, self.plan, capture)

    def test_corrupt_capture_hash_is_rejected(self):
        capture = copy.deepcopy(self.capture)
        capture['cell_evidence']['fixture'] = 'changed'
        with self.assertRaisesRegex(SafetyError, 'CAPTURE_HASH_MISMATCH'):
            bind_initial_section_source(self.journal, self.plan, capture)

    def test_substituted_new_capture_requires_fresh_import(self):
        capture = copy.deepcopy(self.capture)
        capture['source_revision']['version'] = '13'
        self.seal_capture(capture)
        with self.assertRaisesRegex(SafetyError, 'CAPTURE_DIFFERS_FROM_SECTION_IMPORT'):
            bind_initial_section_source(self.journal, self.plan, capture)

    def test_unregistered_changed_plan_rejected(self):
        altered = replace(self.plan, proposal_revision='section:changed')
        with self.assertRaisesRegex(SafetyError, 'FROZEN_AGENT_PLAN_MISMATCH'):
            bind_initial_section_source(self.journal, altered, self.capture)

    def test_missing_live_revision_or_capture_method_rejected(self):
        for key in ('source_revision', 'capture_method'):
            capture = copy.deepcopy(self.capture)
            capture.pop(key)
            with self.subTest(key=key), self.assertRaisesRegex(SafetyError, 'LIVE_CAPTURE_REQUIRED'):
                bind_initial_section_source(self.journal, self.plan, capture)

    def test_stale_capture_blocks_new_dispatch(self):
        bind_initial_section_source(self.journal, self.plan, self.capture)
        with patch('uqmc_recovery.batch_source.datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime.now(timezone.utc) + timedelta(minutes=6)
            with self.assertRaisesRegex(SafetyError, 'EXPIRED_REFRESH_AGAIN'):
                require_batch_source(self.journal, self.plan)

    def test_long_preflight_revalidates_revision_without_restamping_capture(self):
        original = copy.deepcopy(self.capture)
        later = datetime.now(timezone.utc) + timedelta(minutes=6)
        with patch('uqmc_recovery.batch_source.datetime', wraps=datetime) as clock:
            clock.now.return_value = later
            event = bind_initial_section_source(self.journal, self.plan, self.capture,
                revision_reader=lambda: copy.deepcopy(original['source_revision']))
            require_batch_source(self.journal, self.plan)
        self.assertEqual(self.capture, original)
        self.assertEqual(self.journal.read_evidence(event['payload']['capture_ref']), original)
        proof = event['payload']['revision_revalidation']
        self.assertEqual(proof['capture_sha256'], original['capture_sha256'])
        self.assertEqual(proof['source_revision'], original['source_revision'])
        self.assertEqual(proof['started_at_utc'], later.isoformat().replace('+00:00', 'Z'))

    def test_old_capture_without_revalidation_still_blocks_binding(self):
        with patch('uqmc_recovery.batch_source.datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime.now(timezone.utc) + timedelta(minutes=6)
            with self.assertRaisesRegex(SafetyError, 'EXPIRED_REFRESH_AGAIN'):
                bind_initial_section_source(self.journal, self.plan, self.capture)
        self.assertFalse(any(e['kind'] == 'INITIAL_BATCH_SOURCE_BOUND' for e in self.journal.records()))

    def test_changed_revision_and_unavailable_read_cannot_bind(self):
        for field, value in [('version', '13'), ('modifiedTime', '2026-10-22T00:00:00Z'),
                             ('id', 'different-workbook'), ('trashed', True)]:
            revision = self.capture['source_revision'] | {field: value}
            with self.subTest(field=field), self.assertRaisesRegex(
                    SafetyError, 'WORKBOOK_CHANGED_BEFORE_BIND_REFRESH_REQUIRED'):
                bind_initial_section_source(self.journal, self.plan, self.capture,
                    revision_reader=lambda: revision)
        def unavailable():
            raise TimeoutError('fixture read timeout')
        with self.assertRaisesRegex(TimeoutError, 'fixture read timeout'):
            bind_initial_section_source(self.journal, self.plan, self.capture,
                revision_reader=unavailable)
        self.assertFalse(any(e['kind'] == 'INITIAL_BATCH_SOURCE_BOUND' for e in self.journal.records()))

    def test_revalidation_expiry_and_authority_revocation_remain_enforced(self):
        later = datetime.now(timezone.utc) + timedelta(minutes=6)
        with patch('uqmc_recovery.batch_source.datetime', wraps=datetime) as clock:
            clock.now.return_value = later
            bind_initial_section_source(self.journal, self.plan, self.capture,
                revision_reader=lambda: self.capture['source_revision'])
            clock.now.return_value = later + timedelta(seconds=301)
            with self.assertRaisesRegex(SafetyError, 'EXPIRED_REFRESH_AGAIN'):
                require_batch_source(self.journal, self.plan)
            clock.now.return_value = later
            self.journal.append('SECTION_APPROVAL_REVOKED', payload={'approval_id': self.approval_id})
            with self.assertRaisesRegex(SafetyError, 'REVOKED_OR_SUPERSEDED'):
                require_batch_source(self.journal, self.plan)

    def test_slow_revision_request_does_not_make_old_read_fresh(self):
        later = datetime.now(timezone.utc) + timedelta(minutes=6)
        with patch('uqmc_recovery.batch_source.datetime', wraps=datetime) as clock:
            clock.now.return_value = later
            def slow_read():
                clock.now.return_value = later + timedelta(seconds=301)
                return self.capture['source_revision']
            with self.assertRaisesRegex(SafetyError, 'EXPIRED_REFRESH_AGAIN'):
                bind_initial_section_source(self.journal, self.plan, self.capture,
                    revision_reader=slow_read)
        self.assertFalse(any(e['kind'] == 'INITIAL_BATCH_SOURCE_BOUND' for e in self.journal.records()))

    def test_revalidation_does_not_allow_changed_capture(self):
        capture = copy.deepcopy(self.capture)
        set_cell(capture, 'Move Manifest', 1, 'Exact target path', 'Unapproved/Elsewhere')
        self.seal_capture(capture)
        with self.assertRaisesRegex(SafetyError, 'WORKBOOK_DIFFERS_FROM_SECTION_IMPORT'):
            bind_initial_section_source(self.journal, self.plan, capture,
                revision_reader=lambda: capture['source_revision'])

    def test_late_revocation_blocks_dispatch(self):
        bind_initial_section_source(self.journal, self.plan, self.capture)
        self.journal.append('SECTION_APPROVAL_REVOKED', payload={'approval_id': self.approval_id})
        with self.assertRaisesRegex(SafetyError, 'REVOKED_OR_SUPERSEDED'):
            require_batch_source(self.journal, self.plan)

    def test_late_owner_approval_expiry_blocks_dispatch(self):
        bind_initial_section_source(self.journal, self.plan, self.capture)
        with patch('uqmc_recovery.section_importer.datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime.now(timezone.utc) + timedelta(hours=2)
            with self.assertRaisesRegex(SafetyError, 'SECTION_APPROVAL_EXPIRED'):
                require_batch_source(self.journal, self.plan)

    def test_edit_notice_blocks_resume_and_new_dispatch(self):
        bind_initial_section_source(self.journal, self.plan, self.capture)
        self.journal.append('DESIRED_ATTEMPTED', payload={'plan_sha256': self.plan.proposal_hash})
        self.journal.append('BATCH_REFRESH_REQUIRED', payload={'proposal_hash': self.plan.proposal_hash})
        with self.assertRaisesRegex(SafetyError, 'PLAN_EDITS_REQUIRE_NEW_REFRESHED_BATCH'):
            require_batch_source(self.journal, self.plan, resume=True)

    def test_post_binding_source_observation_requires_reconciliation(self):
        bind_initial_section_source(self.journal, self.plan, self.capture)
        self.journal.append('DESIRED_OBSERVED', payload={'source_id': 'source',
            'plan_sha256': self.plan.proposal_hash})
        with self.assertRaisesRegex(SafetyError, 'HISTORY_CHANGED_SINCE_BATCH_REFRESH'):
            require_batch_source(self.journal, self.plan)

    def test_initial_binding_cannot_be_used_after_dispatch(self):
        self.journal.append('DESIRED_ATTEMPTED', payload={'plan_sha256': self.plan.proposal_hash})
        with self.assertRaisesRegex(SafetyError, 'INITIAL_BATCH_ALREADY_STARTED'):
            bind_initial_section_source(self.journal, self.plan, self.capture)

    def test_existing_started_plan_resume_keeps_original_binding(self):
        bind_initial_section_source(self.journal, self.plan, self.capture)
        self.journal.append('DESIRED_ATTEMPTED', payload={'plan_sha256': self.plan.proposal_hash})
        with patch('uqmc_recovery.batch_source.datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime.now(timezone.utc) + timedelta(minutes=6)
            require_batch_source(self.journal, self.plan, resume=True)

    def test_duplicate_binding_rejected(self):
        bind_initial_section_source(self.journal, self.plan, self.capture)
        with self.assertRaisesRegex(SafetyError, 'BATCH_SOURCE_ALREADY_BOUND'):
            bind_initial_section_source(self.journal, self.plan, self.capture)


class LongPreflightCommandTests(unittest.TestCase):
    def test_destination_command_rechecks_revision_after_long_freeze(self):
        from test_destination_command import DestinationCommandTests
        from uqmc_recovery.journal import Journal
        fixture = DestinationCommandTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        later = datetime.now(timezone.utc) + timedelta(minutes=6)
        with patch('uqmc_recovery.batch_source.datetime', wraps=datetime) as clock:
            clock.now.return_value = later
            result = fixture.run_command(apply=True)
        self.assertEqual(result['counts'], {'APPLIED': 2})
        self.assertEqual(fixture.capture.calls, 1)
        self.assertEqual(len(fixture.server.requests), 2)
        event = next(e for e in Journal(fixture.config['store']).records()
                     if e['kind'] == 'INITIAL_BATCH_SOURCE_BOUND')
        proof = event['payload']['revision_revalidation']
        self.assertEqual(proof['source_revision'], fixture.capture._revision())
        self.assertEqual(proof['started_at_utc'], later.isoformat().replace('+00:00', 'Z'))

    def test_revision_change_stops_before_preparation_or_binding(self):
        from test_destination_command import DestinationCommandTests
        for changed_on in (2, 3):
            with self.subTest(changed_on=changed_on):
                fixture = DestinationCommandTests()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                original_read = fixture.capture._revision
                reads = []
                def revision():
                    reads.append(True)
                    value = original_read()
                    return value | {'version': '2'} if len(reads) >= changed_on else value
                fixture.capture._revision = revision
                with self.assertRaisesRegex(SafetyError, 'WORKBOOK_CHANGED_BEFORE_BIND_REFRESH_REQUIRED'):
                    fixture.run_command(apply=True)
                self.assertEqual(fixture.server.requests, [])
                self.assertEqual(fixture.server.created, [])
                self.assertEqual(fixture.capture.calls, 1)


if __name__ == '__main__':
    unittest.main()
