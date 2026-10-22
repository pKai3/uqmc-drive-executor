"""Explicit proposal retirement; synthetic IDs and offline evidence only."""
import copy
from pathlib import Path
import tempfile
import unittest

import test_batch_refresh as refresh_fixture
import test_desired as desired_fixture
import test_destination_subtree_coverage as subtree_fixture
import test_run_authority as authority_fixture
from test_importer import append_row, export_fixture, set_cell, GuardedMetadata
from uqmc_recovery.destination import validate_resume_scope, read_and_plan
from uqmc_recovery.importer import RETIRED_MARKER, catalog, import_workbook, persist_retirements, retired_source_ids
from uqmc_recovery.journal import Journal
from uqmc_recovery.model import SafetyError


class RetiredImporterTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.journal = Journal(Path(temp.name) / 'journal')
        self.export = export_fixture()

    def mark(self, text=RETIRED_MARKER):
        set_cell(self.export, 'Proposed Renames', 1, 'Evidence / rationale', text)
        set_cell(self.export, 'Proposed Renames', 1, 'Decision (Yes/No)', 'No')

    def import_move(self):
        return import_workbook(self.journal, self.export, batch_id='fixture-retired',
            selected_refs=['MOVE-001/ITEM-001'], metadata_by_id=GuardedMetadata())

    def test_exact_first_line_is_typed_retirement_and_preserves_rows(self):
        self.mark(RETIRED_MARKER + '\r\nOriginal rationale retained.')
        original = copy.deepcopy(self.export)
        source = catalog(self.export)
        self.assertEqual(source['retired_ids'], ['source'])
        self.assertIn('source', source['excluded_ids'])
        self.assertNotIn('source', source['private_excluded_ids'])
        record = source['retired_records']['source']
        self.assertEqual(record['proposal_ref'], 'RENAME-001')
        self.assertEqual(record['original_mapping']['rename_row']['Evidence / rationale'],
                         RETIRED_MARKER + '\r\nOriginal rationale retained.')
        self.assertEqual(record['original_mapping']['move_rows'][0]['Exact target path'], 'Club/Archive/Minutes')
        self.assertEqual(self.export, original)

    def test_substring_later_line_case_and_whitespace_do_not_retire(self):
        for text in ('Missing or inaccessible', 'Note\n' + RETIRED_MARKER,
                     ' ' + RETIRED_MARKER, RETIRED_MARKER + ' ', RETIRED_MARKER.lower()):
            with self.subTest(text=text):
                self.mark(text)
                self.assertEqual(catalog(self.export)['retired_ids'], [])
        self.mark('')
        self.assertEqual(catalog(self.export)['retired_ids'], [])  # ordinary No

    def test_retirement_requires_stable_identity(self):
        self.mark()
        set_cell(self.export, 'Proposed Renames', 1, 'Source ID', '')
        with self.assertRaisesRegex(SafetyError, 'RETIRED_SOURCE_ID_REQUIRED'):
            catalog(self.export)

    def test_typed_event_durable_without_source_reads_and_no_generic_exclusion(self):
        self.mark()
        original = copy.deepcopy(self.export)
        result = self.import_move()
        self.assertIsNone(result.plan)
        self.assertEqual(result.manifest['rows'][0]['reason_code'], 'EXCLUDED')
        events = [e for e in self.journal.records() if e['kind'] == 'RETIRED']
        self.assertEqual(len(events), 1)
        payload = events[0]['payload']
        self.assertEqual((payload['file_id'], payload['reason_code'], payload['effect']),
                         ('source', 'USER_CONFIRMED_DELETED', 'NOT_APPLIED'))
        self.assertEqual(self.journal.read_evidence(payload['evidence_ref'])['original_mapping'], payload['original_mapping'])
        self.assertNotIn('observed_ref', payload)
        self.assertFalse(any(e['kind'] == 'EXCLUDED' and e['payload']['file_id'] == 'source'
                             for e in self.journal.records()))
        self.assertEqual(self.export, original)
        self.mark('Marker removed later')
        set_cell(self.export, 'Proposed Renames', 1, 'Decision (Yes/No)', 'Yes')
        result = self.import_move()
        self.assertIsNone(result.plan)
        self.assertEqual(result.manifest['retired_ids'], ['source'])
        self.assertEqual(len([e for e in self.journal.records() if e['kind'] == 'RETIRED']), 1)

    def test_private_withdrawal_is_not_reopened_by_retirement(self):
        self.mark()
        set_cell(self.export, 'Proposed Renames', 1, 'Review state', 'Withdrawn')
        self.import_move()
        self.assertTrue(any(e['kind'] == 'EXCLUDED' and e['payload']['file_id'] == 'source'
                            for e in self.journal.records()))
        self.assertEqual(retired_source_ids(self.journal.records()), {'source'})

    def test_prior_missing_exclusion_is_preserved_not_replaced(self):
        old = self.journal.append('EXCLUDED', payload={'file_id': 'source', 'recoverable': True,
            'effect': 'NOT_APPLIED', 'reason_code': 'MISSING_OR_INACCESSIBLE'})
        self.mark()
        persist_retirements(self.journal, catalog(self.export))
        self.assertEqual(self.journal.records()[0], old)
        self.assertEqual([e['kind'] for e in self.journal.records()], ['EXCLUDED', 'RETIRED'])

    def test_only_exact_typed_retired_history_is_honored(self):
        valid = {'kind': 'RETIRED', 'payload': {'file_id': 'source', 'effect': 'NOT_APPLIED',
                                              'reason_code': 'USER_CONFIRMED_DELETED'}}
        invalid = [dict(valid, kind='EXCLUDED'),
            {'kind': 'RETIRED', 'payload': {**valid['payload'], 'effect': 'APPLIED'}},
            {'kind': 'RETIRED', 'payload': {**valid['payload'], 'reason_code': 'MISSING_OR_INACCESSIBLE'}},
            {'kind': 'RETIRED', 'payload': {**valid['payload'], 'file_id': ''}}]
        self.assertEqual(retired_source_ids(invalid), set())
        self.assertEqual(retired_source_ids([*invalid, valid]), {'source'})

    def test_whole_subtree_coverage_cannot_reschedule_retired_member(self):
        fixture = subtree_fixture.WholeSubtreeCoverageTests()
        fixture.setUp()
        sid = fixture.kept_ids[0]
        name = fixture.states[sid]['name']
        append_row(fixture.export, 'Proposed Renames', {'Rename ID': 'RENAME-RETIRED', 'Source ID': sid,
            'Current name + type suffix': name, 'Proposed name + type suffix': name,
            'Proposed destination folder': subtree_fixture.DESTINATION,
            'Decision (Yes/No)': 'No', 'Evidence / rationale': RETIRED_MARKER})
        source = catalog(fixture.export, include_subtree_coverage=True)
        self.assertEqual(source['retired_ids'], [sid])
        self.assertFalse(any(r['source_id'] == sid and r.get('whole_subtree_origin_reference')
                             for r in source['records'].values()))
        prepared = fixture.prepare()
        self.assertEqual(set(prepared['source_ids']), set(fixture.kept_ids + fixture.explicit_ids) - {sid})
        self.assertNotIn('source-media', prepared['source_ids'])

    def test_read_only_preview_honors_marker_without_journal_append(self):
        self.mark()
        set_cell(self.export, 'Proposed Renames', 1, 'Proposed destination folder', 'Club/Archive')
        self.journal.append('FIXTURE_CHECKPOINT', payload={'reason': 'unchanged preview baseline'})
        before = copy.deepcopy(self.journal.records())
        def no_transport(*args, **kwargs):
            self.fail('Retired-only preview must not open source metadata')
        with self.assertRaisesRegex(SafetyError, 'SECTION_NO_ELIGIBLE_RECORDS|NO_ELIGIBLE_ITEMS_FOR_DESTINATION'):
            read_and_plan({'workbook_id': self.export['workbook_id']}, 'Club/Archive',
                self.journal, self.export, no_transport, {}, lambda event: None, preview=True)
        self.assertEqual(self.journal.records(), before)
        self.assertEqual(retired_source_ids(self.journal.records()), set())

    def test_saved_destination_resume_rejects_retired_history_even_with_yes_now(self):
        self.mark()
        persist_retirements(self.journal, catalog(self.export))
        self.mark('')
        set_cell(self.export, 'Proposed Renames', 1, 'Decision (Yes/No)', 'Yes')
        with self.assertRaisesRegex(SafetyError, 'RESUME_SOURCE_AUTHORITY_REMOVED:source'):
            validate_resume_scope(catalog(self.export), 'Club', {'selected_source_ids': ['source']}, self.journal)

    def test_refresh_persists_retirement_and_later_removed_marker_cannot_reopen(self):
        fixture = refresh_fixture.BatchRefreshTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        set_cell(fixture.fresh, 'Proposed Renames', 1, 'Evidence / rationale', RETIRED_MARKER)
        first = fixture.refresh('retired-one')['snapshot']
        self.assertEqual(first['pending_candidates'], [])
        self.assertEqual(retired_source_ids(fixture.journal.records()), {'source'})
        self.assertFalse(any(e['kind'] == 'EXCLUDED' and e['payload']['file_id'] == 'source'
                             for e in fixture.journal.records()))
        set_cell(fixture.fresh, 'Proposed Renames', 1, 'Evidence / rationale', '')
        second = fixture.refresh('retired-two')['snapshot']
        self.assertEqual(second['pending_candidates'], [])

    def test_old_frozen_desired_plan_cannot_replay_retired_source(self):
        fixture = desired_fixture.DesiredStateTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.freeze()
        fixture.journal.append('RETIRED', payload={'file_id': 'fixture-source',
            'reason_code': 'USER_CONFIRMED_DELETED', 'effect': 'NOT_APPLIED'})
        with self.assertRaisesRegex(SafetyError, 'EXCLUDED'):
            fixture.runner()
        self.assertEqual(fixture.transport.calls, [])

    def test_retirement_after_cached_authority_blocks_before_dispatch(self):
        fixture = authority_fixture.RunAuthorityTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with fixture.journal.locked():
            authority = fixture.cache()
            fixture.journal.append('RETIRED', payload={'file_id': 'source',
                'reason_code': 'USER_CONFIRMED_DELETED', 'effect': 'NOT_APPLIED'})
            with self.assertRaisesRegex(SafetyError, 'EXCLUDED'):
                authority.check()
