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


if __name__ == '__main__':
    unittest.main()
