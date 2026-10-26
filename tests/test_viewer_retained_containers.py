"""Source-only wrappers must not collide with active destination contexts."""
import copy
import unittest
from test_viewer_publication import node
from uqmc_recovery import viewer_publication as v


class RetainedContainerTests(unittest.TestCase):
    def fixture(self):
        data = {'nodes': [node(0, -1, 'Governance'),
            node(1, 0, 'Procedures'),
            node(2, 1, '2026', sid='wrapper', old='2026 Procedures'),
            node(3, 2, 'Ratified PDFs', sid='pdf-wrapper'),
            node(4, 1, '2026'),
            node(5, 4, 'Water Activities'),
            node(6, 3, 'Procedure.pdf', False, 'file'),
            node(7, 1, 'Old context')],
            'roots': {'destination': [0], 'retained': []}, 'notes': []}
        for i in (2, 3, 7): data['nodes'][i]['viewer_hidden'] = True
        # Simulate stale child arrays in an independently corrected viewer.
        data['nodes'][2]['c'] = [3, 4, 5, 6]
        workbook = {'exported_at_utc': '2026-10-26T00:00:00Z', 'sheets': {
            'Proposed Renames': [
                ['Source ID', 'Current name + type suffix', 'Proposed name + type suffix', 'Proposed destination folder'],
                ['wrapper', '2026 Procedures', '2026 Procedures', 'Source'],
                ['pdf-wrapper', 'Ratified PDFs', 'Ratified PDFs', 'Source/2026 Procedures'],
                ['file', 'Procedure.pdf', 'Procedure.pdf', 'Governance/Procedures/2026/Water Activities']],
            'Move Manifest': [
                ['Source ID', 'Source path', 'Exact target path', 'Scope'],
                ['wrapper', 'Source/2026 Procedures', 'Source/2026 Procedures', 'Retain container only'],
                ['pdf-wrapper', 'Source/2026 Procedures/Ratified PDFs', 'Source/2026 Procedures/Ratified PDFs', 'Retain container only']]}}
        return data, workbook

    def test_retained_wrappers_do_not_compete_with_destination_or_create_source_branches(self):
        data, workbook = self.fixture()
        ids = {n['id'] for n in data['nodes'] if n['id']}
        for _ in range(2):
            self.assertEqual(v.proposed_folder_updates(data, workbook), [])
            v.refresh_proposal(data, workbook)
            v.mark_retired_empty_folders(data, workbook)
            destination = v.resolve_folder_path(data, 'Governance/Procedures/2026')
            self.assertEqual(destination['id'], '')
            self.assertFalse(destination.get('viewer_hidden'))
            file = next(n for n in data['nodes'] if n['id'] == 'file')
            self.assertEqual(file['p'], v.resolve_folder_path(data, 'Governance/Procedures/2026/Water Activities')['i'])
            self.assertFalse(any(n['n'] == 'Source' for n in data['nodes']))
            self.assertEqual(ids, {n['id'] for n in data['nodes'] if n['id']})
            for n in data['nodes']:
                self.assertLess(n['p'], n['i'])
                self.assertEqual(n['c'], [c['i'] for c in data['nodes'] if c['p'] == n['i']])
            hidden = [n for n in data['nodes'] if n.get('viewer_hidden')]
            self.assertEqual({n['id'] for n in hidden}, {'wrapper', 'pdf-wrapper', ''})
            self.assertTrue(all(not n.get('retired_empty_parent') for n in hidden))

    def test_visible_different_identity_collision_remains_blocked(self):
        data, workbook = self.fixture()
        data['nodes'].append(node(8, 1, '2026', sid='different-folder'))
        v.proposed_folder_updates(data, workbook)
        with self.assertRaisesRegex(ValueError, 'AMBIGUOUS_PROPOSAL_FOLDER'):
            v.refresh_proposal(data, workbook)

    def test_literal_slash_in_retained_title_is_not_parsed_as_hierarchy(self):
        data, workbook = self.fixture()
        rows = workbook['sheets']
        rows['Proposed Renames'][1][1:3] = ['Old / Superseded'] * 2
        rows['Proposed Renames'][2][3] = 'Source/Old / Superseded'
        for row in rows['Move Manifest'][1:]:
            row[1:3] = [p.replace('2026 Procedures', 'Old / Superseded') for p in row[1:3]]
        self.assertEqual(v.proposed_folder_updates(data, workbook), [])
        v.refresh_proposal(data, workbook)
        self.assertFalse(any(n['n'] == 'Source' for n in data['nodes']))

    def test_changed_original_name_or_source_parent_is_not_silently_hidden(self):
        for field in (1, 3):
            data, workbook = self.fixture()
            workbook['sheets']['Proposed Renames'][1][field] = 'Changed'
            with self.assertRaisesRegex(ValueError, 'RETAINED_CONTAINER_RENAME_CONFLICT'):
                v.proposed_folder_updates(data, workbook)

    def test_optional_at_source_rename_is_not_a_destination_move(self):
        data, workbook = self.fixture()
        workbook['sheets']['Proposed Renames'][1][2] = 'Clean source name'
        self.assertEqual(v.proposed_folder_updates(data, workbook), [])
        v.refresh_proposal(data, workbook)
        self.assertFalse(any(n['n'] == 'Clean source name' for n in data['nodes']))

    def test_inconsistent_retained_target_is_rejected(self):
        data, workbook = self.fixture()
        workbook['sheets']['Move Manifest'][1][2] = 'Elsewhere'
        with self.assertRaisesRegex(ValueError, 'RETAINED_CONTAINER_TARGET_CHANGED'):
            v.proposed_folder_updates(data, workbook)

    def test_unmapped_active_file_cannot_be_hidden(self):
        data, workbook = self.fixture()
        workbook['sheets']['Proposed Renames'].pop()
        v.refresh_proposal(data, workbook)
        with self.assertRaisesRegex(ValueError, 'ACTIVE_PROPOSAL_UNDER_HIDDEN_FOLDER:file'):
            v.mark_retired_empty_folders(data, workbook)

    def test_pending_folder_reuses_anonymous_proposal_context_without_losing_children(self):
        from test_viewer_revision import tree, workbook, revision
        data = tree()
        data['nodes'] += [node(4, 0, '2024'), node(5, 4, 'Minutes'),
                          node(6, 5, 'Already routed.pdf', False, 'other')]
        identities = {n['id'] for n in data['nodes'] if n['id']}
        for _ in range(2):
            v.refresh_proposal(data, workbook(), folder_revisions=[revision(False)], proposed_layout=True)
            target = v.resolve_folder_path(data, 'Root/2024/Minutes')
            self.assertEqual(target['id'], 'folder')
            self.assertIn('pending_revision', target)
            self.assertEqual({c['id'] for c in data['nodes'] if c['p'] == target['i']}, {'file', 'other'})
            self.assertEqual({n['id'] for n in data['nodes'] if n['id']}, identities)
        self.assertEqual(sum(n.get('proposal_context_replaced_by') == 'folder' for n in data['nodes']), 1)

    def test_nested_anonymous_contexts_merge_but_real_folder_ids_do_not(self):
        from test_viewer_revision import tree, workbook, revision
        for conflict in (False, True):
            data = tree()
            data['nodes'] += [node(4, 0, '2024'), node(5, 4, 'Minutes'),
                              node(6, 5, 'Nested', sid='actual-child'),
                              node(7, 2, 'Nested', sid='other-actual' if conflict else ''),
                              node(8, 7, 'Already routed.pdf', False, 'other')]
            if conflict:
                with self.assertRaisesRegex(ValueError, 'REVISION_VIEWER_CHILD_COLLISION'):
                    v.refresh_proposal(data, workbook(), folder_revisions=[revision(False)], proposed_layout=True)
                continue
            v.refresh_proposal(data, workbook(), folder_revisions=[revision(False)], proposed_layout=True)
            target = v.resolve_folder_path(data, 'Root/2024/Minutes/Nested')
            self.assertEqual(target['id'], 'actual-child')
            self.assertEqual(next(n for n in data['nodes'] if n['id'] == 'other')['p'], target['i'])

    def test_context_identity_is_a_real_collision(self):
        from test_viewer_revision import tree, workbook, revision
        data = tree()
        data['nodes'] += [node(4, 0, '2024'), node(5, 4, 'Minutes')]
        data['nodes'][5]['context_id'] = 'existing-destination'
        with self.assertRaisesRegex(ValueError, 'REVISION_VIEWER_TARGET_COLLISION'):
            v.refresh_proposal(data, workbook(), folder_revisions=[revision(False)], proposed_layout=True)

    def test_removing_retain_instruction_does_not_hide_future_proposal(self):
        data, workbook = self.fixture()
        v.retained_proposal_containers(data, workbook)
        workbook['sheets']['Move Manifest'] = workbook['sheets']['Move Manifest'][:1]
        v.retained_proposal_containers(data, workbook)
        self.assertFalse(data['nodes'][2].get('viewer_hidden'))
        self.assertFalse(data['nodes'][3].get('viewer_hidden'))


if __name__ == '__main__': unittest.main()
