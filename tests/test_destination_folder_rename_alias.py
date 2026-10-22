"""Offline same-ID folder alias regressions; original proposal rows stay exact."""
import copy
import tempfile
import unittest
from pathlib import Path

from test_importer import append_row, metadata, set_cell
from uqmc_recovery.agent_profile import agent_snapshot
from uqmc_recovery.destination_planner import build_hybrid_preparation, plan_destination
from uqmc_recovery.importer import HEADERS, WORKBOOK_ID, catalog, workbook_fingerprint
from uqmc_recovery.journal import Journal
from uqmc_recovery.model import FOLDER, SafetyError, digest
from uqmc_recovery.section_importer import import_section, validate_section_authority


PARENT = 'Root/Media'
OLD = PARENT + '/Intro videos'
NEW = PARENT + '/Intro Videos'


class FolderRenameAliasTests(unittest.TestCase):
    def setUp(self):
        self.export = {'workbook_id': WORKBOOK_ID, 'exported_at_utc': '2026-10-21T00:00:00Z',
            'complete': {tab: True for tab in HEADERS},
            'sheets': {tab: [list(headers)] for tab, headers in HEADERS.items()}}
        append_row(self.export, 'Proposed Moves', {'Proposal ID': 'MOVE-MEDIA'})
        append_row(self.export, 'Move Manifest', {'Proposal ID': 'MOVE-MEDIA',
            'Manifest item': 'ITEM-MEDIA', 'Source ID': 'media', 'Current item': 'Media',
            'Exact target path': PARENT, 'Scope': 'Whole subtree including all descendants'})
        append_row(self.export, 'Proposed Renames', {'Rename ID': 'RENAME-INTRO',
            'Source ID': 'intro', 'Current name + type suffix': 'Intro videos',
            'Proposed name + type suffix': 'Intro Videos', 'Proposed destination folder': PARENT})
        append_row(self.export, 'Rename Coverage', {'Source ID': 'intro',
            'Current name + type suffix': 'Intro videos', 'Proposed name / retained name': 'Intro Videos',
            'Rename proposal': 'RENAME-INTRO', 'Verified MIME type': FOLDER,
            'Existing proposed target': OLD})
        raw = {'root': metadata('root', 'Root', FOLDER, []),
            'media': metadata('media', 'Media', FOLDER, ['root']),
            'intro': metadata('intro', 'Intro videos', FOLDER, ['media'])}
        for sid, name in [('explicit', 'Original.mov'), ('ruby', 'Ruby.mov'), ('leyton', 'Leyton.mov')]:
            raw[sid] = metadata(sid, name, 'video/quicktime', ['intro'])
            member = {'id': sid, 'name': name, 'mime_type': 'video/quicktime', 'parents': ['intro']}
            raw['media']['subtree']['members'].append(member)
            raw['intro']['subtree']['members'].append(copy.deepcopy(member))
            if sid == 'explicit':
                append_row(self.export, 'Proposed Renames', {'Rename ID': 'RENAME-EXPLICIT',
                    'Source ID': sid, 'Current name + type suffix': name,
                    'Proposed name + type suffix': 'Approved.mov', 'Proposed destination folder': NEW})
            else:
                append_row(self.export, 'Rename Coverage', {'Source ID': sid,
                    'Assessment': 'Keep name', 'Current name + type suffix': name,
                    'Proposed name / retained name': name, 'Verified MIME type': 'video/quicktime',
                    'Existing proposed target': OLD + '/' + name, 'Move proposal': 'MOVE-MEDIA'})
        raw['media']['subtree']['members'].append({'id': 'intro', 'name': 'Intro videos',
            'mime_type': FOLDER, 'parents': ['media']})
        self.states = {sid: agent_snapshot({**value, 'parents_complete': True}) for sid, value in raw.items()}
        self.anchors = {'Root': 'root'}

    def source(self):
        return catalog(self.export, include_subtree_coverage=True)

    def plan(self, source=None, anchors=None):
        return plan_destination(self.source() if source is None else source, section_path=PARENT,
            metadata_by_id=self.states, anchor_paths=self.anchors if anchors is None else anchors)

    def test_mixed_explicit_and_covered_routes_keep_canonical_old_alias(self):
        source = self.source()
        before = copy.deepcopy((source, self.export, self.states, self.anchors))
        layout = self.plan(source)
        self.assertEqual(layout['destination_paths'][OLD], 'intro')
        self.assertEqual(layout['destination_folder_names'][OLD], 'Intro Videos')
        self.assertNotIn(NEW, layout['destination_paths'])
        self.assertEqual(layout['missing_folders'], [])
        for sid in ('explicit', 'ruby', 'leyton'):
            self.assertEqual(layout['item_routes'][sid]['parent_path'], OLD)
        prepared = build_hybrid_preparation(source, section_path=PARENT, section_id='media',
            workbook_id=WORKBOOK_ID, metadata_by_id=self.states,
            destination_paths=layout['destination_paths'], destination_path_evidence_ref='fixture')
        moves = {op['source_id']: op for op in prepared['projection']['operations'] if op['action'] == 'MOVE'}
        self.assertTrue(all(moves[sid]['target_parent_ids'] == ['intro'] for sid in ('explicit', 'ruby', 'leyton')))
        self.assertEqual([op['action'] for op in prepared['projection']['operations'] if op['source_id'] == 'ruby'], ['MOVE'])
        self.assertEqual(layout['records'], list(sorted(source['records'].values(), key=lambda r: r['reference'])))
        self.assertEqual((source, self.export, self.states, self.anchors), before)
        self.assertEqual(source['input_sha256'], workbook_fingerprint(self.export))
        for ref, resolution in prepared['resolutions'].items():
            if ref in source['records']:
                self.assertEqual(resolution['row_sha256'], source['records'][ref]['row_sha256'])

    def test_completed_rename_preserves_same_registered_alias(self):
        self.states['intro']['name'] = 'Intro Videos'
        next(m for m in self.states['media']['subtree']['members'] if m['id'] == 'intro')['name'] = 'Intro Videos'
        anchors = {**self.anchors, PARENT: 'media', OLD: 'intro'}
        self.assertEqual(self.plan(anchors=anchors)['destination_paths'], anchors)

    def test_unproved_or_mismatched_alias_still_fails_closed(self):
        original = self.source()
        for field, value in [('Source ID', 'other'), ('Verified MIME type', 'application/pdf'),
                ('Rename proposal', 'WRONG'), ('Current name + type suffix', 'Other'),
                ('Proposed name / retained name', 'Other'), ('Existing proposed target', PARENT + '/Wrong')]:
            with self.subTest(field=field):
                source = copy.deepcopy(original)
                source['folder_rename_coverage'][0][field] = value
                with self.assertRaisesRegex(SafetyError, 'AMBIGUOUS_RENAMED_FOLDER_LOGICAL_ALIAS'):
                    self.plan(source)
        source = copy.deepcopy(original)
        source.pop('folder_rename_coverage')
        with self.assertRaisesRegex(SafetyError, 'AMBIGUOUS_RENAMED_FOLDER_LOGICAL_ALIAS'):
            self.plan(source)

    def test_other_source_folder_cannot_claim_alias(self):
        self.states['other'] = agent_snapshot({**metadata('other', 'Intro Videos', FOLDER, ['media']),
                                              'parents_complete': True})
        append_row(self.export, 'Move Manifest', {'Proposal ID': 'MOVE-MEDIA',
            'Manifest item': 'ITEM-OTHER', 'Source ID': 'other', 'Current item': 'Intro Videos',
            'Exact target path': NEW, 'Scope': 'Single item'})
        with self.assertRaisesRegex(SafetyError, 'FOLDER_RENAME_ALIAS_IDENTITY_CONFLICT'):
            self.plan()

    def test_conflicting_registered_identities_cannot_merge(self):
        self.states['other'] = agent_snapshot({**metadata('other', 'Intro Videos', FOLDER, ['media']),
                                              'parents_complete': True})
        with self.assertRaisesRegex(SafetyError, 'FOLDER_RENAME_ALIAS_ANCHOR_IDENTITY_CONFLICT'):
            self.plan(anchors={**self.anchors, OLD: 'intro', NEW: 'other'})

    def test_exact_component_prefix_does_not_redirect_similarly_named_folder(self):
        set_cell(self.export, 'Proposed Renames', 2, 'Proposed destination folder', NEW + ' Extra')
        layout = self.plan()
        self.assertEqual(layout['item_routes']['explicit']['parent_path'], NEW + ' Extra')
        self.assertIn(NEW + ' Extra', [spec['logical_path'] for spec in layout['missing_folders']])

    def test_proved_alias_does_not_relax_move_rename_parent_disagreement(self):
        append_row(self.export, 'Move Manifest', {'Proposal ID': 'MOVE-MEDIA',
            'Manifest item': 'ITEM-EXPLICIT', 'Source ID': 'explicit', 'Current item': 'Original.mov',
            'Exact target path': PARENT + '/Wrong/Original.mov', 'Scope': 'Single item'})
        with self.assertRaisesRegex(SafetyError, 'FILE_MOVE_RENAME_DESTINATION_DISAGREEMENT'):
            self.plan()

    def test_requested_new_alias_section_boundary_is_preserved(self):
        layout = plan_destination(self.source(), section_path=NEW,
            metadata_by_id=self.states, anchor_paths={PARENT: 'media'})
        self.assertEqual(layout['item_routes']['intro']['path'], NEW)
        self.assertEqual(layout['item_routes']['explicit']['parent_path'], NEW)
        self.assertNotIn(OLD, layout['destination_paths'])

    def test_aliases_rebuild_and_import_under_same_section_authority(self):
        source, paths = self.source(), self.plan()['destination_paths']
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(Path(directory) / 'state')
            proof = journal.evidence({'kind': 'EXACT_SYNTHETIC_METADATA'})
            paths_ref = journal.evidence({'schema_version': 'uqmc.section.destination-paths.v1',
                'section_id': 'media', 'input_sha256': source['input_sha256'],
                'destination_paths': paths, 'provenance_ref': proof})
            prepared = build_hybrid_preparation(source, section_path=PARENT, section_id='media',
                workbook_id=WORKBOOK_ID, metadata_by_id=self.states,
                destination_paths=paths, destination_path_evidence_ref=paths_ref)
            projection = prepared['projection']
            owner_ref = journal.evidence({'schema_version': 'uqmc.section.owner-approval.v1',
                'decision': 'APPROVED', 'actor': 'fixture-owner', 'approval_id': 'media-owner',
                'approved_at_utc': '2026-01-01T00:00:00Z',
                'provenance': {'kind': 'explicit_owner_instruction', 'evidence_reference': 'fixture'},
                'section_id': 'media', 'method_profile': projection['method_profile'],
                'projection_revision': projection['projection_revision'],
                'projection_sha256': digest(projection),
                'member_source_ids': [m['source_id'] for m in projection['members']],
                'operation_refs': prepared['selected_refs'], 'allowed_actions': projection['allowed_actions'],
                'allowed_destination_parent_ids': projection['allowed_destination_parent_ids']})
            result = import_section(journal, self.export, projection=projection, owner_approval_ref=owner_ref,
                expected_section_id='media', batch_id='media-batch', metadata_by_id=self.states,
                resolutions=prepared['resolutions'], expected_method_profile='destination_hybrid',
                metadata_profile='AGENT_ASSISTED')
            self.assertIsNotNone(result.plan, result.manifest)
            self.assertEqual(validate_section_authority(journal, result.plan)['section_id'], 'media')
            self.assertFalse(any(e['kind'] == 'ATTEMPTED' for e in journal.records()))


if __name__ == '__main__':
    unittest.main()
