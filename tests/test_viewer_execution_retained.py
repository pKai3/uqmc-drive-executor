import unittest
from test_viewer_publication import node
from uqmc_recovery import viewer_publication as v


class ExecutionRetainedTests(unittest.TestCase):
    def fixture(self):
        data={'nodes':[node(0,-1,'Assets'),node(1,0,'Registers'),
            node(2,1,'First Aid Supplies',sid='wrapper'),node(3,1,'2023'),
            node(4,2,'Kit.pdf',False,'file')],
            'roots':{'destination':[0],'retained':[]},'notes':[],
            'execution_sections':{'basis':'verified_journal_readback','sections':[
                {'path':'Assets','destination_id':'root','retained_source_folder_ids':['wrapper'],
                 'verified_file_ids':['file'],'verified_destinations':[{'id':'year'}]}]}}
        data['nodes'][3]['destination_overlay']={'destination_id':'year'}
        workbook={'exported_at_utc':'2026-10-26T00:00:00Z','sheets':{
            'Proposed Renames':[['Source ID','Proposed name + type suffix','Proposed destination folder'],
                ['wrapper','2023','Assets/Registers'],['file','Kit.pdf','Assets/Registers/2023']]}}
        revision={'source_id':'wrapper','before_path':'Assets/Registers/First Aid Supplies',
            'target_path':'Assets/Registers/2023','target_name':'2023','verified':False}
        return data,workbook,revision

    def test_mixed_source_wrapper_is_not_pending_revision_of_created_destination(self):
        data,workbook,revision=self.fixture()
        identities={n.get('id') for n in data['nodes'] if n.get('id')}
        for _ in range(2):
            self.assertEqual(v.proposed_folder_updates(data,workbook,folder_revisions=[revision]),[])
            v.refresh_proposal(data,workbook,folder_revisions=[revision],proposed_layout=True)
            v.mark_proposed_revision_status(data,[revision])
            target=v.resolve_folder_path(data,'Assets/Registers/2023')
            self.assertEqual(target['destination_overlay']['destination_id'],'year')
            wrapper=next(n for n in data['nodes'] if n.get('id')=='wrapper')
            self.assertTrue(wrapper['viewer_hidden']);self.assertNotIn('pending_revision',wrapper)
            file=next(n for n in data['nodes'] if n.get('id')=='file')
            self.assertEqual(file['p'],target['i']);self.assertNotIn('pending_revision',file)
            self.assertEqual(identities,{n.get('id') for n in data['nodes'] if n.get('id')})

    def test_active_destination_is_never_hidden_as_a_retained_source(self):
        data,workbook,_=self.fixture()
        data['execution_sections']['sections'][0]['verified_destinations'].append({'id':'wrapper'})
        self.assertNotIn('wrapper',v.retained_proposal_containers(data,workbook))
        self.assertFalse(data['nodes'][2].get('viewer_hidden'))

    def test_unverified_retained_claim_cannot_hide_real_collision(self):
        data,workbook,revision=self.fixture();data['execution_sections']['basis']='unverified'
        with self.assertRaisesRegex(ValueError,'REVISION_VIEWER_TARGET_COLLISION'):
            v.refresh_proposal(data,workbook,folder_revisions=[revision],proposed_layout=True)

if __name__=='__main__':unittest.main()
