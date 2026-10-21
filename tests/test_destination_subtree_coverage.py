"""Synthetic whole-subtree media regression; no Drive calls or real identities."""
import copy
import tempfile
import unittest
from pathlib import Path

from test_destination_history import MemoryJournal
from test_importer import append_row, metadata
from uqmc_recovery.agent_profile import agent_snapshot
from uqmc_recovery.destination_history import build_history_index, reconcile_item
from uqmc_recovery.destination_planner import plan_destination, build_hybrid_preparation
from uqmc_recovery.importer import HEADERS, WORKBOOK_ID, catalog
from uqmc_recovery.model import FOLDER, SafetyError, digest
from uqmc_recovery.journal import Journal
from uqmc_recovery.section_importer import import_section, validate_section_authority

SECTION = 'Club/Trip'
DESTINATION = SECTION + '/Media'


class WholeSubtreeCoverageTests(unittest.TestCase):
    def setUp(self):
        self.export = {'workbook_id': WORKBOOK_ID, 'exported_at_utc': '2026-10-21T00:00:00Z',
            'complete': {tab: True for tab in HEADERS},
            'sheets': {tab: [list(headers)] for tab, headers in HEADERS.items()}}
        append_row(self.export, 'Proposed Moves', {'Proposal ID': 'MOVE-MEDIA', 'Action': 'Move',
            'Proposed target path': DESTINATION, 'Decision (Yes/No)': ''})
        append_row(self.export, 'Move Manifest', {'Proposal ID': 'MOVE-MEDIA',
            'Manifest item': 'ITEM-MEDIA', 'Source ID': 'source-media', 'Current item': 'Old media',
            'Exact target path': DESTINATION, 'Scope': 'Whole subtree including unlisted children',
            'Source path': 'Old/Media'})
        append_row(self.export, 'Proposed Renames', {'Rename ID': 'RENAME-MEDIA',
            'Source ID': 'source-media', 'Current name + type suffix': 'Old media',
            'Proposed name + type suffix': 'Media', 'Proposed destination folder': SECTION,
            'Move proposal ID': 'MOVE-MEDIA', 'Manifest item': 'ITEM-MEDIA'})
        raw = {'club': metadata('club', 'Club', FOLDER, ['top']),
            'trip': metadata('trip', 'Trip', FOLDER, ['club']),
            'destination-media': metadata('destination-media', 'Media', FOLDER, ['trip']),
            'source-media': metadata('source-media', 'Old media', FOLDER, ['old']),
            'old': metadata('old', 'Old', FOLDER, ['top'])}
        self.kept_ids = ['keep-%02d' % index for index in range(50)]
        self.explicit_ids = ['explicit-%02d' % index for index in range(5)]
        for index, sid in enumerate(self.kept_ids + self.explicit_ids):
            mime = 'video/quicktime' if index < 38 or index >= 50 else 'image/jpeg'
            name = 'Clip %02d.mov' % index if mime == 'video/quicktime' else 'Photo %02d.JPG' % index
            raw[sid] = metadata(sid, name, mime, ['source-media'])
            raw['source-media']['subtree']['members'].append(
                {'id': sid, 'name': name, 'mime_type': mime, 'parents': ['source-media']})
            append_row(self.export, 'Rename Coverage', {'Rename necessity': 'Keep name',
                'Assessment': 'Keep name', 'Current name + type suffix': name,
                'Proposed name / retained name': name, 'Source ID': sid,
                'Verified MIME type': mime, 'Item kind': 'File',
                'Existing proposed target': DESTINATION + '/' + name, 'Move proposal': 'MOVE-MEDIA',
                'Manifest item / root reference': 'DESC-%03d' % index,
                'Assessment scope': 'Descendant of ITEM-MEDIA under MOVE-MEDIA; name assessment only, not an additional move',
                'Coverage basis': 'Whole subtree including unlisted children'})
            if sid in self.explicit_ids:
                append_row(self.export, 'Proposed Renames', {'Rename ID': 'RENAME-' + sid,
                    'Source ID': sid, 'Current name + type suffix': name,
                    'Proposed name + type suffix': 'Approved ' + name,
                    'Proposed destination folder': DESTINATION,
                    'Move proposal ID': 'MOVE-MEDIA', 'Manifest item': 'ITEM-MEDIA'})
        self.states = {sid: agent_snapshot({**value, 'parents_complete': True})
                       for sid, value in raw.items()}
        self.paths = {'Club': 'club', SECTION: 'trip', DESTINATION: 'destination-media'}

    def plan(self):
        return plan_destination(catalog(self.export, include_subtree_coverage=True), section_path=SECTION,
            metadata_by_id=self.states, anchor_paths=self.paths)

    def prepare(self):
        return build_hybrid_preparation(catalog(self.export, include_subtree_coverage=True), section_path=SECTION,
            section_id='trip', workbook_id=WORKBOOK_ID, metadata_by_id=self.states,
            destination_paths=self.paths, destination_path_evidence_ref='fixture-exact-path-evidence')

    def test_fifty_keep_name_members_join_five_explicit_members(self):
        layout = self.plan()
        self.assertEqual(set(layout['item_routes']) - {'source-media'},
                         set(self.kept_ids + self.explicit_ids))
        self.assertEqual(layout['source_container_ids'], ['source-media'])
        prepared = self.prepare()
        self.assertEqual(set(prepared['source_ids']), set(self.kept_ids + self.explicit_ids))
        for sid in self.kept_ids:
            self.assertEqual(layout['item_routes'][sid],
                {'parent_path': DESTINATION, 'exact_name': self.states[sid]['name']})
            ops = [op for op in prepared['projection']['operations'] if op['source_id'] == sid]
            self.assertEqual([op['action'] for op in ops], ['MOVE'])
            self.assertEqual(ops[0]['target_parent_ids'], ['destination-media'])
        self.assertEqual(sum(op['action'] == 'RENAME' for op in prepared['projection']['operations']), 5)

    def test_explicit_other_destination_and_withdrawn_child_are_not_dragged(self):
        outside, excluded = self.kept_ids[-2:]
        append_row(self.export, 'Proposed Renames', {'Rename ID': 'RENAME-OUTSIDE',
            'Source ID': outside, 'Current name + type suffix': self.states[outside]['name'],
            'Proposed name + type suffix': self.states[outside]['name'],
            'Proposed destination folder': 'Club/Unrelated'})
        headers = HEADERS['Rename Coverage']
        row = next(row for row in self.export['sheets']['Rename Coverage'][1:]
                   if row[headers.index('Source ID')] == excluded)
        row[headers.index('Assessment')] = 'Withdrawn'
        prepared = self.prepare()
        self.assertEqual(set(prepared['source_ids']), set(self.kept_ids + self.explicit_ids) - {outside, excluded})
        self.assertNotIn('source-media', prepared['source_ids'])
        self.assertEqual(prepared['projection']['source_container_ids'], ['source-media'])

    def test_single_item_scope_does_not_expand_to_unlisted_children(self):
        self.export['sheets']['Move Manifest'][1][HEADERS['Move Manifest'].index('Scope')] = 'Single item'
        prepared = self.prepare()
        self.assertEqual(set(prepared['source_ids']), set(self.explicit_ids))
        self.assertEqual(prepared['projection']['source_container_ids'], ['source-media'])

    def test_coverage_members_import_under_exact_section_authority(self):
        source = catalog(self.export, include_subtree_coverage=True)
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(Path(directory) / 'state')
            proof = journal.evidence({'kind': 'EXACT_SYNTHETIC_METADATA'})
            paths_ref = journal.evidence({'schema_version': 'uqmc.section.destination-paths.v1',
                'section_id': 'trip', 'input_sha256': source['input_sha256'],
                'destination_paths': self.paths, 'provenance_ref': proof})
            prepared = build_hybrid_preparation(source, section_path=SECTION, section_id='trip',
                workbook_id=WORKBOOK_ID, metadata_by_id=self.states,
                destination_paths=self.paths, destination_path_evidence_ref=paths_ref)
            projection = prepared['projection']
            owner_ref = journal.evidence({'schema_version': 'uqmc.section.owner-approval.v1',
                'decision': 'APPROVED', 'actor': 'fixture-owner', 'approval_id': 'media-owner',
                'approved_at_utc': '2026-01-01T00:00:00Z',
                'provenance': {'kind': 'explicit_owner_instruction', 'evidence_reference': 'fixture'},
                'section_id': 'trip', 'method_profile': projection['method_profile'],
                'projection_revision': projection['projection_revision'],
                'projection_sha256': digest(projection),
                'member_source_ids': [m['source_id'] for m in projection['members']],
                'operation_refs': prepared['selected_refs'], 'allowed_actions': projection['allowed_actions'],
                'allowed_destination_parent_ids': projection['allowed_destination_parent_ids']})
            result = import_section(journal, self.export, projection=projection, owner_approval_ref=owner_ref,
                expected_section_id='trip', batch_id='media-batch', metadata_by_id=self.states,
                resolutions=prepared['resolutions'], expected_method_profile='destination_hybrid',
                metadata_profile='AGENT_ASSISTED')
            self.assertIsNotNone(result.plan, result.manifest)
            self.assertEqual({o.file_id for o in result.plan.operations}, set(self.kept_ids + self.explicit_ids))
            self.assertEqual(validate_section_authority(journal, result.plan)['section_id'], 'trip')
            self.assertFalse(any(e['kind'] == 'ATTEMPTED' for e in journal.records()))
            self.assertEqual(self.export['sheets']['Proposed Moves'][1][HEADERS['Proposed Moves'].index('Decision (Yes/No)')], '')

    def test_historical_catalog_stays_unchanged_without_opt_in(self):
        records = catalog(self.export)['records']
        self.assertFalse(set(self.kept_ids) & {r['source_id'] for r in records.values()})
        expanded = catalog(self.export, include_subtree_coverage=True)['records']
        self.assertTrue(set(self.kept_ids) <= {r['source_id'] for r in expanded.values()})

    def test_unknown_live_child_cannot_be_silently_discarded_from_whole_subtree(self):
        self.states['source-media']['subtree']['members'].append({
            'id': 'unknown-child', 'name': 'Unknown.mov', 'mime_type': 'video/quicktime',
            'parents': ['source-media']})
        self.states['unknown-child'] = agent_snapshot({**metadata(
            'unknown-child', 'Unknown.mov', 'video/quicktime', ['source-media']), 'parents_complete': True})
        with self.assertRaises(SafetyError):
            self.plan()

    def test_numeric_retained_year_folder_is_move_only(self):
        sid = self.kept_ids[0]
        self.states[sid] = agent_snapshot({**metadata(sid, '2020', FOLDER, ['source-media']),
                                           'parents_complete': True})
        member = next(m for m in self.states['source-media']['subtree']['members'] if m['id'] == sid)
        member.update(name='2020', mime_type=FOLDER)
        headers = HEADERS['Rename Coverage']
        row = next(r for r in self.export['sheets']['Rename Coverage'][1:] if r[headers.index('Source ID')] == sid)
        for key, value in {'Current name + type suffix': '2020', 'Proposed name / retained name': 2020,
                           'Existing proposed target': DESTINATION + '/2020', 'Item kind': 'Folder',
                           'Verified MIME type': FOLDER}.items():
            row[headers.index(key)] = value
        prepared = self.prepare()
        ops = [o for o in prepared['projection']['operations'] if o['source_id'] == sid]
        self.assertEqual([o['action'] for o in ops], ['MOVE'])
        self.assertEqual(ops[0]['target_parent_ids'], ['destination-media'])
        self.assertEqual(self.plan()['item_routes'][sid]['exact_name'], '2020')

    def test_shortcut_display_suffix_absent_from_path_never_synthesizes_rename(self):
        sid, mime, exact_name = self.kept_ids[0], 'application/vnd.google-apps.shortcut', 'Safety links'
        self.states[sid] = agent_snapshot({**metadata(sid, exact_name, mime, ['source-media']),
                                           'parents_complete': True})
        member = next(m for m in self.states['source-media']['subtree']['members'] if m['id'] == sid)
        member.update(name=exact_name, mime_type=mime)
        headers = HEADERS['Rename Coverage']
        row = next(r for r in self.export['sheets']['Rename Coverage'][1:] if r[headers.index('Source ID')] == sid)
        for key, value in {'Current name + type suffix': exact_name + '.shortcut',
                           'Proposed name / retained name': exact_name + '.shortcut',
                           'Existing proposed target': DESTINATION + '/' + exact_name,
                           'Verified MIME type': mime}.items():
            row[headers.index(key)] = value
        layout = self.plan()
        self.assertEqual(layout['item_routes'][sid], {'parent_path': DESTINATION, 'exact_name': exact_name})
        prepared = self.prepare()
        ops = [o for o in prepared['projection']['operations'] if o['source_id'] == sid]
        self.assertEqual([o['action'] for o in ops], ['MOVE'])
        self.assertEqual(ops[0]['target_parent_ids'], ['destination-media'])

    def test_excluded_whole_subtree_root_cannot_authorize_derived_children(self):
        source = catalog(self.export, include_subtree_coverage=True)
        source['excluded_ids'].append('source-media')
        layout = plan_destination(source, section_path=SECTION,
            metadata_by_id=self.states, anchor_paths=self.paths)
        self.assertFalse(set(self.kept_ids) & set(layout['item_routes']))
        self.assertFalse(set(self.kept_ids) & {r['source_id'] for r in layout['records']})

    def test_fake_drive_command_moves_covered_children_once_and_then_skips(self):
        import test_destination_command as command_fixture
        fixture = command_fixture.DestinationCommandTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.server.drive.files['existing'] = command_fixture.raw(
            'existing', 'Skills', ['club'], True)
        before_root = copy.deepcopy(fixture.server.drive.files['source-root'])
        for sid, name in [('covered-one', 'Retained one.pdf'), ('covered-two', 'Retained two.pdf')]:
            fixture.server.drive.files[sid] = command_fixture.raw(sid, name, ['source-root'])
            append_row(fixture.export, 'Rename Coverage', {'Rename necessity': 'Keep name',
                'Assessment': 'Keep name', 'Current name + type suffix': name,
                'Proposed name / retained name': name, 'Source ID': sid,
                'Verified MIME type': 'application/pdf', 'Item kind': 'File',
                'Existing proposed target': command_fixture.SECTION + '/' + name,
                'Move proposal': 'MOVE-001', 'Manifest item / root reference': 'DESC-' + sid,
                'Assessment scope': 'Descendant of ITEM-001 under MOVE-001; name assessment only, not an additional move',
                'Coverage basis': 'Whole subtree including unlisted children'})
        first = fixture.run_command(apply=True)
        self.assertEqual(first['counts'], {'APPLIED': 3}, first)
        self.assertEqual(fixture.server.drive.files['source-root'], before_root)
        self.assertEqual(fixture.server.created, [])
        covered = [request for request in fixture.server.requests if request['fileId'].startswith('covered-')]
        self.assertEqual(len(covered), 2)
        self.assertTrue(all('name' not in request and request['addParents'] == 'existing'
                            and request['removeParents'] == 'source-root' for request in covered))
        requests = copy.deepcopy(fixture.server.requests)
        second = fixture.run_command(apply=True)
        self.assertTrue(second['no_op'], second)
        self.assertEqual(second['counts'], {'COMPLETE': 3})
        self.assertEqual(fixture.server.requests, requests)
        history = build_history_index(Journal(fixture.config['store']))
        for sid in ('covered-one', 'covered-two'):
            self.assertEqual(history[sid]['completed_fields'], {'parents': ['existing']})
            self.assertEqual(history[sid]['pending_requests'], [])

    def test_narrow_root_alias_keeps_same_id_approved_folder_rename(self):
        approved_name = 'Media - Source Photos'
        self.export['sheets']['Proposed Renames'][1][HEADERS['Proposed Renames'].index(
            'Proposed name + type suffix')] = approved_name
        self.states['destination-media']['name'] = approved_name
        source = catalog(self.export, include_subtree_coverage=True)
        layout = plan_destination(source, section_path=DESTINATION,
            metadata_by_id=self.states,
            anchor_paths={SECTION: 'trip', DESTINATION: 'destination-media'})
        self.assertIn('RENAME-MEDIA', {record['reference'] for record in layout['records']})
        self.assertEqual(layout['destination_folder_names'][DESTINATION], approved_name)
        self.assertEqual(layout['destination_paths'][DESTINATION], 'destination-media')
        self.assertEqual(layout['item_routes']['source-media']['path'], DESTINATION)
        self.assertEqual(layout['item_routes']['source-media']['exact_name'], approved_name)
        self.assertEqual(set(layout['item_routes']) - {'source-media'},
                         set(self.kept_ids + self.explicit_ids))

    def test_completed_five_skipped_while_fifty_are_move_only(self):
        layout = self.plan()
        journal = MemoryJournal()
        operations = []
        for sid in self.explicit_ids:
            before = copy.deepcopy(self.states[sid])
            target_name = layout['item_routes'][sid]['exact_name']
            operations += [
                {'operation_id': 'rename-' + sid, 'file_id': sid, 'action': 'RENAME',
                 'direction': 'FORWARD', 'before': before, 'target': {'name': target_name}, 'depends_on': []},
                {'operation_id': 'move-' + sid, 'file_id': sid, 'action': 'MOVE',
                 'direction': 'FORWARD', 'before': {**before, 'name': target_name},
                 'target': {'parents': ['destination-media']}, 'depends_on': ['rename-' + sid]}]
        plan = {'batch_id': 'prior-five', 'run_id': 'prior-five', 'proposal_revision': 'fixture',
                'excluded_ids': [], 'operations': operations}
        plan_hash = digest(plan)
        journal.add('DRAFTED', {'proposal_hash': plan_hash, 'plan_ref': journal.evidence(plan)})
        for sid in self.explicit_ids:
            before = copy.deepcopy(self.states[sid])
            target = {**before, 'name': layout['item_routes'][sid]['exact_name'], 'parents': ['destination-media']}
            journal.add('DESIRED_APPLIED', {'plan_sha256': plan_hash, 'source_id': sid,
                'direction': 'FORWARD', 'before_ref': journal.evidence(before),
                'target_ref': journal.evidence(target), 'observed_ref': journal.evidence(target),
                'applied_ref': journal.evidence(target)}, 'desired-' + sid)
            self.states[sid] = target
        self.states['source-media']['subtree']['members'] = [m for m in self.states['source-media']['subtree']['members'] if m['id'] in self.kept_ids]
        layout = self.plan()
        history = build_history_index(journal)
        results = []
        for sid in self.kept_ids + self.explicit_ids:
            desired = {**self.states[sid], 'name': layout['item_routes'][sid]['exact_name'],
                       'parents': ['destination-media']}
            results.append(reconcile_item(history.get(sid), desired, self.states[sid], baseline=self.states[sid]))
        self.assertEqual(sum(r['disposition'] == 'COMPLETE' for r in results), 5)
        pending = [r for r in results if r['disposition'] == 'READY']
        self.assertEqual({r['source_id'] for r in pending}, set(self.kept_ids))
        self.assertTrue(all(r['pending_fields'] == ['parents'] for r in pending))


if __name__ == '__main__':
    unittest.main()
