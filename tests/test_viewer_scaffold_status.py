"""Live scaffold names can differ from proposal aliases without duplicating IDs."""
import copy
import unittest
from test_viewer_publication import fixture,node
from uqmc_recovery import viewer_publication as v


class ScaffoldStatusTests(unittest.TestCase):
    def fixture(self):
        data,bundle=fixture()
        # The source record is distinct from the scaffold context identity.
        data['nodes'][2].update(n='01 Constitution',o='0 Constitution',id='',context_id='scaffold')
        data['nodes'].append(node(4,2,'2026'))
        data['nodes'][3]['p']=4
        v.refresh_proposal(data,{'exported_at_utc':'2026-10-26T00:00:00Z','sheets':{
            'Proposed Renames':[['Source ID','Proposed name + type suffix','Proposed destination folder'],
                               ['file-a','File.pdf','Root/Section/01 Constitution/2026']]}})
        bundle['html']['items']=[i for i in bundle['html']['items'] if i['source_id']=='file-a']
        sec=bundle['sections']['sections'][0]
        sec['created_destinations']=[
            {'id':'scaffold','name':'0 Constitution','parent_ids':['new-root'],
             'destination_link':'https://drive.google.com/drive/folders/scaffold','mode':'REUSED'},
            {'id':'year','name':'2026','parent_ids':['scaffold'],
             'destination_link':'https://drive.google.com/drive/folders/year','mode':'CREATED'}]
        bundle['workbook']['items'][0]['observed']['parent_ids']=['year']
        return data,bundle

    def test_context_id_binds_parent_alias_and_new_child_without_execution_identity(self):
        data,bundle=self.fixture();source_ids={n['id'] for n in data['nodes'] if n['id']}
        for _ in range(2):
            v.integrate(data,bundle)
            parent=v.resolve_folder_path(data,'Root/Section/01 Constitution')
            year=v.resolve_folder_path(data,'Root/Section/01 Constitution/2026')
            self.assertEqual(parent['destination_overlay']['destination_id'],'scaffold')
            self.assertEqual(year['destination_overlay']['destination_id'],'year')
            self.assertEqual(parent['id'],'')
            self.assertEqual({n['id'] for n in data['nodes'] if n['id']},source_ids)

    def test_hidden_old_wrapper_cannot_collide_with_new_year_destination(self):
        data,bundle=self.fixture()
        parent=v.resolve_folder_path(data,'Root/Section/01 Constitution')
        hidden=node(len(data['nodes']),parent['i'],'2026');hidden['viewer_hidden']=True
        data['nodes'].append(hidden)
        v.integrate(data,bundle)
        self.assertEqual(v.resolve_folder_path(data,'Root/Section/01 Constitution/2026')['destination_overlay']['destination_id'],'year')
        self.assertTrue(hidden['viewer_hidden'])

    def test_duplicate_context_identity_is_rejected(self):
        data,bundle=self.fixture()
        duplicate=node(len(data['nodes']),1,'Other category');duplicate['context_id']='scaffold'
        data['nodes'].append(duplicate)
        with self.assertRaisesRegex(ValueError,'CREATED_DESTINATION_ID_NOT_UNIQUE'):
            v.integrate(data,bundle)

    def test_literal_slash_live_name_does_not_create_path_fragments(self):
        data,bundle=self.fixture()
        bundle['sections']['sections'][0]['created_destinations'][0]['name']='Old / Superseded'
        v.integrate(data,bundle)
        self.assertEqual(v.resolve_folder_path(data,'Root/Section/01 Constitution/2026')['destination_overlay']['destination_id'],'year')


if __name__=='__main__':unittest.main()
