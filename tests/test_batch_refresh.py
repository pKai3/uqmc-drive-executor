"""Synthetic workbook and temporary journal tests; no credentials/network."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_importer import export_fixture, metadata, set_cell, append_row
from uqmc_recovery.batch_refresh import refresh_batch
from uqmc_recovery.journal import Journal
from uqmc_recovery.model import Operation, Plan, SafetyError, digest


class BatchRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = Journal(Path(self.temp.name) / 'journal')
        self.old = export_fixture()
        set_cell(self.old, 'Proposed Renames', 1, 'Current name + type suffix', 'Original.pdf')
        set_cell(self.old, 'Proposed Renames', 1, 'Proposed name + type suffix', 'Approved.pdf')
        self.old['source_revision'] = {'version': '1'}
        self.old['capture_sha256'] = 'fixture-original-capture'
        before = metadata('source', 'Original.pdf', 'application/pdf')
        rename = Operation('rename', 'source', 'RENAME', before, {'name': 'Approved.pdf'})
        move = Operation('move', 'source', 'MOVE', rename.intended(), {'parents': ['new']}, ('rename',))
        self.plan = Plan('old-run', 'old-run', 'fixture-section', (rename, move))
        origin = self.journal.evidence({'manifest': {'workbook_id': self.old['workbook_id']},
            'workbook_import_ref': self.journal.evidence({'workbook_export': self.old}),
            'projection': {'allowed_destination_parent_ids': ['new', 'new2']}})
        self.binding = {'section_id': 'fixture-section', 'section_manifest_ref': origin}
        self.patcher = patch('uqmc_recovery.batch_refresh.validate_section_authority', return_value=self.binding)
        self.validate = self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.policy = {'schema_version': 'uqmc.batch-refresh.policy.v1',
            'existing_plan_sha256': self.plan.proposal_hash, 'section_id': 'fixture-section',
            'workbook_id': self.old['workbook_id'], 'approved_source_ids': ['source'],
            'allow_pending_name_changes': True, 'allow_pending_destination_changes': True,
            'destination_paths': {}}
        for path, parents in [('Club/Archive/Minutes', ['new']), ('Club/Archive', ['new']), ('Club/Other', ['new2'])]:
            self.policy['destination_paths'][path] = {'parent_ids': parents,
                'provenance_ref': self.journal.evidence({'kind': 'VERIFIED_DESTINATION_PATH_BINDING',
                    'section_id': 'fixture-section', 'path': path, 'parent_ids': parents})}
        self.reauthorize()
        self.fresh = copy.deepcopy(self.old)
        self.fresh['source_revision'] = {'version': '2'}
        self.fresh['capture_sha256'] = 'fixture-fresh-capture'
        self.calls = 0

    def reauthorize(self):
        self.policy['authority_evidence_ref'] = self.journal.evidence({
            **{k: self.policy[k] for k in ('existing_plan_sha256', 'section_id', 'workbook_id', 'approved_source_ids',
               'allow_pending_name_changes', 'allow_pending_destination_changes')},
            'kind': 'EXPLICIT_PENDING_SECTION_EDIT_AUTHORIZATION', 'instruction': 'Fixture owner permits pending edits.',
            'provenance_ref': self.journal.evidence({'kind': 'fixture-human-instruction'})})

    def capture(self):
        self.calls += 1
        return copy.deepcopy(self.fresh)

    def refresh(self, batch='new-batch'):
        return refresh_batch(self.journal, self.plan, self.capture, self.policy, batch_id=batch)

    def observe(self, *, name='Approved.pdf', parent='old', kind='DESIRED_APPLIED'):
        raw = {'id': 'source', 'name': name, 'parents': [parent], 'mimeType': 'application/pdf'}
        ref = self.journal.evidence(raw)
        self.journal.append(kind, payload={'source_id': 'source', 'plan_sha256': self.plan.proposal_hash,
            'observed_ref': ref, 'state': 'TARGET'})
        return ref

    def test_refresh_once_freezes_changed_pending_target_without_old_plan_edit(self):
        original_hash = self.plan.proposal_hash
        set_cell(self.fresh, 'Proposed Renames', 1, 'Proposed name + type suffix', 'Edited.pdf')
        set_cell(self.fresh, 'Move Manifest', 1, 'Exact target path', 'Club/Other')
        self.fresh['cell_evidence'] = {'formula': '=fixture()', 'effective': 'Edited.pdf'}
        result = self.refresh()
        snapshot = result['snapshot']
        self.assertEqual(self.calls, 1)
        self.assertEqual(snapshot['pending_candidates'][0]['target']['name'], 'Edited.pdf')
        self.assertEqual(snapshot['pending_candidates'][0]['target']['parents'], ['new2'])
        self.assertEqual(snapshot['pending_candidates'][0]['actions'], ['RENAME', 'MOVE'])
        self.assertEqual(self.journal.read_evidence(snapshot['capture_ref'])['cell_evidence'], self.fresh['cell_evidence'])
        self.assertEqual(self.plan.proposal_hash, original_hash)
        self.assertFalse(snapshot['execution_approved'])
        self.assertEqual(self.journal.read_evidence(result['evidence_ref']), snapshot)
        self.assertEqual(self.journal.records()[-1]['kind'], 'BATCH_REFRESHED')
        self.assertFalse(any(e['kind'] in {'APPROVED', 'ATTEMPTED'} for e in self.journal.records()))

    def test_completed_name_and_parent_are_preserved_with_edits_separate(self):
        ref = self.observe(parent='new')
        set_cell(self.fresh, 'Proposed Renames', 1, 'Proposed name + type suffix', 'Later proposal.pdf')
        snapshot = self.refresh()['snapshot']
        self.assertEqual(snapshot['pending_candidates'], [])
        self.assertEqual(snapshot['completed_changes_separate_work'][0]['history_evidence_ref'], ref)
        self.assertEqual(snapshot['completed_changes_separate_work'][0]['observed']['name'], 'Approved.pdf')

    def test_prior_rename_not_replayed_but_explicit_pending_edit_propagates(self):
        self.observe()
        unchanged = self.refresh('one')['snapshot']['pending_candidates'][0]
        self.assertEqual(unchanged['actions'], ['MOVE'])
        set_cell(self.fresh, 'Proposed Renames', 1, 'Proposed name + type suffix', 'Changed pending.pdf')
        changed = self.refresh('two')['snapshot']['pending_candidates'][0]
        self.assertEqual(changed['before']['name'], 'Approved.pdf')
        self.assertEqual(changed['target']['name'], 'Changed pending.pdf')
        self.assertEqual(changed['actions'], ['RENAME', 'MOVE'])

    def test_no_withdrawal_tombstone_prevents_later_yes_resurrection(self):
        set_cell(self.fresh, 'Proposed Renames', 1, 'Decision (Yes/No)', 'No')
        first = self.refresh('one')['snapshot']
        self.assertEqual(first['pending_candidates'], [])
        self.assertEqual(first['blocked'][0]['reason'], 'REFRESH_APPROVAL_WITHDRAWN')
        self.assertTrue(any(e['kind'] == 'PROPOSAL_EXCLUDED' for e in self.journal.records()))
        set_cell(self.fresh, 'Proposed Renames', 1, 'Decision (Yes/No)', 'Yes')
        self.assertEqual(self.refresh('two')['snapshot']['pending_candidates'], [])

    def test_withdrawn_source_stays_excluded_and_yes_to_blank_blocks(self):
        set_cell(self.fresh, 'Proposed Renames', 1, 'Review state', 'Withdrawn')
        self.assertEqual(self.refresh('one')['snapshot']['excluded'][0]['source_id'], 'source')
        set_cell(self.fresh, 'Proposed Renames', 1, 'Review state', '')
        self.assertEqual(self.refresh('two')['snapshot']['pending_candidates'], [])
        # A separate clean journal fixture distinguishes loss of Yes from tombstone.
        self.journal = Journal(Path(self.temp.name) / 'journal-two')
        # Reuse original sealed evidence in the independent temporary store.
        for path in (Path(self.temp.name) / 'journal' / 'evidence').glob('*.json'):
            import json
            self.journal.evidence(json.loads(path.read_text()))
        set_cell(self.fresh, 'Proposed Renames', 1, 'Decision (Yes/No)', '')
        self.assertEqual(self.refresh('three')['snapshot']['blocked'][0]['reason'], 'REFRESH_APPROVAL_REMOVED_OR_INVALID')

    def test_missing_duplicate_and_external_mapping_fail_closed(self):
        self.fresh['sheets']['Proposed Renames'] = self.fresh['sheets']['Proposed Renames'][:1]
        self.assertEqual(self.refresh('missing')['snapshot']['pending_candidates'], [])
        self.fresh = copy.deepcopy(self.old)
        set_cell(self.fresh, 'Move Manifest', 1, 'Exact target path', 'Other section')
        self.assertEqual(self.refresh('outside')['snapshot']['blocked'][0]['reason'], 'REFRESH_DESTINATION_UNRESOLVED_OR_OUTSIDE_SECTION')
        self.fresh = copy.deepcopy(self.old)
        append_row(self.fresh, 'Proposed Renames', {'Rename ID': 'RENAME-two', 'Source ID': 'source',
            'Decision (Yes/No)': 'Yes', 'Proposed name + type suffix': 'Second.pdf'})
        self.assertEqual(self.refresh('ambiguous')['snapshot']['pending_candidates'], [])

    def test_rename_destination_cannot_escape_or_disagree_with_move(self):
        for value, reason in [('Unapproved section', 'REFRESH_RENAME_DESTINATION_UNRESOLVED_OR_OUTSIDE_SECTION'),
                              ('Club/Other', 'REFRESH_DESTINATION_COLUMNS_DISAGREE')]:
            set_cell(self.fresh, 'Proposed Renames', 1, 'Proposed destination folder', value)
            self.assertEqual(self.refresh(value)['snapshot']['blocked'][0]['reason'], reason)

    def test_scope_expansion_reported_without_including_new_source(self):
        append_row(self.fresh, 'Move Manifest', {'Proposal ID': 'MOVE-001', 'Manifest item': 'ITEM-extra',
            'Source ID': 'extra', 'Exact target path': 'Club/Archive/Minutes'})
        result = self.refresh()['snapshot']
        self.assertEqual({i['source_id'] for i in result['pending_candidates']}, {'source'})
        self.assertEqual(result['blocked'], [{'source_id': 'extra', 'reason': 'REFRESH_NEW_SOURCE_OUTSIDE_APPROVED_SECTION'}])

    def test_capture_failure_never_uses_old_export(self):
        before = self.journal.path.read_bytes()
        def failed():
            raise TimeoutError('fixture transport')
        with self.assertRaises(TimeoutError):
            refresh_batch(self.journal, self.plan, failed, self.policy, batch_id='failed')
        self.assertEqual(self.journal.path.read_bytes(), before)

    def test_revocation_during_capture_blocks_freeze(self):
        self.validate.side_effect = [self.binding, SafetyError('SECTION_APPROVAL_REVOKED_OR_SUPERSEDED')]
        before = self.journal.path.read_bytes()
        with self.assertRaisesRegex(SafetyError, 'REVOKED'):
            self.refresh()
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.journal.path.read_bytes(), before)

    def test_unresolved_request_blocks_and_draft_never_proves_completion(self):
        self.journal.append('DESIRED_ATTEMPTED', payload={'source_id': 'source', 'plan_sha256': self.plan.proposal_hash})
        self.assertEqual(self.refresh()['snapshot']['blocked'][0]['reason'], 'REFRESH_UNRESOLVED_REQUEST_REQUIRES_RECONCILIATION')

    def test_historical_observation_outside_approved_states_is_not_overwritten(self):
        self.observe(name='Manual unrelated name.pdf')
        snapshot = self.refresh()['snapshot']
        self.assertEqual(snapshot['pending_candidates'], [])
        self.assertEqual(snapshot['blocked'][0]['reason'], 'REFRESH_HISTORY_CONTAINS_UNAPPROVED_DRIFT')

    def test_unapproved_pending_edit_and_bad_binary_suffix_block(self):
        self.policy['allow_pending_name_changes'] = False
        self.reauthorize()
        set_cell(self.fresh, 'Proposed Renames', 1, 'Proposed name + type suffix', 'Edited.pdf')
        self.assertEqual(self.refresh('one')['snapshot']['blocked'][0]['reason'], 'REFRESH_PENDING_NAME_EDIT_NOT_AUTHORIZED')
        self.policy['allow_pending_name_changes'] = True
        self.reauthorize()
        set_cell(self.fresh, 'Proposed Renames', 1, 'Proposed name + type suffix', 'Edited.png')
        self.assertEqual(self.refresh('two')['snapshot']['blocked'][0]['reason'], 'UPLOADED_EXTENSION_CHANGED')


if __name__ == '__main__':
    unittest.main()
