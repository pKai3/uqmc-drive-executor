"""Exact partition coverage regressions using synthetic metadata only."""
import copy
import unittest

import test_destination_subtree_coverage as fixtures
from test_importer import append_row, metadata, set_cell
from uqmc_recovery.agent_profile import agent_snapshot
from uqmc_recovery.destination_planner import plan_destination
from uqmc_recovery.importer import HEADERS, catalog
from uqmc_recovery.model import SafetyError


SCOPE = 'Exact source-ID subtree partition; exclude separately scoped descendants'


class ExactPartitionCoverageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.WholeSubtreeCoverageTests()
        self.fixture.setUp()
        fixture = self.fixture
        fixture.kept_ids = fixture.kept_ids[38:45]
        fixture.explicit_ids = []
        self.export, self.states = fixture.export, fixture.states
        members = self.states['source-media']['subtree']['members']
        removed = {m['id'] for m in members} - set(fixture.kept_ids)
        self.states['source-media']['subtree']['members'] = [m for m in members if m['id'] not in removed]
        for sid in removed:
            self.states.pop(sid)
        self.export['sheets']['Proposed Renames'] = self.export['sheets']['Proposed Renames'][:2]
        headers = HEADERS['Rename Coverage']
        self.export['sheets']['Rename Coverage'] = [list(headers)] + [
            row for row in self.export['sheets']['Rename Coverage'][1:]
            if row[headers.index('Source ID')] in fixture.kept_ids]
        set_cell(self.export, 'Move Manifest', 1, 'Scope', SCOPE)
        for row in self.export['sheets']['Rename Coverage'][1:]:
            row[headers.index('Manifest item / root reference')] = 'ITEM-MEDIA'
            row[headers.index('Assessment scope')] = (
                'Descendant of ITEM-MEDIA; naming assessment only, not another move')
            row[headers.index('Coverage basis')] = 'Naming inventory only'

    def derived(self, **kwargs):
        return [r for r in catalog(self.export, include_subtree_coverage=True, **kwargs)['records'].values()
                if r.get('whole_subtree_origin_reference')]

    def test_seven_keep_name_photos_receive_move_only_routes(self):
        self.assertEqual(len(self.fixture.kept_ids), 7)
        before = copy.deepcopy((self.export, self.states))
        prepared = self.fixture.prepare()
        self.assertEqual(set(prepared['source_ids']), set(self.fixture.kept_ids))
        self.assertEqual(prepared['projection']['source_container_ids'], ['source-media'])
        operations = prepared['projection']['operations']
        self.assertEqual(len(operations), 7)
        self.assertTrue(all(op['action'] == 'MOVE' and op['target_parent_ids'] == ['destination-media']
                            for op in operations))
        self.assertTrue(all(r['whole_subtree_origin_source_id'] == 'source-media'
                            and r['whole_subtree_origin_reference'] == 'MOVE-MEDIA/ITEM-MEDIA'
                            for r in self.derived()))
        self.assertEqual((self.export, self.states), before)
        historical = catalog(self.export)['records']
        self.assertFalse(set(self.fixture.kept_ids) & {r['source_id'] for r in historical.values()})

    def test_matching_proposal_and_manifest_are_both_required(self):
        for field, value in [('Move proposal', ''), ('Move proposal', 'MOVE-OTHER'),
                             ('Manifest item / root reference', ''),
                             ('Manifest item / root reference', 'ITEM-MEDIA-OTHER'),
                             ('Manifest item / root reference', 'ITEM-OTHER')]:
            with self.subTest(field=field, value=value):
                self.setUp()
                set_cell(self.export, 'Rename Coverage', 1, field, value)
                with self.assertRaisesRegex(SafetyError, 'UNBOUND_SUBTREE_COVERAGE'):
                    self.derived()

    def test_same_proposal_and_target_cannot_substitute_another_manifest_root(self):
        append_row(self.export, 'Move Manifest', {'Proposal ID': 'MOVE-MEDIA',
            'Manifest item': 'ITEM-OTHER', 'Source ID': 'source-other',
            'Exact target path': fixtures.DESTINATION, 'Scope': SCOPE})
        self.assertEqual({r['whole_subtree_origin_source_id'] for r in self.derived()}, {'source-media'})
        set_cell(self.export, 'Move Manifest', 1, 'Source ID', '')
        with self.assertRaisesRegex(SafetyError, 'UNBOUND_SUBTREE_COVERAGE'):
            self.derived()

    def test_only_supported_scope_and_exact_target_prefix_expand(self):
        for scope in ['Single item', 'Exact source-ID subtree partition', SCOPE + '; additional scope']:
            with self.subTest(scope=scope):
                set_cell(self.export, 'Move Manifest', 1, 'Scope', scope)
                self.assertEqual(self.derived(), [])
        set_cell(self.export, 'Move Manifest', 1, 'Scope', SCOPE)
        set_cell(self.export, 'Rename Coverage', 1, 'Existing proposed target',
                 fixtures.DESTINATION + '-Other/' + self.states[self.fixture.kept_ids[0]]['name'])
        self.assertEqual({r['source_id'] for r in self.derived()}, set(self.fixture.kept_ids[1:]))
        self.assertEqual(self.derived(subtree_section_path='Club/Unrelated'), [])

    def test_separate_descendant_move_and_exclusions_override_partition(self):
        outside, withdrawn, rejected = self.fixture.kept_ids[-3:]
        for sid, pid, target, decision in [
                (outside, 'MOVE-OUTSIDE', 'Club/Unrelated', ''),
                (rejected, 'MOVE-REJECTED', fixtures.DESTINATION, 'No')]:
            append_row(self.export, 'Proposed Moves', {'Proposal ID': pid,
                'Action': 'Move', 'Decision (Yes/No)': decision})
            append_row(self.export, 'Move Manifest', {'Proposal ID': pid, 'Manifest item': 'ITEM-' + sid,
                'Source ID': sid, 'Current item': self.states[sid]['name'],
                'Exact target path': target + '/' + self.states[sid]['name'], 'Scope': 'Single item'})
        set_cell(self.export, 'Rename Coverage', 6, 'Assessment', 'Withdrawn')
        prepared = self.fixture.prepare()
        self.assertEqual(set(prepared['source_ids']), set(self.fixture.kept_ids) - {outside, withdrawn, rejected})
        self.assertEqual(prepared['projection']['source_container_ids'], ['source-media'])
        self.assertFalse({outside, withdrawn, rejected} & set(self.fixture.plan()['item_routes']))

    def test_root_rejection_blocks_inherited_authority(self):
        set_cell(self.export, 'Proposed Moves', 1, 'Decision (Yes/No)', 'No')
        self.assertTrue(all(r['decision'] == 'No' for r in self.derived()))
        with self.assertRaisesRegex(SafetyError, 'SECTION_NO_ELIGIBLE_RECORDS'):
            self.fixture.plan()

    def test_unknown_live_partition_child_is_blocked(self):
        self.states['source-media']['subtree']['members'].append({
            'id': 'unknown-child', 'name': 'Unknown.jpg', 'mime_type': 'image/jpeg',
            'parents': ['source-media']})
        self.states['unknown-child'] = agent_snapshot({**metadata(
            'unknown-child', 'Unknown.jpg', 'image/jpeg', ['source-media']), 'parents_complete': True})
        with self.assertRaisesRegex(SafetyError, 'CHILD_MISSING_PROPOSAL_COVERAGE'):
            self.fixture.plan()

    def test_unbound_known_child_cannot_silently_reduce_scope(self):
        set_cell(self.export, 'Rename Coverage', 1, 'Manifest item / root reference', 'ITEM-OTHER')
        with self.assertRaisesRegex(SafetyError, 'UNBOUND_SUBTREE_COVERAGE:.*Rename Coverage row 2'):
            self.fixture.plan()

    def test_descendant_inventory_ref_binds_exact_parent_assertion(self):
        for index in range(1, 8):
            set_cell(self.export, 'Rename Coverage', index, 'Manifest item / root reference', 'DESC-' + str(index))
            set_cell(self.export, 'Rename Coverage', index, 'Assessment scope',
                     'Descendant of ITEM-MEDIA under MOVE-MEDIA; name assessment only, not an additional move')
        before = copy.deepcopy(self.export)
        prepared = self.fixture.prepare()
        self.assertEqual(set(prepared['source_ids']), set(self.fixture.kept_ids))
        self.assertTrue(all(op['action'] == 'MOVE' for op in prepared['projection']['operations']))
        self.assertEqual(self.export, before)

    def test_desc_ref_needs_exact_scope_proof_not_prefix_or_free_text(self):
        for proof in ['', 'Descendant of ITEM-OTHER under MOVE-MEDIA; name assessment only, not an additional move',
                      'Descendant of ITEM-MEDIA under MOVE-OTHER; name assessment only, not an additional move',
                      'Descendant of ITEM-MEDIA under MOVE-MEDIA; name assessment only, not an additional move EXTRA',
                      'See ITEM-MEDIA under MOVE-MEDIA']:
            with self.subTest(proof=proof):
                self.setUp()
                set_cell(self.export, 'Rename Coverage', 1, 'Manifest item / root reference', 'DESC-child')
                set_cell(self.export, 'Rename Coverage', 1, 'Assessment scope', proof)
                with self.assertRaisesRegex(SafetyError, 'UNBOUND_SUBTREE_COVERAGE'):
                    self.derived()

    def test_withdrawn_coverage_cannot_block_or_reopen_via_bad_binding(self):
        for field in ['Coverage basis', 'Rename necessity']:
            with self.subTest(field=field):
                self.setUp()
                sid = self.fixture.kept_ids[0]
                set_cell(self.export, 'Rename Coverage', 1, field, 'Withdrawn')
                set_cell(self.export, 'Rename Coverage', 1, 'Manifest item / root reference', 'DESC-bad')
                prepared = self.fixture.prepare()
                self.assertEqual(set(prepared['source_ids']), set(self.fixture.kept_ids) - {sid})
                self.assertNotIn(sid, self.fixture.plan()['item_routes'])

    def test_all_unbound_rows_reported_together(self):
        for index in [1, 3, 5]:
            set_cell(self.export, 'Rename Coverage', index, 'Manifest item / root reference', 'DESC-missing')
        with self.assertRaises(SafetyError) as raised:
            self.derived()
        for index in [0, 2, 4]:
            self.assertIn(self.fixture.kept_ids[index], str(raised.exception))

    def test_inventory_presence_cannot_hide_dropped_catalog_record(self):
        source = catalog(self.export, include_subtree_coverage=True)
        sid = self.fixture.kept_ids[0]
        source['records'] = {ref: r for ref, r in source['records'].items() if r['source_id'] != sid}
        self.assertIn(sid, source['coverage_source_ids'])
        with self.assertRaisesRegex(SafetyError, 'CHILD_MISSING_PROPOSAL_COVERAGE:.*' + sid):
            plan_destination(source, section_path=fixtures.SECTION,
                             metadata_by_id=self.states, anchor_paths=self.fixture.paths)

    def test_outside_inventory_route_stays_outside_without_source_folder_reuse(self):
        sid = self.fixture.kept_ids[0]
        set_cell(self.export, 'Rename Coverage', 1, 'Existing proposed target',
                 'Club/Unrelated/' + self.states[sid]['name'])
        paths = {p: i for p, i in self.fixture.paths.items() if p != fixtures.DESTINATION}
        layout = plan_destination(catalog(self.export, include_subtree_coverage=True),
            section_path=fixtures.SECTION, metadata_by_id=self.states, anchor_paths=paths)
        self.assertNotIn(sid, layout['item_routes'])
        self.assertIn('source-media', layout['source_container_ids'])

    def test_thirty_five_desc_rows_are_all_prepared_without_renames(self):
        fixture = fixtures.WholeSubtreeCoverageTests()
        fixture.setUp()
        selected = set(fixture.kept_ids[:35])
        remove = set(fixture.kept_ids + fixture.explicit_ids) - selected
        for sid in remove:
            fixture.states.pop(sid)
        fixture.states['source-media']['subtree']['members'] = [
            m for m in fixture.states['source-media']['subtree']['members'] if m['id'] in selected]
        fixture.export['sheets']['Proposed Renames'] = fixture.export['sheets']['Proposed Renames'][:2]
        h = HEADERS['Rename Coverage']
        fixture.export['sheets']['Rename Coverage'] = [list(h)] + [
            row for row in fixture.export['sheets']['Rename Coverage'][1:] if row[h.index('Source ID')] in selected]
        set_cell(fixture.export, 'Move Manifest', 1, 'Scope', SCOPE)
        prepared = fixture.prepare()
        self.assertEqual(set(prepared['source_ids']), selected)
        ops = prepared['projection']['operations']
        self.assertEqual(len(ops), 35)
        self.assertTrue(all(op['action'] == 'MOVE' and op['target_parent_ids'] == ['destination-media'] for op in ops))


if __name__ == '__main__':
    unittest.main()
