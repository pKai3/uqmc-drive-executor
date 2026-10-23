"""Manifest-only folder mappings stay proposals and preserve source identity."""
import copy
import unittest
from test_viewer_revision import tree, workbook
from uqmc_recovery import viewer_publication as v


class ManifestFolderTests(unittest.TestCase):
    def fixture(self, scope='Exact source-ID subtree partition; exclude separately scoped descendants'):
        d=tree();w=workbook();w['sheets']['Proposed Renames']=w['sheets']['Proposed Renames'][:2]
        w['sheets']['Proposed Renames'][1][2]='Root/Social/2024'
        w['sheets']['Move Manifest']=[['Source ID','Exact target path','Scope'],['folder','Root/Social/2024',scope]]
        d['execution_sections']={'sections':[{'path':'Root','verified_file_ids':['file']}]}
        return d,w

    def test_keep_name_partition_root_and_children_are_pending_at_exact_target(self):
        for scope in ('Exact source-ID subtree partition; exclude separately scoped descendants','Whole subtree'):
            d,w=self.fixture(scope);before={n['id'] for n in d['nodes']}
            for _ in range(2):
                r=v.proposed_folder_updates(d,w)
                self.assertEqual(len(r),1);self.assertFalse(r[0]['verified'])
                v.refresh_proposal(d,w,folder_revisions=r)
                v.refresh_proposal(d,w,folder_revisions=r,proposed_layout=True)
                v.mark_proposed_revision_status(d,r)
                folder=v.resolve_folder_path(d,'Root/Social/2024')
                child=next(n for n in d['nodes'] if n['id']=='file')
                self.assertEqual(folder['id'],'folder');self.assertEqual(child['p'],folder['i'])
                self.assertEqual(folder['migration_summary']['completed_file_count'],0)
                self.assertEqual(folder['migration_summary']['historically_completed_file_count'],1)
                self.assertEqual(before,{n['id'] for n in d['nodes']})

    def test_separately_scoped_file_keeps_its_independent_target(self):
        d,w=self.fixture();w['sheets']['Proposed Renames'][1][2]='Root/Elsewhere'
        r=v.proposed_folder_updates(d,w)
        v.refresh_proposal(d,w,folder_revisions=r,proposed_layout=True)
        child=next(n for n in d['nodes'] if n['id']=='file')
        self.assertEqual(child['p'],v.resolve_folder_path(d,'Root/Elsewhere')['i'])
        self.assertEqual(v.resolve_folder_path(d,'Root/Social/2024')['id'],'folder')

    def test_ambiguous_same_source_targets_are_rejected(self):
        d,w=self.fixture();w['sheets']['Move Manifest'].append(['folder','Root/Other/2024','Whole subtree'])
        with self.assertRaisesRegex(ValueError,'AMBIGUOUS_MANIFEST_FOLDER_TARGET'):
            v.proposed_folder_updates(d,w)

    def test_retain_container_and_undisclosed_identity_do_not_relocate(self):
        d,w=self.fixture('Retain container only')
        w['sheets']['Move Manifest'].append(['unknown','Root/Social/Unknown','Whole subtree'])
        self.assertEqual(v.proposed_folder_updates(d,w),[])

    def test_explicit_folder_rename_takes_precedence(self):
        d,w=self.fixture();w['sheets']['Proposed Renames'].append(['folder','Events','Root/Social'])
        r=v.proposed_folder_updates(d,w)
        self.assertEqual(r[0]['target_path'],'Root/Social/Events')

    def test_different_identity_at_destination_is_not_overwritten(self):
        d,w=self.fixture();d['nodes'] += [dict(d['nodes'][1],i=4,p=0,n='Social',id='social'),dict(d['nodes'][2],i=5,p=4,id='other')]
        r=v.proposed_folder_updates(d,w)
        with self.assertRaisesRegex(ValueError,'REVISION_VIEWER_TARGET_COLLISION'):
            v.refresh_proposal(d,w,folder_revisions=r,proposed_layout=True)


if __name__=='__main__':unittest.main()
