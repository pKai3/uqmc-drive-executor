import copy
import unittest
from tools import update_viewer as v


def node(i, parent, name, folder=True, sid='', old=None):
    return dict(i=i, p=parent, s='destination', n=name, o=old, f=folder,
                id=sid, u='', context_id='', c=[], changed=False, review=False)


def fixture():
    data = {'nodes': [node(0,-1,'Root'), node(1,0,'Section'),
                      node(2,1,'New subfolder',sid='original-folder',old='Old subfolder'),
                      node(3,2,'File.pdf',False,'file-a')],
            'roots':{'destination':[0],'retained':[]}, 'stats':{}, 'notes':[]}
    item = {'source_id':'file-a','observed':{'exact_name':None,'parent_ids':None,'mime_type':None}, 'location':{'verified_path':None}}
    other = copy.deepcopy(item);other['source_id']='original-folder'
    section = dict(section_id='section',title='Section',path='Root/Section',
                   destination_id='new-root',destination_url='https://drive.google.com/drive/folders/new-root',
                   mode='CREATED',expected_file_count=1,completed_file_count=1,status='COMPLETED',
                   retained_source_folder_ids=['original-folder'],created_folder_count=2,
                   created_destinations=[{'id':'new-child','name':'New subfolder','parent_ids':['new-root'],'destination_link':'https://drive.google.com/drive/folders/new-child'}])
    bundle={'html':{'snapshot_id':'snapshot','journal_sequence':5,'items':[item,other]},
            'sections':{'snapshot_id':'snapshot','journal_sequence':5,'sections':[section]},
            'workbook':{'items':[{'source_id':'file-a','observed':{'read_state':'VERIFIED','parent_ids':['new-child']},'rename':{'effect':'APPLIED'},'move':{'effect':'APPLIED'},'location':{'effect':'DIRECT'}}]}}
    return data,bundle


class ViewerPublicationTests(unittest.TestCase):
    def test_created_destination_keeps_source_identity_and_link(self):
        d,b=fixture();v.integrate(d,b)
        n=d['nodes'][2]
        self.assertEqual(n['id'],'original-folder')
        self.assertEqual(n['destination_overlay']['destination_id'],'new-child')
        self.assertTrue(n['migration_summary']['source_retained'])
        self.assertEqual(n['migration_summary']['status'],'COMPLETE')

    def test_excluded_or_new_source_identity_cannot_enter(self):
        d,b=fixture();extra=copy.deepcopy(b['html']['items'][0]);extra['source_id']='not-approved'
        b['html']['items'].append(extra)
        with self.assertRaisesRegex(ValueError,'ALLOWLIST'):v.integrate(d,b)

    def test_raw_private_observation_rejected(self):
        d,b=fixture();b['html']['items'][0]['observed']['exact_name']='Private detail'
        with self.assertRaisesRegex(ValueError,'PRIVATE_OBSERVATION'):v.integrate(d,b)

    def test_snapshot_mismatch_rejected(self):
        d,b=fixture();b['sections']['journal_sequence']=6
        with self.assertRaisesRegex(ValueError,'SNAPSHOT'):v.integrate(d,b)

    def test_membership_mismatch_rejected(self):
        d,b=fixture();b['sections']['sections'][0]['expected_file_count']=2
        with self.assertRaisesRegex(ValueError,'MEMBERSHIP'):v.integrate(d,b)

    def test_invalid_destination_link_rejected(self):
        d,b=fixture();b['sections']['sections'][0]['destination_url']='https://example.org/collect'
        with self.assertRaisesRegex(ValueError,'UNSAFE'):v.integrate(d,b)

    def test_same_count_different_membership_is_not_completed(self):
        d,b=fixture();b['workbook']['items'][0]['observed']['parent_ids']=['some-other-parent']
        with self.assertRaisesRegex(ValueError,'MEMBER_NOT_VERIFIED'):v.integrate(d,b)

    def test_proposal_refresh_respects_folder_rename_alias(self):
        d,_=fixture()
        w={'exported_at_utc':'2026-01-01T00:00:00Z','matrices':{'Proposed Renames':[
          ['Source ID','Proposed name + type suffix','Proposed destination folder'],
          ['file-a','New file.pdf','Root/Section/Old subfolder']]}}
        result=v.refresh_proposal(d,w)
        self.assertEqual(result,{'proposal_records_updated':1,'proposal_contexts_added':0})
        self.assertEqual(d['nodes'][3]['p'],2)
        self.assertEqual(d['nodes'][3]['n'],'New file.pdf')

    def test_proposal_refresh_groups_and_is_repeatable(self):
        d,_=fixture()
        w={'exported_at_utc':'2026-01-01T00:00:00Z','sheets':{'Proposed Renames':[
          ['Source ID','Proposed name + type suffix','Proposed destination folder'],
          ['file-a','File.pdf','Root/Historical']]}}
        result=v.refresh_proposal(d,w)
        self.assertEqual(result['proposal_contexts_added'],1)
        self.assertEqual(v.refresh_proposal(d,w)['proposal_records_updated'],0)
        ids=[n['id'] for n in d['nodes'] if n['id']]
        self.assertEqual(sorted(ids),['file-a','original-folder'])


if __name__=='__main__':unittest.main()
