"""Current sheet proposals are displayable without conferring execution authority."""
import copy
import unittest
from test_viewer_revision import tree, workbook, revision
from uqmc_recovery import viewer_publication as v


class PendingProposalTests(unittest.TestCase):
    def render_tree(self, created=False, repeat=1):
        d = tree(created)
        d['execution_sections'] = {'sections':[{'path':'Root','verified_file_ids':['file']}]}
        for n in d['nodes']:
            if n['f']:n['migration_summary']={'status':'COMPLETE','file_count':1,'completed_file_count':1}
        before_ids = {n['id'] for n in d['nodes'] if n['id']}
        for _ in range(repeat):
            updates=v.proposed_folder_updates(d,workbook(created))
            self.assertEqual(len(updates),1)
            self.assertFalse(updates[0]['verified'])
            v.refresh_proposal(d,workbook(created),folder_revisions=updates)
            v.refresh_proposal(d,workbook(created),folder_revisions=updates,proposed_layout=True)
            v.mark_proposed_revision_status(d,updates)
            self.assertEqual(before_ids,{n['id'] for n in d['nodes'] if n['id']})
        return d

    def test_sheet_revision_is_shown_before_freeze_and_keeps_actual_id(self):
        for created in (False,True):
            d=self.render_tree(created)
            folder=v.resolve_folder_path(d,'Root/2024/Minutes')
            child=next(n for n in d['nodes'] if n['id']=='file')
            self.assertEqual(child['p'],folder['i'])
            self.assertEqual(folder['pending_revision']['before_path'],'Root/General/2024')
            self.assertEqual(folder['migration_summary']['status'],'REVISION_PENDING')
            self.assertEqual(folder['migration_summary']['completed_file_count'],0)
            self.assertEqual(folder['migration_summary']['historically_completed_file_count'],1)
            self.assertEqual(d['execution_sections']['sections'][0]['proposal_status'],'PENDING_REVISION')
            self.assertEqual(v.resolve_folder_path(d,'Root/General')['migration_summary']['file_count'],0)
            self.assertNotIn('destination_overlay',v.resolve_folder_path(d,'Root/2024'))
            if created:self.assertEqual(folder['destination_overlay']['destination_id'],'folder')

    def test_repeated_publication_remains_pending_with_same_placement(self):
        for created in (False,True):
            d=self.render_tree(created,repeat=2)
            self.assertEqual(d['proposal_revision_summary']['pending_file_count'],1)
            self.assertEqual(len([n for n in d['nodes'] if n['n']=='Minutes']),1)

    def test_split_or_unlisted_child_does_not_claim_intact_folder_mapping(self):
        for missing in (False,True):
            d=tree(True);d['nodes'].append(dict(d['nodes'][3],i=4,id='other',n='Other.pdf'))
            w=workbook(True)
            if not missing:w['sheets']['Proposed Renames'].append(['other','Other.pdf','Root/Elsewhere'])
            self.assertEqual(v.proposed_folder_updates(d,w),[])

    def test_existing_path_alias_is_not_a_new_pending_revision(self):
        d=tree(True);d['nodes'][1]['o']='Old general'
        w=workbook(True);w['sheets']['Proposed Renames'][1][2]='Root/Old general/2024'
        self.assertEqual(v.proposed_folder_updates(d,w),[])

    def test_conflicting_different_identity_is_not_overwritten(self):
        d=tree();d['nodes'] += [dict(d['nodes'][1],i=4,p=0,n='2024',id='year'),dict(d['nodes'][2],i=5,p=4,n='Minutes',id='other')]
        u=v.proposed_folder_updates(d,workbook())
        with self.assertRaisesRegex(ValueError,'REVISION_VIEWER_TARGET_COLLISION'):
            v.refresh_proposal(d,workbook(),folder_revisions=u,proposed_layout=True)

    def test_verified_current_revision_can_drop_pending_status(self):
        d=tree();u=v.proposed_folder_updates(d,workbook(),folder_revisions=[revision(True)])
        self.assertTrue(u[0]['verified'])

if __name__=='__main__':unittest.main()
