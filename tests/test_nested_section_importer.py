import copy
import tempfile
import unittest
from pathlib import Path

from test_importer import append_row, metadata
from uqmc_recovery.importer import HEADERS, WORKBOOK_ID, catalog
from uqmc_recovery.journal import Journal
from uqmc_recovery.model import FOLDER, SafetyError, digest
from uqmc_recovery.section_importer import import_section, validate_section_authority


class NestedSectionImporterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = Journal(Path(self.tmp.name) / 'state')
        self.export = {'workbook_id': WORKBOOK_ID, 'exported_at_utc': '2026-01-01T00:00:00Z',
                       'complete': {tab: True for tab in HEADERS},
                       'sheets': {tab: [list(headers)] for tab, headers in HEADERS.items()}}
        append_row(self.export, 'Proposed Moves', {'Proposal ID': 'MOVE-001', 'Action': 'Move', 'Proposed target path': 'Club/Skills'})
        append_row(self.export, 'Move Manifest', {'Proposal ID': 'MOVE-001', 'Manifest item': 'ITEM-001',
            'Source ID': 'year', 'Exact target path': 'Club/Skills/2024'})
        renames = [('RENAME-001', 'year', 'Old year', '2024', 'Club/Skills'),
                   ('RENAME-002', 'nested', 'Old nested', 'Nested', 'Club/Skills/2024'),
                   ('RENAME-003', 'file', 'Old.pdf', 'New.pdf', 'Club/Skills/2024/Nested')]
        for ref, sid, before, after, parent in renames:
            append_row(self.export, 'Proposed Renames', {'Rename ID': ref, 'Source ID': sid,
                'Current name + type suffix': before, 'Proposed name + type suffix': after,
                'Proposed destination folder': parent})
        self.states = {'club': metadata('club', 'Club ', FOLDER, []),
            'old': metadata('old', 'Old', FOLDER, []),
            'skills': metadata('skills', 'Skills', FOLDER, ['club']),
            'year': metadata('year', 'Old year', FOLDER, ['old']),
            'nested': metadata('nested', 'Old nested', FOLDER, ['year']),
            'file': metadata('file', 'Old.pdf', 'application/pdf', ['nested'])}
        def child(sid):
            return {key: self.states[sid][key] for key in ('id', 'name', 'mime_type', 'parents')}
        self.states['year']['subtree']['members'] = [child('nested'), child('file')]
        self.states['nested']['subtree']['members'] = [child('file')]
        self.operations = [
            {'reference': 'RENAME-001', 'source_id': 'year', 'action': 'RENAME', 'exact_target_name': '2024', 'depends_on_refs': []},
            {'reference': 'RENAME-002', 'source_id': 'nested', 'action': 'RENAME', 'exact_target_name': 'Nested', 'depends_on_refs': ['RENAME-001']},
            {'reference': 'RENAME-003', 'source_id': 'file', 'action': 'RENAME', 'exact_target_name': 'New.pdf', 'depends_on_refs': ['RENAME-002']},
            {'reference': 'MOVE-001/ITEM-001', 'source_id': 'year', 'action': 'MOVE', 'target_parent_ids': ['skills'],
             'depends_on_refs': ['RENAME-001', 'RENAME-002', 'RENAME-003']}]
        self.projection = {'schema_version': 'uqmc.destination.section.v1', 'section_id': 'skills',
            'section_path': 'Club/Skills', 'workbook_id': WORKBOOK_ID, 'projection_revision': 'one',
            'method_profile': 'approved_nested_section', 'allowed_actions': ['MOVE', 'RENAME'],
            'complete': True, 'declared_member_count': 3, 'allowed_destination_parent_ids': ['skills'],
            'operations': self.operations, 'members': [
                {'source_id': 'year', 'direct_operation_refs': ['RENAME-001', 'MOVE-001/ITEM-001'], 'ancestor_move_operation_refs': []},
                {'source_id': 'nested', 'direct_operation_refs': ['RENAME-002'], 'ancestor_move_operation_refs': ['MOVE-001/ITEM-001']},
                {'source_id': 'file', 'direct_operation_refs': ['RENAME-003'], 'ancestor_move_operation_refs': ['MOVE-001/ITEM-001']}],
            'destination_paths': {'Club': 'club', 'Club/Skills': 'skills', 'Club/Skills/2024': 'year', 'Club/Skills/2024/Nested': 'nested'}}

    def tearDown(self):
        self.tmp.cleanup()

    def freeze(self):
        source = catalog(self.export)
        self.projection['input_sha256'] = source['input_sha256']
        provenance = self.journal.evidence({'kind': 'FIXTURE_METADATA_CAPTURE'})
        self.projection['destination_path_evidence_ref'] = self.journal.evidence({
            'schema_version': 'uqmc.section.destination-paths.v1', 'section_id': 'skills',
            'input_sha256': source['input_sha256'], 'destination_paths': self.projection['destination_paths'], 'provenance_ref': provenance})
        approval = {'schema_version': 'uqmc.section.owner-approval.v1', 'decision': 'APPROVED', 'actor': 'fixture-owner',
            'approval_id': 'scope-one', 'approved_at_utc': '2026-01-01T00:00:00Z',
            'provenance': {'kind': 'explicit_owner_instruction', 'evidence_reference': 'fixture-message'},
            'section_id': 'skills', 'method_profile': 'approved_nested_section', 'projection_revision': 'one',
            'projection_sha256': digest(self.projection), 'member_source_ids': [m['source_id'] for m in self.projection['members']],
            'operation_refs': [op['reference'] for op in self.operations], 'allowed_actions': ['MOVE', 'RENAME'],
            'allowed_destination_parent_ids': self.projection['allowed_destination_parent_ids']}
        resolutions = {op['reference']: {'input_sha256': source['input_sha256'],
            'row_sha256': source['records'][op['reference']]['row_sha256'],
            'source_id': op['source_id'], 'live_snapshot_sha256': digest(self.states[op['source_id']]),
            'reviewed_checks': True, **{k: copy.deepcopy(v) for k, v in op.items() if k in
                ('depends_on_refs', 'target_parent_ids', 'exact_target_name', 'extension_correction')}} for op in self.operations}
        return self.journal.evidence(approval), resolutions

    def run_import(self, **kwargs):
        approval, resolutions = self.freeze()
        return import_section(self.journal, self.export, projection=self.projection, owner_approval_ref=approval,
            expected_section_id='skills', batch_id='batch-nested', metadata_by_id=self.states, resolutions=resolutions,
            expected_method_profile='approved_nested_section', **kwargs)

    def test_nested_move_rename_blank_rows_preserve_authority_and_before(self):
        result = self.run_import()
        self.assertIsNotNone(result.plan, result.manifest)
        self.assertEqual(len(result.plan.operations), 4)
        self.assertEqual(validate_section_authority(self.journal, result.plan)['section_id'], 'skills')
        move = next(op for op in result.plan.operations if op.action == 'MOVE')
        self.assertEqual(move.before['name'], '2024')
        self.assertEqual({m['name'] for m in move.before['subtree']['members']}, {'Nested', 'New.pdf'})
        self.assertFalse(any(event['kind'] in ('ATTEMPTED', 'APPROVED') for event in self.journal.records()))

    def test_compiler_imports_root_rename_nested_native_and_inherited_members(self):
        from uqmc_recovery.agent_profile import agent_snapshot
        from uqmc_recovery.section_prepare import build_preparation, resolve_destination_paths, select_records
        def put(tab, row, key, value):
            self.export['sheets'][tab][row][HEADERS[tab].index(key)] = value
        put('Move Manifest', 1, 'Exact target path', 'Club/Skills')
        put('Move Manifest', 1, 'Scope', 'Whole subtree including all descendants')
        put('Proposed Renames', 1, 'Proposed destination folder', 'Club')
        put('Proposed Renames', 1, 'Proposed name + type suffix', 'Skills')
        put('Proposed Renames', 2, 'Proposed destination folder', 'Club/Skills')
        put('Proposed Renames', 3, 'Proposed destination folder', 'Club/Skills/Nested')
        put('Proposed Renames', 3, 'Proposed name + type suffix', 'New native.gdoc')
        self.states['file']['name'] = 'Old native'
        self.states['file']['mime_type'] = 'application/vnd.google-apps.document'
        for container in ('year', 'nested'):
            for member in self.states[container]['subtree']['members']:
                if member['id'] == 'file':
                    member['name'] = 'Old native'
                    member['mime_type'] = self.states['file']['mime_type']
        self.states['year']['subtree']['members'].append({'id': 'inherited', 'name': 'Kept.pdf',
            'mime_type': 'application/pdf', 'parents': ['year']})
        states = {sid: agent_snapshot({**state, 'parents_complete': True}) for sid, state in self.states.items()}
        source = catalog(self.export)
        resolved = resolve_destination_paths(select_records(source, 'Club/Skills'), states, {'Club': 'club'}, 'Club/Skills')
        self.assertEqual(resolved['missing_folders'], [])
        provenance = self.journal.evidence({'kind': 'FIXTURE_NATIVE_METADATA', 'id': 'file', 'mime_type': states['file']['mime_type']})
        proof = self.journal.evidence({'schema_version': 'uqmc.rename.native-display-conversion.v1',
            'source_id': 'file', 'mime_type': states['file']['mime_type'],
            'workbook_row_sha256': source['records']['RENAME-003']['row_sha256'],
            'workbook_proposed_name': 'New native.gdoc', 'exact_target_name': 'New native',
            'display_suffix': '.gdoc', 'provenance_ref': provenance})
        path_proof = self.journal.evidence({'schema_version': 'uqmc.section.destination-paths.v1',
            'section_id': 'skills', 'input_sha256': source['input_sha256'],
            'destination_paths': resolved['destination_paths'], 'provenance_ref': provenance})
        prepared = build_preparation(source, section_path='Club/Skills', section_id='skills', workbook_id=WORKBOOK_ID,
            metadata_by_id=states, destination_paths=resolved['destination_paths'], destination_path_evidence_ref=path_proof,
            native_conversion_refs={'RENAME-003': proof})
        projection = prepared['projection']
        owner = self.journal.evidence({'schema_version': 'uqmc.section.owner-approval.v1', 'decision': 'APPROVED',
            'actor': 'fixture-owner', 'approval_id': 'compiled-scope', 'approved_at_utc': '2026-01-01T00:00:00Z',
            'provenance': {'kind': 'explicit_owner_instruction', 'evidence_reference': 'fixture-message'},
            'section_id': 'skills', 'method_profile': 'approved_nested_section',
            'projection_revision': projection['projection_revision'], 'projection_sha256': digest(projection),
            'member_source_ids': [m['source_id'] for m in projection['members']],
            'operation_refs': prepared['selected_refs'], 'allowed_actions': projection['allowed_actions'],
            'allowed_destination_parent_ids': projection['allowed_destination_parent_ids']})
        result = import_section(self.journal, self.export, projection=projection, owner_approval_ref=owner,
            expected_section_id='skills', batch_id='compiled-nested', metadata_by_id=states, resolutions=prepared['resolutions'],
            expected_method_profile='approved_nested_section', metadata_profile='AGENT_ASSISTED')
        self.assertIsNotNone(result.plan, result.manifest)
        self.assertEqual(len(result.plan.operations), 4)
        self.assertEqual(next(op for op in result.plan.operations if op.file_id == 'file').target, {'name': 'New native'})
        self.assertIn('inherited', {member['source_id'] for member in result.manifest['members']})
        self.assertEqual(validate_section_authority(self.journal, result.plan)['section_id'], 'skills')

    def test_logical_move_alias_can_retain_old_label_under_exact_folder_rename(self):
        self.export['sheets']['Move Manifest'][1][HEADERS['Move Manifest'].index('Exact target path')] = 'Club/Skills/Old year'
        self.export['sheets']['Proposed Renames'][2][HEADERS['Proposed Renames'].index('Proposed destination folder')] = 'Club/Skills/Old year'
        self.export['sheets']['Proposed Renames'][3][HEADERS['Proposed Renames'].index('Proposed destination folder')] = 'Club/Skills/Old year/Nested'
        self.projection['destination_paths'] = {'Club': 'club', 'Club/Skills': 'skills',
            'Club/Skills/Old year': 'year', 'Club/Skills/Old year/Nested': 'nested'}
        result = self.run_import()
        self.assertIsNotNone(result.plan, result.manifest)
        self.assertEqual(next(op for op in result.plan.operations if op.file_id == 'year' and op.action == 'RENAME').target, {'name': '2024'})

    def test_added_descendant_cannot_ride_folder_approval(self):
        self.states['year']['subtree']['members'].append({'id': 'outsider', 'name': 'Extra'})
        result = self.run_import()
        self.assertIsNone(result.plan)
        self.assertEqual(result.manifest['section_reason_code'], 'SECTION_MEMBERSHIP_NOT_PROVEN_BY_COMPLETE_SCOPE')

    def test_changed_folder_mapping_rejected(self):
        self.projection['destination_paths']['Club/Skills/2024'] = 'nested'
        result = self.run_import()
        self.assertEqual(result.manifest['section_reason_code'], 'NESTED_DESTINATION_PATH_AMBIGUOUS')

    def test_wrong_move_parent_rejected_even_with_matching_resolution(self):
        self.operations[-1]['target_parent_ids'] = ['club']
        self.projection['allowed_destination_parent_ids'] = ['club']
        result = self.run_import()
        self.assertEqual(result.manifest['section_reason_code'], 'NESTED_RENAME_DESTINATION_MISMATCH')

    def test_wrong_proposed_rename_rejected(self):
        self.operations[2]['exact_target_name'] = 'Wrong.pdf'
        result = self.run_import()
        self.assertEqual(result.manifest['section_reason_code'], 'WORKBOOK_RENAME_TARGET_MISMATCH')

    def test_omitted_nested_row_rejected(self):
        self.operations.pop(2)
        self.operations[-1]['depends_on_refs'].remove('RENAME-003')
        self.projection['members'][2]['direct_operation_refs'] = []
        result = self.run_import()
        self.assertEqual(result.manifest['section_reason_code'], 'NESTED_SECTION_OPERATION_SET_MISMATCH')

    def test_no_decision_stays_excluded(self):
        self.export['sheets']['Proposed Renames'][3][HEADERS['Proposed Renames'].index('Decision (Yes/No)')] = 'No'
        result = self.run_import()
        self.assertIsNone(result.plan)
        self.assertEqual(result.manifest['section_reason_code'], 'NESTED_SECTION_OPERATION_SET_MISMATCH')

    def test_late_section_revocation_blocks_authority(self):
        result = self.run_import()
        self.journal.append('SECTION_APPROVAL_REVOKED', payload={'approval_id': 'scope-one'})
        with self.assertRaisesRegex(SafetyError, 'REVOKED_OR_SUPERSEDED'):
            validate_section_authority(self.journal, result.plan)

    def test_real_folder_location_must_explain_target_path(self):
        self.states['nested']['parents'] = ['old']
        result = self.run_import()
        self.assertEqual(result.manifest['section_reason_code'], 'NESTED_RENAME_DESTINATION_MISMATCH')

    def test_own_move_must_depend_on_rename(self):
        self.operations[-1]['depends_on_refs'].remove('RENAME-001')
        result = self.run_import()
        self.assertEqual(result.manifest['section_reason_code'], 'NESTED_MOVE_REQUIRES_OWN_RENAME_DEPENDENCY')


if __name__ == '__main__':
    unittest.main()
